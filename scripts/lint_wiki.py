"""
lint_wiki.py — Health-check a knowledge wiki for structural and semantic issues.

Two checks:
  1. Structural lint (no LLM): orphan pages, broken [[wikilinks]], stub sections,
     pages with no outlinks, frontmatter missing required fields.
  2. Semantic lint (LLM): contradictions between pages, stale claims superseded by
     newer entries, important concepts referenced in multiple entries but missing an
     entity page, source pages with empty 关联 sections that obviously should be filled.

Output: a Markdown health report saved to <wiki-dir>/lint-report.md, also printed.

Usage:
    python lint_wiki.py --wiki-dir path/to/wiki [--semantic] [--llm-provider auto|anthropic|openai|ollama]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from config_loader import cfg, get_wiki_paths
from entry_store import RELATIONS_HEADER, RELATIONS_HEADER_ALIASES, find_first_header
from llm_cli_utils import (
    anthropic_client_kwargs,
    call_llm,
    describe_provider_selection,
    openai_client_kwargs,
    resolve_model_arg,
    resolve_provider,
)
from organize_images import organize_pasted_images

TODAY = date.today().isoformat()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_wikilinks(content: str) -> list[str]:
    """Return all [[link]] targets from content."""
    return re.findall(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", content)


def _extract_title(content: str, path: Path) -> str:
    m = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return path.stem


def _is_stub(content: str) -> bool:
    return bool(re.search(r"（待补充）|待填写|TODO|placeholder", content))


def _get_md_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.md"))


def _slug(path: Path, root: Path) -> str:
    """Return path relative to root, without .md extension."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return str(rel).replace("\\", "/").removesuffix(".md")


# ---------------------------------------------------------------------------
# Structural lint
# ---------------------------------------------------------------------------

def structural_lint(wiki_dir: Path) -> list[dict]:
    """
    Check for:
    - orphan pages (no other page links to them)
    - broken [[wikilinks]] (target doesn't exist)
    - pages with no outlinks at all
    - source pages with stub sections
    - source pages with an empty relation section
    """
    paths = get_wiki_paths(wiki_dir)
    sources_dir = paths["sources"]
    entities_dir = paths["entities"]
    topics_dir = paths["topics"]
    articles_dir = paths.get("articles")

    all_md: list[Path] = []
    dirs_to_scan = [sources_dir, entities_dir, topics_dir]
    if articles_dir:
        dirs_to_scan.append(articles_dir)
        
    for d in dirs_to_scan:
        if d.exists():
            all_md += _get_md_files(d)

    # Exclude index, log, how-to docs
    excluded_names = {"index.md", "log.md", "如何维护这个知识库.md"}
    content_files = [f for f in all_md if f.name not in excluded_names]

    # Build slug → path map for link resolution (case-insensitive, space→hyphen normalized)
    slug_map: dict[str, Path] = {}

    # Also register wiki-root-level files (index.md, log.md, web-resources.md, etc.)
    for f in wiki_dir.glob("*.md"):
        slug_map[f.stem] = f
        slug_map[f.stem.lower()] = f

    for f in content_files:
        slug_map[_slug(f, wiki_dir / "wiki")] = f
        slug_map[f.stem] = f                          # bare filename match
        slug_map[f.stem.lower()] = f                  # case-insensitive match
        slug_map[f.stem.rstrip("-")] = f              # trailing-hyphen variant
        slug_map[f.stem.rstrip("-").lower()] = f
        # Parse aliases from frontmatter and register them too
        content = _read(f)
        alias_m = re.search(r"^aliases:\s*\[([^\]]*)\]", content, re.MULTILINE)
        if alias_m:
            for alias in re.findall(r'"([^"]+)"', alias_m.group(1)):
                slug_map[alias] = f
                slug_map[alias.lower()] = f

    def _resolve_link(link: str) -> str | None:
        """Return canonical slug for a wikilink target, or None if broken."""
        for key in [link, link.lower(), link.replace(" ", "-").lower(),
                    link.replace(" ", "-"), link.lower().replace(" ", "-")]:
            if key in slug_map:
                return _slug(slug_map[key], wiki_dir / "wiki")
        return None

    # Read all pages
    pages: list[dict] = []
    for f in content_files:
        content = _read(f)
        outlinks = _extract_wikilinks(content)
        pages.append({
            "path": f,
            "slug": _slug(f, wiki_dir / "wiki"),
            "content": content,
            "outlinks": outlinks,
            "title": _extract_title(content, f),
        })

    # Count inbound links
    inbound: dict[str, int] = {}
    for p in pages:
        for link in p["outlinks"]:
            resolved = _resolve_link(link)
            if resolved:
                inbound[resolved] = inbound.get(resolved, 0) + 1

    results: list[dict] = []

    for p in pages:
        rel = str(p["path"].relative_to(wiki_dir))
        slug = p["slug"]
        content = p["content"]

        # Orphan
        if inbound.get(slug, 0) == 0:
            results.append({
                "type": "orphan",
                "severity": "info",
                "page": rel,
                "detail": "没有任何页面链接到此页面（孤儿页）。",
            })

        # No outlinks
        if not p["outlinks"]:
            results.append({
                "type": "no-outlinks",
                "severity": "info",
                "page": rel,
                "detail": "此页面没有任何 [[wikilink]] 出链。",
            })

        # Broken links
        for link in p["outlinks"]:
            if _resolve_link(link) is None:
                results.append({
                    "type": "broken-link",
                    "severity": "warning",
                    "page": rel,
                    "detail": f"断链：[[{link}]] 目标页面不存在。",
                })

        # Stub sections (sources only)
        if "sources" in str(p["path"]) and _is_stub(content):
            stub_sections = re.findall(r"## (.+)\n+（待补充）", content)
            for sec in stub_sections:
                results.append({
                    "type": "stub",
                    "severity": "warning",
                    "page": rel,
                    "detail": f"Section「{sec}」仍为占位符，需要填写。",
                })

        # Empty 关联 section (sources only)
        if "sources" in str(p["path"]):
            relation_header = find_first_header(content, RELATIONS_HEADER_ALIASES, RELATIONS_HEADER)
            assoc_section = re.search(
                rf"## {re.escape(relation_header)}\s*\n+(.*?)(?=\n## |\Z)",
                content,
                re.DOTALL,
            )
            if assoc_section:
                body = assoc_section.group(1).strip()
                if not body or _is_stub(body) or len(body) < 20:
                    results.append({
                        "type": "empty-association",
                        "severity": "info",
                        "page": rel,
                        "detail": f"「{relation_header}」节为空，建议阅读后补充。",
                    })

    return results


# ---------------------------------------------------------------------------
# Semantic lint (LLM)
# ---------------------------------------------------------------------------

LINT_BLOCK_RE = re.compile(
    r"---LINT:\s*([^\n|]+?)\s*\|\s*([^\n|]+?)\s*\|\s*([^\n-]+?)\s*---\n"
    r"([\s\S]*?)---END LINT---",
    re.MULTILINE,
)


def semantic_lint(wiki_dir: Path, llm_provider: str, llm_model: str, direct_input: str | None = None) -> list[dict]:
    """
    Ask LLM to find:
    - Contradictions between pages
    - Stale claims superseded by newer entries
    - Missing entity pages (concept mentioned in 3+ entries but no dedicated page)
    - Suggestions for new sources worth adding
    """
    paths = get_wiki_paths(wiki_dir)
    sources_dir = paths["sources"]
    entities_dir = paths["entities"]

    all_files: list[Path] = []
    for d in [sources_dir, entities_dir]:
        if d.exists():
            all_files += _get_md_files(d)

    # Build compact summaries: frontmatter + first 600 chars of body
    summaries: list[str] = []
    for f in all_files[:60]:  # cap at 60 pages to stay within context
        content = _read(f)
        # Strip frontmatter from preview
        body = re.sub(r"^---\n.*?---\n", "", content, flags=re.DOTALL).strip()
        preview = body[:600] + ("..." if len(body) > 600 else "")
        rel = str(f.relative_to(wiki_dir))
        summaries.append(f"### {rel}\n{preview}")

    if not summaries:
        return []

    print(f"  [semantic] 分析 {len(summaries)} 个页面...", file=sys.stderr)

    prompt = """你是一位个人知识库的质量分析师。请审查以下 wiki 页面摘要，识别需要关注的问题。

对每个问题，请严格按照以下格式输出（不要输出其他任何内容）：

---LINT: type | severity | 简短标题---
问题描述（一段话，具体说明哪些页面有冲突/哪个概念缺失）。
PAGES: page1.md, page2.md
---END LINT---

Type 类型说明：
- contradiction：两个或多个页面对同一事实有相互矛盾的描述
- stale：某页面的内容已被更新的条目推翻或过时
- missing-page：某个重要概念在 3 个以上条目中被提及，但没有独立的 entity 页面
- suggestion：值得新增的条目或来源

Severity 级别：
- warning：应该处理
- info：建议处理

只报告真正的问题，不要捏造。如果没有问题就不输出任何内容。

## Wiki 页面摘要

""" + "\n\n".join(summaries)

    raw = call_llm(prompt, provider=llm_provider, model=llm_model, direct_input=direct_input)
    if not raw:
        return []

    results: list[dict] = []
    for m in LINT_BLOCK_RE.finditer(raw):
        raw_type = m.group(1).strip().lower()
        severity = m.group(2).strip().lower()
        title = m.group(3).strip()
        body = m.group(4).strip()

        pages_match = re.search(r"^PAGES:\s*(.+)$", body, re.MULTILINE)
        affected = [p.strip() for p in pages_match.group(1).split(",")] if pages_match else []
        detail = re.sub(r"^PAGES:.*$", "", body, flags=re.MULTILINE).strip()

        results.append({
            "type": "semantic",
            "severity": "warning" if severity == "warning" else "info",
            "page": title,
            "detail": f"[{raw_type}] {detail}",
            "affected_pages": affected,
        })

    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _severity_icon(s: str) -> str:
    return "⚠️" if s == "warning" else "ℹ️"


def build_report(structural: list[dict], semantic: list[dict], wiki_dir: Path) -> str:
    warnings = [r for r in structural + semantic if r["severity"] == "warning"]
    infos = [r for r in structural + semantic if r["severity"] == "info"]

    lines = [
        f"# Wiki 健康报告",
        f"",
        f"> 生成时间：{TODAY}  |  知识库：`{wiki_dir.name}`",
        f"",
        f"**总计**：{len(warnings)} 个警告，{len(infos)} 条提示",
        f"",
        f"---",
        f"",
    ]

    # --- Structural ---
    by_type: dict[str, list[dict]] = {}
    for r in structural:
        by_type.setdefault(r["type"], []).append(r)

    type_labels = {
        "orphan": "孤儿页（无入链）",
        "no-outlinks": "无出链页",
        "broken-link": "断链",
        "stub": "占位符未填写",
        "empty-association": "关联节为空",
        "unorganized-image": "未整理图片",
        "missing-pasted-image": "缺失图片",
    }

    lines.append("## 结构性问题\n")
    if not structural:
        lines.append("✅ 没有发现结构性问题。\n")
    else:
        for t, items in by_type.items():
            label = type_labels.get(t, t)
            lines.append(f"### {_severity_icon(items[0]['severity'])} {label}（{len(items)}）\n")
            for r in items:
                lines.append(f"- `{r['page']}`  \n  {r['detail']}")
            lines.append("")

    # --- Semantic ---
    lines.append("## 语义问题（LLM 分析）\n")
    if not semantic:
        lines.append("✅ 没有运行语义分析，或没有发现语义问题。\n")
    else:
        for r in semantic:
            icon = _severity_icon(r["severity"])
            lines.append(f"### {icon} {r['page']}\n")
            lines.append(r["detail"])
            if r.get("affected_pages"):
                lines.append(f"\n**涉及页面**：{', '.join(r['affected_pages'])}")
            lines.append("")

    # --- Summary ---
    lines += [
        "---",
        "",
        "## 建议操作顺序",
        "",
        "1. **断链**（broken-link）→ 立即修复：创建缺失页面或修正拼写",
        "2. **占位符**（stub）→ 阅读对应来源后填写",
        "3. **矛盾**（contradiction）→ 判断哪个条目更新/更可靠，修正描述",
        "4. **缺失实体页**（missing-page）→ 运行 `enrich_wiki.py --only-entities`",
        "5. **孤儿页** / **关联节为空** → 阅读来源后补充关系说明",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Health-check a knowledge wiki")
    parser.add_argument("--wiki-dir", type=Path, required=True, help="Wiki root directory")
    parser.add_argument("--semantic", action="store_true", help="Run LLM semantic analysis")
    parser.add_argument("--llm-provider", default=cfg("llm", "provider", "auto"),
                        choices=["auto", "anthropic", "openai", "ollama", "direct-inference"], help="LLM provider for semantic lint")
    parser.add_argument("--llm-model", default="", help="LLM model override")
    parser.add_argument("--direct-input", help="File path containing raw LLM output to bypass the direct-inference pause.")
    parser.add_argument("--out", type=Path, default=None, help="Output path (default: <wiki-dir>/lint-report.md)")
    parser.add_argument("--no-save", action="store_true", help="Print report but don't save")
    args = parser.parse_args()
    print(
        f"[llm] provider: {describe_provider_selection(args.llm_provider, allowed={'direct-inference', 'openai', 'anthropic', 'ollama'})}",
        file=sys.stderr,
        flush=True,
    )

    wiki_dir = args.wiki_dir.resolve()
    if not wiki_dir.exists():
        print(f"Error: wiki directory not found: {wiki_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[lint] 扫描知识库：{wiki_dir}", file=sys.stderr)

    # Report image issues, but keep lint read-only by default.
    image_issues = organize_pasted_images(wiki_dir, fix=False)

    # Structural
    print("[lint] 结构性检查...", file=sys.stderr)
    structural = image_issues + structural_lint(wiki_dir)
    warn_count = sum(1 for r in structural if r["severity"] == "warning")
    info_count = sum(1 for r in structural if r["severity"] == "info")
    print(f"  → {warn_count} 警告，{info_count} 提示", file=sys.stderr)

    # Semantic (optional)
    semantic: list[dict] = []
    if args.semantic:
        print("[lint] 正在进行语义分析 (LLM)...", file=sys.stderr)
        semantic = semantic_lint(wiki_dir, args.llm_provider, args.llm_model, args.direct_input)
        print(f"  → {len(semantic)} 条语义问题", file=sys.stderr)


    # Build report
    report = build_report(structural, semantic, wiki_dir)

    # Save
    if not args.no_save:
        out_path = args.out or (wiki_dir / "lint-report.md")
        out_path.write_text(report, encoding="utf-8")
        print(f"\n[lint] 报告已保存：{out_path}", file=sys.stderr)

    # Print to stdout
    print("\n" + report)

    # Append to log.md
    log_path = wiki_dir / "log.md"
    if log_path.exists():
        entry = (
            f"\n## {TODAY} lint | 知识库健康检查\n\n"
            f"- 结构性警告：{warn_count}\n"
            f"- 结构性提示：{info_count}\n"
            f"- 语义问题：{len(semantic)}\n"
            f"- 语义分析：{'开启' if args.semantic else '未运行'}\n"
            f"- 操作类型：lint\n\n---\n"
        )
        log_path.write_text(log_path.read_text(encoding="utf-8") + entry, encoding="utf-8")


if __name__ == "__main__":
    main()
