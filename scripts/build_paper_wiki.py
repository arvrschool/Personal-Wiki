#!/usr/bin/env python3
"""
Build an llm-wiki–style knowledge base from structured source entries.

Each entry → wiki/sources/<slug>.md               (source page)
Each key concept → wiki/entities/<concept>.md     (entity page)
Each topic/category → wiki/topics/<topic>.md      (topic page)
Global navigation → index.md, log.md

Usage:
    python build_paper_wiki.py \\
        --entries /path/to/entries.json \\
        --wiki-dir /path/to/wiki-output \\
        --topic "Personal Wiki" \\
        [--rebuild]          # overwrite existing pages
        [--only-seeds]       # only write source pages for seed papers

Output structure:
    <wiki-dir>/
    ├── index.md
    ├── log.md
    ├── wiki/
    │   ├── sources/         ← one page per source entry
    │   ├── articles/        ← one page per web article
    │   ├── entities/        ← optional concept pages
    │   └── topics/          ← topic/category pages
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from config_loader import cfg, get_wiki_paths
from entry_semantics import DEFAULT_FALLBACK_CATEGORY, concept_metadata, extract_concepts
from entry_store import (
    ENTITY_ENTRIES_HEADER,
    ENTITY_ENTRIES_HEADER_ALIASES,
    ENTITY_PERSPECTIVES_HEADER,
    TOPIC_ENTRIES_HEADER,
    TOPIC_ENTRIES_HEADER_ALIASES,
    add_entries_argument,
    count_entry_kinds,
    entries_file_label,
    entry_meta_badge,
    entry_owner_label,
    entry_time_label,
    find_first_header,
    load_entries,
)
from git_utils import git_commit_paths
from template_utils import (
    extract_frontmatter_value,
    markdown_bullets,
    markdown_links,
    render_template,
    resolve_template,
    scalar_or_null,
    yaml_array,
    yaml_bool,
    yaml_string,
)
from toc_utils import inject_toc, update_toc

TODAY = date.today().isoformat()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """Convert text to a safe filename slug."""
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:150]


def _table_esc(text: str) -> str:
    """Escape pipe characters for markdown table compatibility."""
    if not text:
        return ""
    return str(text).replace("|", "\\|")


def _wikilink(title: str) -> str:
    """Create an Obsidian-style wikilink. If it looks like a concept, slugify the target."""
    if not title:
        return ""
    slug = _slug(title)
    # If the title is already exactly equal to its slugified version, 
    # don't add a redundant | label.
    if slug == title:
        return f"[[{title}]]"
    # If it looks like a concept (title maps to slug with just casing/space changes),
    # use [[slug|Title]] and escape the pipe for tables.
    if slug == title.lower().replace(" ", "-"):
        return f"[[{slug}\\|{title}]]"
    return f"[[{_table_esc(title)}]]"


def _infer_year(arxiv_id: str, published: str = "") -> str:
    """Infer year from published date or arXiv ID (YYMM.NNNNN)."""
    if published and len(str(published)) >= 4:
        return str(published)[:4]
    
    # ArXiv ID pattern: YYMM.NNNNN
    m = re.search(r"(\d{2})\d{2}\.\d+", arxiv_id)
    if m:
        yy = int(m.group(1))
        # 00-90 -> 2000-2090, 91-99 -> 1900-1999
        return str(2000 + yy if yy < 91 else 1900 + yy)
    
    return ""


def _is_enriched_content(text: str) -> bool:
    """
    Return True if file has real content (not just a stub placeholder).
    Heuristic: check for common stub markers and length.
    """
    stubs = ["（待补充）", "待消化后填写", "（待填）", "### 整体架构\n（待补充）"]
    
    # If the file is very large, it's almost certainly enriched even if some stubs remain
    if len(text) > 4000:
        return True
        
    # If it's short and has any stub marker, it's a stub
    if any(s in text for s in stubs) and len(text) < 2500:
        return False
        
    # If it's extremely short, it's a stub or metadata-only
    if len(text) < 600:
        return False
        
    return True

def _write_if_missing(path: Path, content: str, rebuild: bool, label: str = "source") -> bool:
    """Write file; skip if exists and not rebuilding. Never overwrite enriched content."""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if _is_enriched_content(existing):
            # Hard protection: enriched files are never overwritten via template rendering
            return False
        if not rebuild:
            return False
    
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _replace_section(content: str, header: str, new_body: str) -> str:
    """Replace the body of a markdown section in-place, preserving all other sections."""
    # Ensure header has ## if it's a level 2 header
    header_regex = re.escape(header)
    pattern = re.compile(
        r"(^" + header_regex + r"\s*\n)"  # section header line
        r"([\s\S]*?)"                            # current body (lazy)
        r"(?=^##|\Z)",                           # stop at next ## or EOF
        re.MULTILINE,
    )
    match = pattern.search(content)
    if match:
        updated = content[:match.start(2)] + new_body.rstrip("\n") + "\n\n" + content[match.end(2):]
        return updated
    else:
        # Section missing — append it
        return content.rstrip("\n") + f"\n\n{header}\n\n{new_body.rstrip()}\n"


def _update_entity_page(path: Path, concept: str, papers: list[dict]) -> bool:
    """Update only the '涉及该概念的条目' section; preserve user-written sections."""
    existing = path.read_text(encoding="utf-8")

    citing_papers = []
    for p in papers:
        if concept in _get_concepts(p):
            aid = p.get("arxiv_id", "")
            slug_title = _slug(p.get("title") or "paper")
            name = f"{slug_title}-{aid}" if aid else slug_title
            name = name.rstrip("-")
            citing_papers.append((name, p.get("year")))

    source_links = "\n".join(
        f"- [[{name}]] ({year})" for name, year in citing_papers[:10]
    ) if citing_papers else "- （暂无关联论文）"

    header = find_first_header(existing, ENTITY_ENTRIES_HEADER_ALIASES, ENTITY_ENTRIES_HEADER)
    updated = _replace_section(existing, f"## {header}", source_links + "\n")
    # Also bump the updated date
    updated = re.sub(r"^updated:.*$", f"updated: {TODAY}", updated, flags=re.MULTILINE)
    if updated != existing:
        path.write_text(updated, encoding="utf-8")
        return True
    return False


def _update_topic_page(path: Path, topic: str, papers: list[dict]) -> bool:
    """Update only the '条目汇总' table; preserve user-written sections."""
    existing = path.read_text(encoding="utf-8")

    topic_papers = [p for p in papers if _get_category(p) == topic]
    rows = [_topic_table_row(p) for p in topic_papers]

    table_body = _topic_table(rows)

    header = find_first_header(existing, TOPIC_ENTRIES_HEADER_ALIASES, TOPIC_ENTRIES_HEADER)
    updated = _replace_section(existing, f"## {header}", table_body + "\n")
    updated = re.sub(r"^updated:.*$", f"updated: {TODAY}", updated, flags=re.MULTILINE)
    if updated != existing:
        path.write_text(updated, encoding="utf-8")
        return True
    return False


def _update_source_frontmatter(content: str, paper: dict) -> str:
    """Updates the category and year in the frontmatter of a source page based on papers.json."""
    if not content.startswith("---"):
        return content
    
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content
        
    fm = parts[1]
    
    # Update category
    cat = paper.get("category")
    if cat:
        fm = re.sub(r"^category:\s*.*$", f"category: {cat}", fm, flags=re.MULTILINE)

    # Update year
    year = paper.get("year")
    if year:
        fm = re.sub(r"^year:\s*.*$", f"year: {year}", fm, flags=re.MULTILINE)
        
    return f"---{fm}---{parts[2]}"


def _update_source_page(path: Path, paper: dict) -> bool:
    """Update the '基本信息' section and frontmatter of an existing source page from papers.json."""
    existing = path.read_text(encoding="utf-8")
    template_id = extract_frontmatter_value(existing, "template_id") or paper.get("template_id") or "research_paper"
    
    title = paper.get("title") or "Untitled"
    arxiv_id = paper.get("arxiv_id") or ""
    authors = paper.get("authors") or []
    year = paper.get("year") or "?"
    citations = paper.get("citations") or 0
    is_seed = paper.get("is_seed", False)
    category = _get_category(paper)
    
    arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else paper.get("url", "")
    pdf_url = f"../../../pdfs/{arxiv_id}.pdf" if arxiv_id else paper.get("pdf_url", "")
    
    authors_str = ", ".join(authors[:4])
    if len(authors) > 4:
        authors_str += " et al."

    facts_lines = [
        f"- **分类**：{category}",
        f"- **作者**：{authors_str or '（未知）'}",
        f"- **发表年份**：{year}",
        f"- **引用次数**：{citations:,}",
        f"- **链接**：[{arxiv_id if arxiv_id else '访问原文'}]({arxiv_url})" if arxiv_url else "- **来源**：（暂无）",
    ]
    if pdf_url:
        facts_lines.append(f"- **PDF**：[下载/查看]({pdf_url})")
    if is_seed:
        facts_lines.append("- **是否种子论文**：是 🌱")

    info_header = "关键事实" if template_id == "generic" else "基本信息"
    updated = _replace_section(existing, f"## {info_header}", "\n".join(facts_lines))

    # Also sync frontmatter
    updated = _update_source_frontmatter(updated, paper)
    updated = update_toc(updated)

    if updated != existing:
        path.write_text(updated, encoding="utf-8")
        return True
    return False


def _truncate(text: str, max_chars: int = 400) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


FALLBACK_CATEGORY = cfg("wiki", "fallback_category", DEFAULT_FALLBACK_CATEGORY)


def _load_topic_taxonomy(topics_dir: Path) -> dict[str, str]:
    taxonomy: dict[str, str] = {}
    if topics_dir.exists():
        for tp in sorted(topics_dir.glob("*.md")):
            name = tp.stem
            content = tp.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^>\s*(.+)", content, re.MULTILINE)
            desc = m.group(1).strip() if m else name
            taxonomy[name] = desc
    if FALLBACK_CATEGORY not in taxonomy:
        taxonomy[FALLBACK_CATEGORY] = "未归类内容"
    return taxonomy


def _get_category(paper: dict) -> str:
    cat = paper.get("category", "")
    if cat:
        return cat
    return FALLBACK_CATEGORY


def _get_concepts(paper: dict) -> list[str]:
    return extract_concepts(paper, max_items=6)


def _source_kind_from_paper(paper: dict) -> str:
    source_kind = (paper.get("source_kind") or "").strip()
    if source_kind:
        return source_kind
    if paper.get("arxiv_id"):
        return "arxiv"
    if paper.get("local_pdf"):
        return "pdf"
    if paper.get("url"):
        return "url"
    return "meta"


def _related_pages(category: str, concepts: list[str]) -> str:
    pages = ["[[index]]"]
    if category:
        pages.append(f"[[{category}]]")
    pages.extend(f"[[{c}]]" for c in concepts[:5])
    seen = set()
    out = []
    for page in pages:
        if page not in seen:
            out.append(page)
            seen.add(page)
    return "\n".join(f"- {page}" for page in out)


def _entry_page_name(entry: dict) -> str:
    aid = entry.get("arxiv_id", "")
    slug_title = _slug(entry.get("title") or "entry")
    page_name = f"{slug_title}-{aid}" if aid else slug_title
    return page_name.rstrip("-")


def _first_summary_sentence(entry: dict, limit: int = 60) -> str:
    for key in ("summary", "abstract", "description", "key_claim"):
        text = re.sub(r"\s+", " ", str(entry.get(key) or "")).strip()
        if not text:
            continue
        sentence = re.split(r"(?<=[。！？.!?])\s+", text, maxsplit=1)[0].strip()
        if sentence:
            return sentence[:limit].rstrip()
    return ""


def _one_line_contribution(entry: dict) -> str:
    summary = _first_summary_sentence(entry)
    if summary:
        return summary
    if _is_research_entry(entry):
        return "（待填）"
    title = (entry.get("title") or "该条目").strip()
    return f"围绕《{title[:20]}》提供资料线索"


def _topic_table_row(entry: dict) -> str:
    page_name = _entry_page_name(entry)
    title = (entry.get("title") or "?")[:50]
    owner = entry_owner_label(entry)
    when = entry_time_label(entry)
    contrib = _one_line_contribution(entry)
    return f"| {_wikilink(page_name)} | {_table_esc(title)} | {_table_esc(owner)} | {_table_esc(when)} | {_table_esc(contrib)} |"


def _topic_table(rows: list[str]) -> str:
    return (
        "| 条目页面 | 标题 | 作者/来源 | 时间 | 核心信息 |\n"
        "|---------|------|-----------|------|----------|\n"
    ) + ("\n".join(rows) if rows else "| （暂无） | | | | |")


def _is_research_entry(entry: dict) -> bool:
    template_id = (entry.get("template_id") or "").strip().lower()
    entry_type = (entry.get("entry_type") or "").strip().lower()
    return template_id == "research_paper" or entry_type == "paper" or bool(entry.get("arxiv_id"))


def _is_article_entry(entry: dict) -> bool:
    template_id = (entry.get("template_id") or "").strip().lower()
    entry_type = (entry.get("entry_type") or "").strip().lower()
    return template_id == "web_article" or entry_type == "article"


def _topic_compare_block(entries: list[dict]) -> tuple[str, str]:
    research_topic = bool(entries) and all(_is_research_entry(entry) for entry in entries)
    if research_topic:
        return (
            "（各论文在方法、训练目标、数据要求和局限性等维度的横向对比）",
            "\n".join([
                "| 论文 | 年份 | 架构 | 训练目标 | 数据要求 | 核心贡献 | 局限性 |",
                "|------|------|------|----------|----------|----------|--------|",
                "| （待填） | | | | | | |",
            ]),
        )
    return (
        "（各条目在来源、关注点、适用范围和关键信息等维度的横向对比）",
        "\n".join([
            "| 条目 | 时间 | 来源类型 | 核心关注点 | 关键信息 | 备注 |",
            "|------|------|----------|------------|----------|------|",
            "| （待填） | | | | | |",
        ]),
    )


# ---------------------------------------------------------------------------
# Source page (one per paper)
# ---------------------------------------------------------------------------

def build_source_page(paper: dict, template_dir: str | Path | None = None) -> str:
    title = paper.get("title") or "Untitled"
    arxiv_id = paper.get("arxiv_id") or ""
    authors = paper.get("authors") or []
    author = paper.get("author") or (authors[0] if authors else "")
    account = paper.get("account") or ""
    year = paper.get("year") or _infer_year(arxiv_id, str(paper.get("date") or paper.get("published_at") or "")) or "?"
    date_value = paper.get("date") or paper.get("published_at") or (year if year != "?" else None)
    citations = paper.get("citations") or 0
    abstract = _truncate(paper.get("abstract") or "", 500)
    project_urls = paper.get("project_urls") or []
    is_seed = paper.get("is_seed", False)
    category = _get_category(paper)
    concepts = _get_concepts(paper)
    template_id = (paper.get("template_id") or resolve_template("auto", item=paper, template_dir=template_dir).template_id)
    default_entry_type = "paper" if template_id == "research_paper" else ("article" if template_id == "web_article" else "generic")

    arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else paper.get("url", "")
    pdf_url = f"../../../pdfs/{arxiv_id}.pdf" if arxiv_id else paper.get("pdf_url", "")
    source_url = paper.get("url") or arxiv_url
    platform = paper.get("platform") or paper.get("source") or ("article" if template_id == "web_article" else "")
    platform_display_map = {
        "weixin": "微信公众号",
        "zhihu": "知乎",
        "xiaohongshu": "小红书",
        "twitter": "X (Twitter)",
        "youtube": "YouTube",
        "github": "GitHub",
        "github_issue": "GitHub Issue",
        "github_pr": "GitHub PR",
        "github_discussion": "GitHub Discussion",
        "substack": "Substack",
        "medium": "Medium",
        "notion": "Notion",
        "huggingface": "HuggingFace",
        "web": "网页",
        "site": "网页",
        "news": "新闻",
        "article": "文章",
    }
    platform_display = platform_display_map.get(str(platform), str(platform) or "文章")
    insights = paper.get("key_insights") or []
    mentions_body = paper.get("mentions_markdown") or "（待补充）"

    authors_str = ", ".join(authors[:4])
    if len(authors) > 4:
        authors_str += " et al."
    byline = " · ".join([v for v in [account, author] if v]) or authors_str or "（未知）"

    concept_links = markdown_bullets([_wikilink(c) for c in concepts], fallback="（暂无提取）")

    research_method_stub = "（待补充 — 运行 enrich_wiki.py 后由 LLM 自动填写）"
    research_detailed_method_stub = "\n".join([
        "（待补充 — 运行 enrich_wiki.py 后由 LLM 自动补全：核心方法细节、架构描述、关键公式和训练推理流程）",
        "",
        "### 整体架构",
        "（待补充）",
        "",
        "### 核心模块",
        "（待补充）",
        "",
        "### 关键公式",
        "（待补充）",
        "",
        "### 训练与推理流程",
        "（待补充）",
        "",
        "### 架构描述（基于图表）",
        "（待补充）",
    ])
    research_results_stub = "（待补充 — 运行 enrich_wiki.py 后由 LLM 自动填写）"
    research_figures_stub = "（待补充 — 可使用 enrich_wiki.py --figures 自动抓取论文图表）"

    project_section = ""
    if project_urls:
        links = markdown_links(project_urls)
        project_section = f"\n## 项目资源\n\n{links}\n"

    seed_badge = " 🌱" if is_seed else ""
    sources_frontmatter = yaml_array([source_url] if source_url else [])
    source_type = "arxiv" if arxiv_id else "other"
    if template_id == "research_paper":
        facts_lines = [
            f"- **分类**：{category}",
            f"- **作者**：{authors_str or '（未知）'}",
            f"- **发表年份**：{year}",
            f"- **引用次数**：{citations:,}",
            f"- **链接**：[{arxiv_id if arxiv_id else '访问原文'}]({source_url})" if source_url else "- **来源**：（暂无）",
        ]
        if pdf_url:
            facts_lines.append(f"- **PDF**：[下载/查看]({pdf_url})")
        if is_seed:
            facts_lines.append("- **是否种子论文**：是 🌱")
    elif template_id == "web_article":
        facts_lines = [
            f"- **分类**：{category}",
            f"- **平台**：{platform_display}",
            f"- **作者/账号**：{byline}",
            f"- **发布时间**：{entry_time_label(paper)}",
            f"- **链接**：[访问原文]({source_url})" if source_url else "- **来源**：（暂无）",
        ]
    else:
        facts_lines = [
            f"- **分类**：{category}",
            f"- **来源类型**：{platform_display if platform else '通用条目'}",
            f"- **来源/作者**：{entry_owner_label(paper)}",
            f"- **时间**：{entry_time_label(paper)}",
            f"- **链接**：[访问原文]({source_url})" if source_url else "- **来源**：（暂无）",
        ]
        if project_urls:
            facts_lines.append(f"- **项目资源**：{len(project_urls)} 项")

    context = {
        "template_id_yaml": yaml_string(template_id),
        "entry_type_yaml": yaml_string(paper.get("entry_type") or default_entry_type),
        "source_kind_yaml": yaml_string(_source_kind_from_paper(paper)),
        "created": TODAY,
        "updated": TODAY,
        "sources_frontmatter": sources_frontmatter,
        "source_type_yaml": yaml_string(source_type),
        "arxiv_id_yaml": yaml_string(arxiv_id),
        "source_url_yaml": yaml_string(source_url),
        "title": title,
        "title_yaml": yaml_string(title),
        "lead": abstract[:120] + "…" if len(abstract) > 120 else (abstract or arxiv_url or "（待补充来源说明）"),
        "author_yaml": yaml_string(author),
        "account_yaml": yaml_string(account),
        "date_value": scalar_or_null(date_value),
        "source_url": source_url,
        "seed_badge": seed_badge,
        "year_value": scalar_or_null(year if year != "?" else None),
        "citations_value": scalar_or_null(citations),
        "is_seed_value": yaml_bool(is_seed),
        "category_yaml": yaml_string(category),
        "category_tag": category,
        "summary_body": abstract or "（待补充）",
        "facts_body": "\n".join(facts_lines),
        "highlights_body": (
            "\n".join(f"{i}. **要点 {i}**：{point}" for i, point in enumerate(insights, start=1))
            if insights
            else (
                "（待消化后填写 — 给每篇论文添加 3-5 个核心要点）\n\n"
                "1. **要点一**：...\n"
                "2. **要点二**：...\n"
                "3. **要点三**：..."
                if template_id == "research_paper"
                else "（待补充）"
            )
        ),
        "method_body": research_method_stub if template_id == "research_paper" else "",
        "detailed_method_body": research_detailed_method_stub if template_id == "research_paper" else "",
        "results_body": research_results_stub if template_id == "research_paper" else "",
        "figures_body": research_figures_stub if template_id == "research_paper" else "",
        "concept_links": concept_links,
        "relations_body": "（待补充）" if template_id != "research_paper" else "（待补充 — 运行 enrich_wiki.py 后由 LLM 自动填写）",
        "citation_body": "（待补充）",
        "notes_body": "（待补充）",
        "actions_body": "（待补充）",
        "platform_yaml": yaml_string(str(platform)),
        "platform_tag": str(platform) or "article",
        "platform_display": platform_display,
        "byline": byline,
        "mentions_body": mentions_body,
        "image_section": "",
        "related_pages": _related_pages(category, concepts),
        "project_section": project_section,
    }
    return render_template(template_id, context, template_dir=template_dir)


# ---------------------------------------------------------------------------
# Entity page (one per concept)
# ---------------------------------------------------------------------------

def build_entity_page(concept: str, papers: list[dict]) -> str:
    concept_type, hint = concept_metadata(concept)
    type_zh = {"concept": "概念", "person": "人物",
               "organization": "组织", "tool": "工具", "method": "方法"}.get(concept_type, "概念")

    citing_papers = []
    for p in papers:
        if concept in _get_concepts(p):
            aid = p.get("arxiv_id", "")
            slug_title = _slug(p.get("title") or "paper")
            name = f"{slug_title}-{aid}" if aid else slug_title
            name = name.rstrip("-")
            citing_papers.append((name, p.get("year")))

    source_links = "\n".join(
        f"- {_wikilink(name)} ({year})" for name, year in citing_papers[:10]
    ) if citing_papers else "- （暂无关联论文）"

    return f"""---
tags: [实体, {type_zh}]
created: {TODAY}
updated: {TODAY}
sources: []
---

# {concept}

> {hint if hint else "（待填写：一句话描述这个概念）"}

## 简介

（从相关条目中提取的关于这个概念的详细介绍）

## 关键信息

- **类型**：{type_zh}
- **领域**：（待填写）
- **相关概念**：（链接到相关实体）

## 在当前知识库中的角色

（这个概念在当前知识库中扮演什么角色？）

## {ENTITY_ENTRIES_HEADER}

{source_links}

## {ENTITY_PERSPECTIVES_HEADER}

（不同条目对这个概念的不同阐述、演进或侧重点，标注来源）

## 相关页面

- {_wikilink("index")}
"""


# ---------------------------------------------------------------------------
# Topic page (one per category)
# ---------------------------------------------------------------------------

def build_topic_page(topic: str, description: str, papers: list[dict], template_dir: str | Path | None = None) -> str:
    topic_papers = [p for p in papers if _get_category(p) == topic]
    rows = [_topic_table_row(p) for p in topic_papers]
    table_rows = "\n".join(rows) if rows else "| （暂无） | | | | |"
    
    all_concepts = sorted({concept for p in topic_papers for concept in _get_concepts(p)})
    concept_links = markdown_bullets([_wikilink(c) for c in all_concepts[:15]], fallback="（暂无相关概念）")

    template_id = "research_topic"
    context = {
        "title": topic,
        "category_tag": topic,
        "created": TODAY,
        "updated": TODAY,
        "sources_frontmatter": yaml_array([]),
        "lead": description,
        "summary_body": "（运行 enrich_wiki.py --only-topics 自动生成综述，或在此手动填写该主题的核心认知）",
        "concept_links": concept_links,
        "table_rows": table_rows,
        "parent_category": "index",
    }
    
    try:
        return render_template(template_id, context, template_dir=template_dir)
    except Exception:
        # Fallback if template doesn't exist yet or fails
        table = _topic_table(rows)
        return f"""---
tags: [主题]
created: {TODAY}
updated: {TODAY}
sources: []
---

# {topic}

> {description}

## {TOPIC_ENTRIES_HEADER}

{table}

## 相关页面

- {_wikilink("index")}
"""


# ---------------------------------------------------------------------------
# index.md
# ---------------------------------------------------------------------------

def build_index(topic: str, papers: list[dict], categories: dict) -> str:
    total = len(papers)
    seeds = [p for p in papers if p.get("is_seed")]
    n_with_links = sum(1 for p in papers if p.get("project_urls") or p.get("url") or p.get("source_url"))
    entry_kind_counts = count_entry_kinds(papers)
    all_concepts = sorted({concept for paper in papers for concept in _get_concepts(paper)})

    sections = []
    for cat, desc in categories.items():
        cat_papers = [p for p in papers if _get_category(p) == cat]
        if not cat_papers:
            continue
        lines = []
        for p in cat_papers:
            page_name = _entry_page_name(p)
            seed = " 🌱" if p.get("is_seed") else ""
            lines.append(f"- {_wikilink(page_name)}{seed} ({entry_meta_badge(p)})")
        sections.append(
            f"### {cat}\n\n> {desc}\n\n" + "\n".join(lines)
        )

    categories_section = "\n\n---\n\n".join(sections)
    entity_list = "\n".join(f"- {_wikilink(c)}" for c in all_concepts[:100]) if all_concepts else "- （暂无自动提取的实体页）"
    topic_list = "\n".join(f"- {_wikilink(t)}" for t in categories.keys())

    return f"""# {topic} 知识库索引

> 最后更新：{TODAY}

---

## 概览

- **主题**：{topic}
- **条目总数**：{total}
- **研究论文**：{entry_kind_counts['research']}
- **网页/文章**：{entry_kind_counts['article']}
- **通用条目**：{entry_kind_counts['generic']}
- **种子条目**：{len(seeds)}
- **含外部链接**：{n_with_links}
- **Wiki 页面**：（随消化进度增长）

---

## 实体页

> 人物、概念、技术组件等

{entity_list}

---

## 主题页

> 按研究方向分类

{topic_list}

---

## 来源条目

> 每个结构化来源对应一个条目页

{categories_section}

---

## 综合分析

> 跨条目的深度综合报告

（暂无，使用 `digest` 工作流生成）
"""


# ---------------------------------------------------------------------------
# log.md initial entry
# ---------------------------------------------------------------------------

def build_log(topic: str, n_sources: int, n_entities: int, n_topics: int) -> str:
    return f"""# {topic} 知识库日志

> 记录每次知识库更新操作

---

## {TODAY} init | 批量导入知识条目

- 导入来源：结构化条目数据（entries.json / papers.json）
- 新增来源条目页：{n_sources}
- 新增实体页：{n_entities}
- 新增主题页：{n_topics}
- 操作类型：batch-ingest（条目批量导入）

---
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build llm-wiki from structured source entries")
    add_entries_argument(ap, required=True)
    ap.add_argument("--wiki-dir", required=True, help="Output wiki root directory")
    ap.add_argument("--topic", default=cfg("wiki", "topic", "Personal Wiki"), help="Wiki topic name")
    ap.add_argument("--rebuild", action="store_true", help="Overwrite existing pages")
    ap.add_argument("--only-seeds", action="store_true",
                    help="Only build source pages for seed entries")
    ap.add_argument("--template-dir", default=None,
                    help="Directory containing editable page templates (default: repo templates/)")
    git_group = ap.add_mutually_exclusive_group()
    git_group.add_argument("--git-commit", dest="git_commit", action="store_true",
                           help="Create a git commit after writing files")
    git_group.add_argument("--no-git-commit", dest="git_commit", action="store_false",
                           help="Disable git commit for this run")
    ap.set_defaults(git_commit=None)
    args = ap.parse_args()

    entries_path = Path(args.entries_path)
    if not entries_path.exists():
        print(f"[ERROR] entries file not found: {entries_path}", file=sys.stderr)
        sys.exit(1)

    papers = load_entries(entries_path)
    wiki_root = Path(args.wiki_dir)
    wiki_root.mkdir(parents=True, exist_ok=True)
    should_git_commit = args.git_commit if args.git_commit is not None else cfg("git", "auto_commit", False)

    if args.only_seeds:
        papers_to_write = [p for p in papers if p.get("is_seed")]
    else:
        papers_to_write = papers

    # Determine output directories (support both legacy nested 'wiki/' and flat root)
    if (wiki_root / "sources").exists() or (wiki_root / "entities").exists():
        # Flat structure
        sources_dir = wiki_root / "sources"
        entities_dir = wiki_root / "entities"
        topics_dir = wiki_root / "topics"
        articles_dir = wiki_root / "articles"
    elif (wiki_root / "wiki" / "sources").exists():
        # Legacy/Default nested structure
        sources_dir = wiki_root / "wiki" / "sources"
        entities_dir = wiki_root / "wiki" / "entities"
        topics_dir = wiki_root / "wiki" / "topics"
        articles_dir = wiki_root / "wiki" / "articles"
    else:
        # Default fallback
        sources_dir = wiki_root / "wiki" / "sources"
        entities_dir = wiki_root / "wiki" / "entities"
        topics_dir = wiki_root / "wiki" / "topics"
        articles_dir = wiki_root / "wiki" / "articles"
        
    for d in [sources_dir, entities_dir, topics_dir, articles_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # --- Source pages ---
    import re as _re

    # Pre-build index for duplicate detection across BOTH sources and articles
    existing_by_arxiv: dict[str, Path] = {}
    existing_by_urlslug: dict[str, Path] = {}
    for d in [sources_dir, articles_dir]:
        for existing_f in d.glob("*.md"):
            m = _re.search(r"(\d{4}\.\d{4,5})", existing_f.stem)
            if m:
                existing_by_arxiv.setdefault(m.group(1), existing_f)
            else:
                # index by first 40 chars of stem as URL-slug proxy
                existing_by_urlslug.setdefault(existing_f.stem[:40], existing_f)

    def _find_existing(aid: str, slug_t: str) -> Path | None:
        """Return existing file for this paper if one exists under a different name."""
        if aid and aid in existing_by_arxiv:
            return existing_by_arxiv[aid]
        if not aid:
            # Match no-id papers by longest common prefix (≥40 chars) of slug
            key = slug_t[:40]
            if key in existing_by_urlslug:
                return existing_by_urlslug[key]
        return None

    written_sources = []
    skipped_sources = []
    touched_files: list[Path] = []
    for paper in papers_to_write:
        aid = paper.get("arxiv_id") or ""
        slug_t = _slug(paper.get("title") or "paper")
        filename = f"{slug_t}-{aid}.md" if aid else f"{slug_t}.md"
        
        # Decide target directory based on entry type
        is_article = _is_article_entry(paper)
        target_dir = articles_dir if is_article else sources_dir
        path = target_dir / filename

        existing_path = _find_existing(aid, slug_t)
        if existing_path is not None and existing_path != path:
            # A file for this paper already exists — update it, skip creation
            if _update_source_page(existing_path, paper):
                written_sources.append(existing_path.name)
                touched_files.append(existing_path)
            else:
                skipped_sources.append(filename + f" (exists as {existing_path.name})")
            continue

        content = inject_toc(build_source_page(paper, template_dir=args.template_dir))
        if _write_if_missing(path, content, args.rebuild):
            written_sources.append(filename)
            touched_files.append(path)
            if aid:
                existing_by_arxiv[aid] = path
            else:
                existing_by_urlslug[slug_t[:40]] = path
        else:
            if _update_source_page(path, paper):
                written_sources.append(filename)
                touched_files.append(path)
            else:
                skipped_sources.append(filename)

    print(f"[wiki] sources/articles: {len(written_sources)} written, {len(skipped_sources)} skipped",
          file=sys.stderr)

    # --- Entity pages ---
    all_concepts: set[str] = set()
    for p in papers:
        all_concepts.update(_get_concepts(p))

    written_entities = []
    updated_entities = []
    for concept in sorted(all_concepts):
        # Strictly slugify for filename
        slug_name = _slug(concept)
        if not slug_name: continue
        
        path = entities_dir / f"{slug_name}.md"
        content = build_entity_page(concept, papers)
        if _write_if_missing(path, content, args.rebuild, label="entity"):
            written_entities.append(concept)
            touched_files.append(path)
        else:
            if _update_entity_page(path, concept, papers):
                updated_entities.append(concept)
                touched_files.append(path)

    print(f"[wiki] entities: {len(written_entities)} written, {len(updated_entities)} updated", file=sys.stderr)

    # --- Topic pages ---
    topic_taxonomy = _load_topic_taxonomy(topics_dir)
    for topic_name in sorted({_get_category(p) for p in papers if _get_category(p)}):
        topic_taxonomy.setdefault(topic_name, f"{topic_name} 相关条目汇总与知识脉络")
    written_topics = []
    updated_topics = []
    for topic_name, description in topic_taxonomy.items():
        path = topics_dir / f"{topic_name}.md"
        content = build_topic_page(topic_name, description, papers)
        if _write_if_missing(path, content, args.rebuild, label="topic"):
            written_topics.append(topic_name)
            touched_files.append(path)
        else:
            if _update_topic_page(path, topic_name, papers):
                updated_topics.append(topic_name)
                touched_files.append(path)

    print(f"[wiki] topics: {len(written_topics)} written, {len(updated_topics)} updated", file=sys.stderr)

    # --- index.md ---
    index_path = wiki_root / "index.md"
    new_index_content = build_index(args.topic, papers, topic_taxonomy)
    
    if index_path.exists():
        existing_index = index_path.read_text(encoding="utf-8")
        # Surgically update sections while preserving others (like ## 文章)
        updated_index = existing_index
        for section in ["## 概览", "## 实体页", "## 主题页", "## 来源条目"]:
            # Extract new body for this section
            m = re.search(r"(^" + re.escape(section) + r"\s*\n)([\s\S]*?)(?=^##|\Z)", new_index_content, re.MULTILINE)
            if m:
                new_body = m.group(2)
                updated_index = _replace_section(updated_index, section, new_body)
        
        # Also update the title/date line at top
        updated_index = re.sub(r"^# .*$", f"# {args.topic} 知识库索引", updated_index, flags=re.MULTILINE)
        updated_index = re.sub(r"^> 最后更新：.*$", f"> 最后更新：{TODAY}", updated_index, flags=re.MULTILINE)
        
        if updated_index != existing_index:
            index_path.write_text(updated_index, encoding="utf-8")
            print(f"[wiki] index.md surgically updated", file=sys.stderr)
    else:
        index_path.write_text(new_index_content, encoding="utf-8")
        print(f"[wiki] index.md created", file=sys.stderr)
    
    touched_files.append(index_path)

    # --- log.md ---
    log_path = wiki_root / "log.md"
    if not log_path.exists():
        log_path.write_text(build_log(args.topic, len(written_sources), len(written_entities), len(written_topics)), encoding="utf-8")
    else:
        existing = log_path.read_text(encoding="utf-8")
        entry = (f"\n## {TODAY} batch-ingest | 重新导入条目数据\n\n"
                 f"- 数据文件：{entries_file_label(entries_path)}\n"
                 f"- 新增/更新来源条目页：{len(written_sources)}\n"
                 f"- 跳过（无变化）：{len(skipped_sources)}\n\n---\n")
        log_path.write_text(existing + entry, encoding="utf-8")
    touched_files.append(log_path)

    # --- Optional git commit scoped to touched files only ---
    if should_git_commit:
        new_pages = [f for f in written_sources if f not in skipped_sources]
        if len(new_pages) == 1:
            msg = f"ingest: {new_pages[0].replace('.md', '')}"
        elif new_pages:
            msg = f"ingest: {len(new_pages)} new pages, {len(skipped_sources)} updated"
        else:
            msg = f"update: index.md and {len(touched_files)} files"
        committed, detail = git_commit_paths(wiki_root, touched_files, msg)
        if committed:
            print(f"[git] committed: {detail}", file=sys.stderr)
        else:
            print(f"[git] commit skipped: {detail}", file=sys.stderr)

    # --- Summary ---
    print(f"\n## Wiki 构建完成：{args.topic}\n")
    print(f"**输出目录**：`{wiki_root.resolve()}/`")
    print(f"\n**文件结构**：")
    print(f"```\n{wiki_root.name}/\n├── index.md\n├── log.md\n└── wiki/\n    ├── sources/\n    ├── articles/\n    ├── entities/\n    └── topics/\n```")


if __name__ == "__main__":
    main()
