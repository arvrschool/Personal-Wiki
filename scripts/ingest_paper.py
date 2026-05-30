#!/usr/bin/env python3
"""
Ingest a new source entry into an existing llm-wiki knowledge base.

Workflow:
  1. Resolve the source entry — from arXiv ID, PDF path, or raw metadata dict
  2. Extract PDF text (if a local PDF is provided)
  3. Create a new source page (with enriched content if LLM available)
  4. Update entries.json / papers.json (append the new entry)
  5. Update entity pages that link to this entry
  6. Update the relevant topic page and index
  7. Append to log.md

Usage:
    # Ingest by arXiv ID (fetches metadata automatically)
    python ingest_paper.py \\
        --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --arxiv-id 2301.08243

    # Ingest a local PDF (metadata from user-supplied args or extracted from PDF)
    python ingest_paper.py \\
        --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --pdf /path/to/paper.pdf --title "My Paper" --authors "Alice" "Bob" --year 2025

    # Ingest with known metadata dict (JSON string)
    python ingest_paper.py \\
        --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --meta '{"title":"...","arxiv_id":"...","authors":[...],"year":2025,"abstract":"..."}'

    # Ingest and mark as a seed paper
    python ingest_paper.py \\
        --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --arxiv-id 2301.08243 --seed

    # Ingest with a generic source template
    python ingest_paper.py \\
        --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --meta '{"title":"A bookmark","url":"https://example.com","abstract":"..."}' \\
        --template generic

    # [FUTURE] Enrich with LLM
    python ingest_paper.py \\
        --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --arxiv-id 2301.08243 \\
        --llm-provider anthropic --llm-model claude-opus-4-7
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from config_loader import cfg
from entry_store import (
    ENTITY_ENTRIES_HEADER,
    ENTITY_ENTRIES_HEADER_ALIASES,
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
    save_entries,
)
from entry_semantics import (
    DEFAULT_FALLBACK_CATEGORY,
    concept_metadata,
    extract_concepts,
    infer_category,
    looks_research_entry,
)
from llm_cli_utils import (
    anthropic_client_kwargs,
    call_llm,
    describe_provider_selection,
    openai_client_kwargs,
    resolve_model_arg,
    resolve_provider,
)
from template_utils import (
    markdown_bullets,
    markdown_links,
    render_template,
    resolve_template,
    scalar_or_null,
    yaml_array,
    yaml_bool,
    yaml_string,
)
from toc_utils import inject_toc

TODAY = date.today().isoformat()

LLM_AVAILABLE = True


# ---------------------------------------------------------------------------
# arXiv metadata fetch
# ---------------------------------------------------------------------------

def fetch_arxiv_metadata(arxiv_id: str) -> dict:
    """Fetch paper metadata from arXiv XML API."""
    # Normalize ID (strip version suffix, URL prefix)
    arxiv_id = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", arxiv_id)
    arxiv_id = re.sub(r"^[Aa]r[Xx]iv:", "", arxiv_id)
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id).strip()

    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}&max_results=1"
    xml_data = ""
    try:
        # Try with standard urlopen
        with urllib.request.urlopen(url, timeout=15) as resp:
            xml_data = resp.read().decode("utf-8")
    except Exception:
        # Fallback 1: try disabling SSL verification
        try:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(url, timeout=15, context=ctx) as resp:
                xml_data = resp.read().decode("utf-8")
        except Exception:
            # Fallback 2: try curl if available
            try:
                result = subprocess.run(["curl", "-sSL", "-k", url], capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    xml_data = result.stdout
            except Exception:
                pass

    if not xml_data:
        print(f"[warn] arXiv fetch failed for {arxiv_id}. Continuing with minimal metadata.", file=sys.stderr)
        return {"arxiv_id": arxiv_id, "title": arxiv_id, "authors": [], "year": None, "abstract": ""}

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        print(f"[warn] arXiv XML parse failed for {arxiv_id}.", file=sys.stderr)
        return {"arxiv_id": arxiv_id, "title": arxiv_id, "authors": [], "year": None, "abstract": ""}

    entry = root.find("atom:entry", ns)
    if entry is None:
        return {"arxiv_id": arxiv_id, "title": arxiv_id, "authors": [], "year": None, "abstract": ""}

    title = (entry.findtext("atom:title", "", ns) or "").replace("\n", " ").strip()
    abstract = (entry.findtext("atom:summary", "", ns) or "").replace("\n", " ").strip()
    authors = [
        a.findtext("atom:name", "", ns)
        for a in entry.findall("atom:author", ns)
    ]
    published = entry.findtext("atom:published", "", ns) or ""
    year = int(published[:4]) if published else None

    # Extract GitHub / project URL from abstract comment field
    comment_el = entry.find("{http://arxiv.org/schemas/atom}comment")
    comment = comment_el.text if comment_el is not None else ""
    project_urls = re.findall(r"https?://github\.com/[\w/.-]+", (abstract or "") + " " + (comment or ""))

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "year": year,
        "abstract": abstract,
        "citations": 0,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "project_urls": list(set(project_urls)),
        "source": "arxiv",
    }


# ---------------------------------------------------------------------------
# PDF text extraction (reused from enrich_wiki.py logic)
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_path: Path, max_chars: int = 8000) -> str:
    txt_path = pdf_path.with_suffix(".txt")
    if txt_path.exists():
        try:
            return txt_path.read_text(encoding="utf-8")[:max_chars].strip()
        except Exception:
            pass
    text = ""
    try:
        import pdfminer.high_level as pdfminer
        text = pdfminer.extract_text(str(pdf_path))
        for marker in ["\nReferences\n", "\nBibliography\n", "\nREFERENCES\n"]:
            idx = text.find(marker)
            if idx != -1:
                text = text[:idx]
    except ImportError:
        pass
    if not text:
        try:
            import pdfplumber
            pages_text = []
            with pdfplumber.open(str(pdf_path)) as pdf:
                for page in pdf.pages[:12]:
                    t = page.extract_text()
                    if t:
                        pages_text.append(t)
            text = "\n".join(pages_text)
        except ImportError:
            pass
    if text:
        try:
            txt_path.write_text(text, encoding="utf-8")
        except Exception:
            pass
    return text[:max_chars].strip()


def classify_paper(paper: dict) -> str:
    """Assign a category to a paper based on title/abstract/authors."""
    return infer_category(paper, fallback=cfg("wiki", "fallback_category", DEFAULT_FALLBACK_CATEGORY))


# ---------------------------------------------------------------------------
# Markdown helpers (duplicated from enrich_wiki.py to keep ingest self-contained)
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:150]


def _paper_slug(paper: dict) -> str:
    arxiv_id = paper.get("arxiv_id", "")
    title = paper.get("title") or ""
    slug = _slug(title)
    return f"{slug}-{arxiv_id}" if arxiv_id else slug


def _paper_filename(paper: dict) -> str:
    return _paper_slug(paper) + ".md"


def _extract_concepts(paper: dict) -> list[str]:
    return extract_concepts(paper)


def _build_entity_page_stub(concept: str, paper: dict) -> str:
    concept_type, hint = concept_metadata(concept)
    type_zh = {
        "concept": "概念",
        "person": "人物",
        "organization": "组织",
        "tool": "工具",
        "method": "方法",
    }.get(concept_type, "概念")
    slug = _paper_slug(paper)
    year = paper.get("year", "?")
    source_links = f"- [[{slug}]] ({year})"

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

## 不同条目中的观点

（不同条目对这个概念的不同阐述、演进或侧重点，标注来源）

## 相关页面

- [[index]]
"""


def _generate_key_points(
    paper: dict,
    pdf_text: str,
    llm_provider: str,
    llm_model: str,
    template_id: str,
    direct_input: str | None = None,
) -> str:
    """Generate the highlights section body for a source page."""
    abstract = paper.get("abstract") or ""
    title = paper.get("title") or ""
    source_text = pdf_text or abstract

    if LLM_AVAILABLE and llm_provider != "none":
        if template_id == "research_paper":
            prompt = f"""你是一位 AI 研究员，正在为个人知识库撰写一篇研究论文摘要页。

论文标题：{title}
论文内容：
{source_text[:6000]}

请用中文提取 3-5 个核心观点，格式（每条以 "N. **主题**：内容" 开头）：
- 聚焦方法创新、关键设计决策、实验亮点
- 每条不超过 80 字
"""
        else:
            prompt = f"""你正在整理个人知识库中的一个通用条目。

条目标题：{title}
内容：
{source_text[:6000]}

请用中文提取 3-5 个要点，格式（每条以 "N. **主题**：内容" 开头）：
- 优先提炼关键信息、判断、结论或可执行信息
- 每条不超过 80 字
"""
        try:
            raw = call_llm(prompt, provider=llm_provider, model=llm_model, direct_input=direct_input)
            points = [l.strip() for l in raw.split("\n") if re.match(r"^\d+\.", l.strip())]
            if not points:
                points = [l.strip() for l in raw.split("\n") if l.strip().startswith("-")]
            if points:
                return "\n".join(points)
        except Exception as e:
            print(f"[warn] key-point generation failed, falling back to rules: {e}", file=sys.stderr)

    # Rule-based fallback
    sentences = [s.strip() for s in re.split(r"[.。!?]", abstract) if len(s.strip()) > 30]
    points = []
    contrib = next((s for s in sentences if re.search(r"\bwe (propose|introduce|present|develop)\b", s, re.I)), "")
    result = next((s for s in sentences if re.search(r"(outperform|state.of.the.art|improve|achieve)", s, re.I)), "")
    if contrib:
        label = "核心贡献" if template_id == "research_paper" else "核心信息"
        points.append(f"1. **{label}**：{contrib[:80]}")
    if result and result != contrib:
        label = "实验结果" if template_id == "research_paper" else "结果/结论"
        points.append(f"{len(points)+1}. **{label}**：{result[:80]}")
    while len(points) < 3:
        points.append(f"{len(points)+1}. （需读全文后补充）")
    return "\n".join(points)


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


def _build_related_pages(category: str, concepts: list[str]) -> str:
    pages = ["[[index]]"]
    if category:
        pages.append(f"[[{category}]]")
    pages.extend(f"[[{concept}]]" for concept in concepts[:5])
    deduped: list[str] = []
    seen = set()
    for page in pages:
        if page not in seen:
            deduped.append(page)
            seen.add(page)
    return "\n".join(f"- {page}" for page in deduped)


def build_source_page(
    paper: dict,
    pdf_text: str,
    llm_provider: str,
    llm_model: str,
    template_id: str,
    template_dir: str | Path | None,
    direct_input: str | None = None,
) -> str:
    """Render a complete source page markdown string from the selected template."""
    arxiv_id = paper.get("arxiv_id") or ""
    title = paper.get("title") or ""
    authors = paper.get("authors") or []
    author = paper.get("author") or (authors[0] if authors else "")
    account = paper.get("account") or ""
    year = paper.get("year") or "?"
    date_value = paper.get("date") or paper.get("published_at") or (year if year != "?" else None)
    citations = paper.get("citations") or 0
    abstract = (paper.get("abstract") or "")[:400]
    category = paper.get("category") or classify_paper(paper)
    is_seed = paper.get("is_seed", False)
    url = paper.get("url") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")
    pdf_url = paper.get("pdf_url") or (f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "")
    project_urls = paper.get("project_urls") or []
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
    default_entry_type = "paper" if template_id == "research_paper" else ("article" if template_id == "web_article" else "generic")

    seed_badge = " 🌱" if is_seed else ""
    authors_str = ", ".join(authors[:4])
    if len(authors) > 4:
        authors_str += " et al."
    byline = " · ".join([v for v in [account, author] if v]) or authors_str or "（未知）"

    concepts = _extract_concepts(paper)
    concept_links = markdown_bullets([f"[[{c}]]" for c in concepts], fallback="（暂无提取）")

    research_method_stub = "（待补充 — 运行 enrich_wiki.py 后由 LLM 自动填写）"
    research_detailed_method_stub = "\n".join([
        "（待补充 — enrich 会按论文模板补全核心方法细节、架构图和公式）",
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
        "### 与图表对应",
        "（待补充）",
    ])
    research_results_stub = "（待补充 — 运行 enrich_wiki.py 后由 LLM 自动填写）"
    research_figures_stub = "（待补充 — 可使用 enrich_wiki.py --figures 自动抓取论文图表）"

    key_points = _generate_key_points(paper, pdf_text, llm_provider, llm_model, template_id, direct_input=direct_input)
    mentions_body = paper.get("mentions_markdown") or "（待补充）"

    relations = "（待在知识库形成更多交叉引用后补充）\n"
    source_type = "arxiv" if arxiv_id else "other"
    source_label = arxiv_id or "访问原文"
    source_field = "arXiv" if arxiv_id else "链接"

    if template_id == "research_paper":
        facts_lines = [
            f"- **分类**：{category}",
            f"- **作者**：{authors_str or '（未知）'}",
            f"- **发表年份**：{year}",
            f"- **引用次数**：{citations:,}",
            f"- **{source_field}**：[{source_label}]({url})" if url else "- **来源**：（暂无）",
        ]
        if pdf_url:
            facts_lines.append(f"- **PDF**：[下载]({pdf_url})")
        if is_seed:
            facts_lines.append("- **是否种子条目**：是 🌱")
    elif template_id == "web_article":
        facts_lines = [
            f"- **分类**：{category}",
            f"- **平台**：{platform_display}",
            f"- **作者/账号**：{byline}",
            f"- **发布时间**：{entry_time_label(paper)}",
            f"- **链接**：[访问原文]({url})" if url else "- **来源**：（暂无）",
        ]
    else:
        facts_lines = [
            f"- **分类**：{category}",
            f"- **来源类型**：{platform_display if platform else '通用条目'}",
            f"- **来源/作者**：{entry_owner_label(paper)}",
            f"- **时间**：{entry_time_label(paper)}",
            f"- **链接**：[访问原文]({url})" if url else "- **来源**：（暂无）",
        ]
        if project_urls:
            facts_lines.append(f"- **项目资源**：{len(project_urls)} 项")

    project_section = ""
    if project_urls:
        project_section = "## 项目资源\n\n" + markdown_links(project_urls) + "\n\n"

    summary_body = abstract + ("…" if len(paper.get("abstract") or "") > 400 else "")
    if not summary_body:
        summary_body = "（待补充）"

    context = {
        "template_id_yaml": yaml_string(template_id),
        "entry_type_yaml": yaml_string(paper.get("entry_type") or default_entry_type),
        "source_kind_yaml": yaml_string(_source_kind_from_paper(paper)),
        "created": TODAY,
        "updated": TODAY,
        "sources_frontmatter": yaml_array([url] if url else []),
        "source_type_yaml": yaml_string(source_type),
        "arxiv_id_yaml": yaml_string(arxiv_id),
        "source_url_yaml": yaml_string(url),
        "title": title,
        "title_yaml": yaml_string(title),
        "lead": abstract[:200] + ("…" if len(abstract) > 200 else "") if abstract else (url or "（待补充来源说明）"),
        "author_yaml": yaml_string(author),
        "account_yaml": yaml_string(account),
        "date_value": scalar_or_null(date_value),
        "source_url": url,
        "platform_yaml": yaml_string(str(platform)),
        "platform_tag": str(platform) or "article",
        "platform_display": platform_display,
        "byline": byline,
        "seed_badge": seed_badge,
        "year_value": scalar_or_null(year if year != "?" else None),
        "citations_value": scalar_or_null(citations),
        "is_seed_value": yaml_bool(is_seed),
        "category_yaml": yaml_string(category),
        "category_tag": category,
        "summary_body": summary_body,
        "facts_body": "\n".join(facts_lines),
        "highlights_body": key_points,
        "method_body": research_method_stub if template_id == "research_paper" else "",
        "detailed_method_body": research_detailed_method_stub if template_id == "research_paper" else "",
        "results_body": research_results_stub if template_id == "research_paper" else "",
        "figures_body": research_figures_stub if template_id == "research_paper" else "",
        "concept_links": concept_links,
        "relations_body": relations.strip(),
        "citation_body": "（待补充）" if template_id == "research_paper" else "",
        "notes_body": "（待补充）",
        "actions_body": "（待补充）",
        "mentions_body": mentions_body,
        "image_section": "",
        "related_pages": _build_related_pages(category, concepts),
        "project_section": project_section,
    }
    return render_template(template_id, context, template_dir=template_dir)


# ---------------------------------------------------------------------------
# Entity page update: append entry to the entity backlink list
# ---------------------------------------------------------------------------

def update_entity_pages(paper: dict, entities_dir: Path) -> int:
    """Add this entry to entity pages for all concepts it touches. Returns count updated."""
    concepts = _extract_concepts(paper)
    updated = 0
    for concept in concepts:
        entity_candidates = [entities_dir / f"{concept}.md", entities_dir / f"{_slug(concept)}.md"]
        entity_path = next((p for p in entity_candidates if p.exists()), None)
        if entity_path is None:
            entities_dir.mkdir(parents=True, exist_ok=True)
            entity_path = entity_candidates[0]
            entity_path.write_text(_build_entity_page_stub(concept, paper), encoding="utf-8")
            updated += 1
            continue
        content = entity_path.read_text(encoding="utf-8")
        slug = _paper_slug(paper)
        link_line = f"- [[{slug}]] ({paper.get('year', '?')})"
        if slug in content:
            continue  # already linked
        header = find_first_header(content, ENTITY_ENTRIES_HEADER_ALIASES, ENTITY_ENTRIES_HEADER)
        content = re.sub(
            rf"(## {re.escape(header)}\n)(.*?)(\n## )",
            lambda m: m.group(1) + m.group(2) + link_line + "\n" + m.group(3),
            content,
            flags=re.DOTALL,
        )
        # Also update the "updated" date in frontmatter
        content = re.sub(r"^updated: .*$", f"updated: {TODAY}", content, flags=re.MULTILINE)
        entity_path.write_text(content, encoding="utf-8")
        updated += 1
    return updated


# ---------------------------------------------------------------------------
# Topic page update: append row to the topic summary table
# ---------------------------------------------------------------------------

def _topic_compare_block_for_entry(paper: dict) -> tuple[str, str]:
    if looks_research_entry(paper):
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


def _build_topic_page_stub(category: str, first_row: str, paper: dict) -> str:
    compare_hint, compare_table = _topic_compare_block_for_entry(paper)
    return f"""---
tags: [主题]
created: {TODAY}
updated: {TODAY}
sources: []
---

# {category}

> {category} 相关条目汇总与后续观察

## 核心观点

（从该分类的多篇条目中综合出的核心认知）

## {TOPIC_ENTRIES_HEADER}

| 条目页面 | 标题 | 作者/来源 | 时间 | 核心信息 |
|---------|------|-----------|------|----------|
{first_row}

## 关键概念

（本主题涉及的核心概念，链接到实体页）

## 对比分析

{compare_hint}

{compare_table}

## 研究脉络

（按时间线梳理该主题下素材的演进方向）

## 未解决的问题

（素材中提到但还没有答案的开放性问题）

## 相关页面

- [[index]]
"""


def _sync_topic_compare_section(content: str, paper: dict) -> str:
    """If the compare block is still the untouched placeholder, align it with the entry type."""
    match = re.search(r"(## 对比分析\s*\n\n)(.*?)(?=\n## |\Z)", content, flags=re.DOTALL)
    if not match:
        return content

    body = match.group(2)
    if "| （待填） |" not in body or "[[" in body:
        return content

    compare_hint, compare_table = _topic_compare_block_for_entry(paper)
    new_body = f"{compare_hint}\n\n{compare_table}\n"
    return content[:match.start(2)] + new_body + content[match.end(2):]

def update_topic_page(paper: dict, topics_dir: Path, category: str) -> bool:
    """Add a row for this entry to the relevant topic page. Returns True if updated."""
    topic_filename = category + ".md"
    topic_path = topics_dir / topic_filename

    slug = _paper_slug(paper)

    title_short = (paper.get("title") or "")[:50]
    authors_short = entry_owner_label(paper)
    year = entry_time_label(paper)
    contrib = _one_line_contribution(paper)
    new_row = f"| [[{slug}]] | {title_short} | {authors_short} | {year} | {contrib} |"

    if not topic_path.exists():
        topic_path.parent.mkdir(parents=True, exist_ok=True)
        topic_path.write_text(_build_topic_page_stub(category, new_row, paper), encoding="utf-8")
        return True

    content = topic_path.read_text(encoding="utf-8")
    if slug in content:
        return False  # already present

    topic_header = find_first_header(content, TOPIC_ENTRIES_HEADER_ALIASES, TOPIC_ENTRIES_HEADER)
    section_match = re.search(rf"(## {re.escape(topic_header)}\s*\n\n)(.*?)(?=\n## |\Z)", content, flags=re.DOTALL)
    if not section_match:
        print(f"  [warn] {topic_header} section not found: {topic_path}")
        return False

    section_body = section_match.group(2).strip("\n")
    section_lines = section_body.splitlines()
    if len(section_lines) < 2:
        print(f"  [warn] {topic_header} table malformed: {topic_path}")
        return False

    table_header = section_lines[:2]
    data_lines = [line for line in section_lines[2:] if line.strip()]
    if data_lines == ["| （暂无） | | | | |"]:
        data_lines = []

    data_lines.append(new_row)
    new_section_body = "\n".join(table_header + data_lines) + "\n"
    updated = (
        content[:section_match.start(2)]
        + new_section_body
        + content[section_match.end(2):]
    )
    updated = _sync_topic_compare_section(updated, paper)

    updated = re.sub(r"^updated: .*$", f"updated: {TODAY}", updated, flags=re.MULTILINE)
    topic_path.write_text(updated, encoding="utf-8")
    return True


def _one_line_contribution(paper: dict) -> str:
    def _first_summary_sentence() -> str:
        for key in ("summary", "abstract", "description", "key_claim"):
            text = re.sub(r"\s+", " ", str(paper.get(key) or "")).strip()
            if not text:
                continue
            sentence = re.split(r"(?<=[。！？.!?])\s+", text, maxsplit=1)[0].strip()
            sentence = sentence.replace("|", "/")
            if sentence:
                return sentence[:60].rstrip()
        return ""

    abstract = paper.get("abstract") or ""
    m = re.search(r"[Ww]e (propose|introduce|present|develop|design)[^.]{10,80}", abstract)
    if m:
        return m.group(0).strip()[:60]
    summary = _first_summary_sentence()
    if summary:
        return summary
    if not looks_research_entry(paper):
        title = (paper.get("title") or "该条目").strip()
        return f"围绕《{title[:20]}》整理资料与线索"
    return f"聚焦于{_domain_hint(paper.get('title',''))}相关问题"


def _domain_hint(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["audio", "speech", "sound", "music"]):
        return "音频/语音领域"
    if any(w in t for w in ["video", "temporal", "action"]):
        return "视频理解"
    if any(w in t for w in ["robot", "locomotion", "manipulation"]):
        return "机器人学习"
    if any(w in t for w in ["language", "text", "nlp", "bert", "llm", "transformer", "attention", "translation"]):
        return "语言模型"
    if any(w in t for w in ["medical", "eeg", "brain"]):
        return "医疗/生物信号"
    if any(w in t for w in ["point cloud", "3d", "lidar"]):
        return "3D 点云"
    if any(w in t for w in ["multimodal", "cross-modal", "vision-language"]):
        return "多模态学习"
    return "视觉表征学习"


# ---------------------------------------------------------------------------
# index.md update
# ---------------------------------------------------------------------------

def _refresh_index_overview(content: str, entries: list[dict]) -> str:
    counts = count_entry_kinds(entries)
    total = len(entries)
    seeds = sum(1 for entry in entries if entry.get("is_seed"))
    with_links = sum(1 for entry in entries if entry.get("project_urls") or entry.get("url") or entry.get("source_url"))

    replacements = {
        r"^- \*\*条目总数\*\*：.*$": f"- **条目总数**：{total}",
        r"^- \*\*研究论文\*\*：.*$": f"- **研究论文**：{counts['research']}",
        r"^- \*\*网页/文章\*\*：.*$": f"- **网页/文章**：{counts['article']}",
        r"^- \*\*通用条目\*\*：.*$": f"- **通用条目**：{counts['generic']}",
        r"^- \*\*种子条目\*\*：.*$": f"- **种子条目**：{seeds}",
        r"^- \*\*含外部链接\*\*：.*$": f"- **含外部链接**：{with_links}",
    }
    updated = content
    for pattern, replacement in replacements.items():
        updated = re.sub(pattern, replacement, updated, flags=re.MULTILINE)
    return updated


def update_index(paper: dict, index_path: Path, category: str, all_entries: list[dict] | None = None) -> bool:
    """Append an entry link to the appropriate section in index.md and refresh overview stats."""
    if not index_path.exists():
        return False
    content = index_path.read_text(encoding="utf-8")
    slug = _paper_slug(paper)
    if slug in content:
        if all_entries:
            refreshed = _refresh_index_overview(content, all_entries)
            if refreshed != content:
                index_path.write_text(refreshed, encoding="utf-8")
                return True
        return False

    seed_badge = " 🌱" if paper.get("is_seed") else ""
    link_line = f"- [[{slug}]]{seed_badge} ({entry_meta_badge(paper)})"

    new_content = content

    # Ensure topic link exists.
    topic_link = f"- [[{category}]]"
    if topic_link not in new_content:
        topic_section = re.search(
            r"(## 主题页\s*\n\n>.*?\n\n)(.*?)(\n\n---\n\n## 来源条目)",
            new_content,
            flags=re.DOTALL,
        )
        if topic_section:
            topic_lines = [line for line in topic_section.group(2).splitlines() if line.strip()]
            topic_lines.append(topic_link)
            topic_lines = list(dict.fromkeys(topic_lines))
            block = topic_section.group(1) + "\n".join(topic_lines)
            new_content = (
                new_content[:topic_section.start()]
                + block
                + topic_section.group(3)
                + new_content[topic_section.end():]
            )

    # Find the section header for this category and append after the last entry.
    pattern = rf"(### {re.escape(category)}.*?\n)((?:- \[\[.*\n)*)"
    updated = re.sub(
        pattern,
        lambda m: m.group(1) + m.group(2) + link_line + "\n",
        new_content,
        flags=re.DOTALL,
        count=1,
    )
    if updated == new_content:
        category_block = (
            f"### {category}\n\n"
            f"> {category} 相关条目\n\n"
            f"{link_line}\n"
        )
        if "\n\n---\n\n## 综合分析" in updated:
            updated = updated.replace(
                "\n\n---\n\n## 综合分析",
                f"\n\n---\n\n{category_block}\n\n---\n\n## 综合分析",
                1,
            )
        elif "\n## 综合分析" in updated:
            updated = updated.replace("\n## 综合分析", f"\n\n{category_block}\n## 综合分析", 1)
        else:
            updated = updated.rstrip("\n") + f"\n\n{category_block}"
    new_content = updated

    if all_entries:
        new_content = _refresh_index_overview(new_content, all_entries)

    index_path.write_text(new_content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# entries.json / papers.json update
# ---------------------------------------------------------------------------

def update_papers_json(paper: dict, papers_path: Path) -> bool:
    """Append entry to the entries store if not already present."""
    papers = load_entries(papers_path)
    existing_ids = {p.get("arxiv_id") for p in papers}
    existing_titles = {p.get("title") for p in papers}

    arxiv_id = paper.get("arxiv_id")
    title = paper.get("title")

    if arxiv_id and arxiv_id in existing_ids:
        print(f"  [skip] {arxiv_id} already in {entries_file_label(papers_path)}")
        return False
    if title and title in existing_titles:
        print(f"  [skip] '{title}' already in {entries_file_label(papers_path)}")
        return False

    papers.append(paper)
    save_entries(papers, papers_path)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ingest a new source entry into an llm-wiki knowledge base")
    parser.add_argument("--wiki-dir", required=True, help="Path to wiki root directory")
    add_entries_argument(parser, required=True)

    # Input source (one of these required)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--arxiv-id", help="arXiv ID (e.g. 2301.08243 or https://arxiv.org/abs/2301.08243)")
    src.add_argument("--pdf", help="Path to a local PDF file")
    src.add_argument("--meta", help="JSON string with paper metadata dict")

    # Optional metadata overrides
    parser.add_argument("--title", help="Paper title (overrides fetched metadata)")
    parser.add_argument("--authors", nargs="+", help="Author names")
    parser.add_argument("--year", type=int, help="Publication year")
    parser.add_argument("--abstract", help="Abstract text")
    parser.add_argument("--category", help="Category/topic name (auto-classified if omitted)")
    parser.add_argument("--seed", action="store_true", help="Mark this paper as a seed paper")
    parser.add_argument("--url", help="URL override (for non-arXiv papers)")
    parser.add_argument("--template", default="auto",
                        help="Page template to use (default: auto; examples: research_paper, generic)")
    parser.add_argument("--template-dir", default=None,
                        help="Directory containing editable page templates (default: repo templates/)")

    # Deprecated KG flags kept as hidden no-ops for backward compatibility.
    parser.add_argument("--no-rebuild-kg", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--topic", default="JEPA", help=argparse.SUPPRESS)

    # LLM options
    parser.add_argument("--llm-provider", default=cfg("llm", "provider", "auto"),
                        choices=["auto", "none", "anthropic", "openai", "ollama", "direct-inference"],
                        help="LLM provider for source-page enrichment")
    parser.add_argument("--llm-model", default=cfg("llm", "model", ""),
                        help="Model name for the LLM provider")
    parser.add_argument("--direct-input", help="File path containing raw LLM output to bypass the direct-inference pause.")

    args = parser.parse_args()
    if args.llm_provider == "none":
        llm_mode = "none (rule-only)"
    else:
        llm_mode = describe_provider_selection(
            args.llm_provider,
            allowed={"anthropic", "openai", "ollama", "direct-inference"},
        )
    print(f"[llm] provider: {llm_mode}", file=sys.stderr, flush=True)

    wiki_dir = Path(args.wiki_dir)
    entries_path = Path(args.entries_path)
    sources_dir = wiki_dir / "wiki" / "sources"
    entities_dir = wiki_dir / "wiki" / "entities"
    topics_dir = wiki_dir / "wiki" / "topics"
    index_path = wiki_dir / "index.md"
    log_path = wiki_dir / "log.md"

    if not entries_path.exists():
        print(f"[error] entries file not found: {entries_path}", file=sys.stderr)
        sys.exit(1)

    # Step 1: Resolve paper metadata
    print("[1/7] Resolving paper metadata...")
    if args.arxiv_id:
        paper = fetch_arxiv_metadata(args.arxiv_id)
    elif args.meta:
        paper = json.loads(args.meta)
    elif args.pdf:
        pdf_path = Path(args.pdf)
        pdf_text_for_meta = extract_pdf_text(pdf_path, max_chars=2000)
        paper = {
            "arxiv_id": "",
            "title": args.title or pdf_path.stem,
            "authors": args.authors or [],
            "year": args.year,
            "abstract": args.abstract or pdf_text_for_meta[:500],
            "citations": 0,
            "source": "local_pdf",
        }
    else:
        print("[error] No input source specified")
        sys.exit(1)

    # Apply overrides
    if args.title:
        paper["title"] = args.title
    if args.authors:
        paper["authors"] = args.authors
    if args.year:
        paper["year"] = args.year
    if args.abstract:
        paper["abstract"] = args.abstract
    if args.url:
        paper["url"] = args.url
    if args.seed:
        paper["is_seed"] = True
    if args.pdf:
        paper["local_pdf"] = args.pdf

    source_kind = "meta"
    if args.arxiv_id or paper.get("arxiv_id"):
        source_kind = "arxiv"
    elif args.pdf or paper.get("local_pdf"):
        source_kind = "pdf"
    elif paper.get("url"):
        source_kind = "url"
    paper["source_kind"] = source_kind
    template_item = {
        **paper,
        "source_kind": source_kind,
    }
    if paper.get("entry_type"):
        template_item["entry_type"] = paper["entry_type"]
    elif looks_research_entry(template_item):
        template_item["entry_type"] = "paper"

    selected_template = resolve_template(
        args.template,
        item=template_item,
        template_dir=args.template_dir,
    )
    paper["template_id"] = selected_template.template_id
    paper["entry_type"] = selected_template.entry_type

    if args.category:
        category = args.category
    elif paper.get("category"):
        category = paper["category"]
    elif selected_template.template_id == "research_paper":
        category = classify_paper(paper)
    else:
        category = cfg("wiki", "fallback_category", DEFAULT_FALLBACK_CATEGORY)
    paper["category"] = category

    print(f"  Title   : {paper.get('title')}")
    print(f"  Authors : {', '.join((paper.get('authors') or [])[:3])}")
    print(f"  Year    : {paper.get('year')}")
    print(f"  Category: {category}")
    print(f"  Template: {selected_template.template_id}")

    # Step 2: Extract PDF text (if local PDF provided directly or via --meta)
    pdf_text = ""
    local_pdf_path = args.pdf or paper.get("local_pdf")
    if local_pdf_path:
        print("[2/7] Extracting PDF text...")
        pdf_text = extract_pdf_text(Path(local_pdf_path))
        print(f"  Extracted {len(pdf_text)} chars")
    else:
        print("[2/7] No local PDF — using abstract only")

    # Step 3: Create source page
    print("[3/7] Creating source page...")
    sources_dir.mkdir(parents=True, exist_ok=True)
    page_content = build_source_page(
        paper,
        pdf_text,
        args.llm_provider,
        args.llm_model,
        selected_template.template_id,
        args.template_dir,
        direct_input=args.direct_input,
    )
    page_filename = _paper_filename(paper)
    page_path = sources_dir / page_filename
    if page_path.exists():
        print(f"  [skip] Source page already exists: {page_filename}")
    else:
        page_path.write_text(inject_toc(page_content), encoding="utf-8")
        print(f"  [ok]   {page_filename}")

    # Step 4: Update entries store
    print(f"[4/7] Updating {entries_file_label(entries_path)}...")
    update_papers_json(paper, entries_path)

    # Step 5: Update entity pages
    print("[5/7] Updating entity pages...")
    n_entities = update_entity_pages(paper, entities_dir)
    print(f"  Updated {n_entities} entity pages")

    # Step 6: Update topic page and index
    print("[6/7] Updating topic page and index...")
    updated_topic = update_topic_page(paper, topics_dir, category)
    if updated_topic:
        print(f"  Added to topic: {category}")
    else:
        print(f"  [skip] Topic page not updated")

    update_index(paper, index_path, category, load_entries(entries_path))
    print("  Updated index.md")

    # Append to log.md
    print("[7/7] Appending to log.md...")
    if log_path.exists():
        log_content = log_path.read_text(encoding="utf-8")
        entry_label = "论文" if selected_template.template_id == "research_paper" else "条目"
        entry = (
            f"\n## {TODAY} ingest | 新增{entry_label}\n\n"
            f"- 标题：{paper.get('title','')}\n"
            f"- arXiv ID：{paper.get('arxiv_id','（无）')}\n"
            f"- 分类：{category}\n"
            f"- 模板：{selected_template.template_id}\n"
            f"- 是否种子：{'是' if paper.get('is_seed') else '否'}\n"
            f"- LLM 模式：{'关闭（规则模式）' if args.llm_provider == 'none' or not LLM_AVAILABLE else '开启 (' + args.llm_provider + '/' + args.llm_model + ')'}\n"
            f"- 操作类型：ingest\n\n---\n"
        )
        log_path.write_text(log_content + entry, encoding="utf-8")
        print("  [ok] log.md updated")
    else:
        print("  [skip] log.md not found")

    print(f"\nDone. Entry ingested: {paper.get('title')}")
    print(f"Source page: {page_path}")


if __name__ == "__main__":
    main()
