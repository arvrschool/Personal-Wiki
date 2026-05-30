from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse


TOPIC_ENTRIES_HEADER = "条目汇总"
TOPIC_ENTRIES_HEADER_ALIASES = (TOPIC_ENTRIES_HEADER, "论文汇总")

ENTITY_ENTRIES_HEADER = "涉及该概念的条目"
ENTITY_ENTRIES_HEADER_ALIASES = (ENTITY_ENTRIES_HEADER, "涉及该概念的论文")

ENTITY_PERSPECTIVES_HEADER = "不同条目中的观点"
ENTITY_PERSPECTIVES_HEADER_ALIASES = (ENTITY_PERSPECTIVES_HEADER, "不同论文中的观点")

RELATIONS_HEADER = "与其他条目的关联"
RELATIONS_HEADER_ALIASES = (RELATIONS_HEADER, "与其他论文的关联")

PLATFORM_LABELS = {
    "weixin": "微信公众号",
    "wechat": "微信公众号",
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


def add_entries_argument(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
    help_text: str = "Path to entries.json (compatible with legacy papers.json)",
) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--entries", dest="entries_path", help=help_text)
    group.add_argument("--papers", dest="entries_path", help=argparse.SUPPRESS)


def load_entries(entries_path: Path) -> list[dict]:
    return json.loads(entries_path.read_text(encoding="utf-8"))


def save_entries(entries: list[dict], entries_path: Path) -> None:
    entries_path.parent.mkdir(parents=True, exist_ok=True)
    entries_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def entries_file_label(entries_path: Path) -> str:
    return entries_path.name or "entries.json"


def find_first_header(content: str, header_aliases: tuple[str, ...], default: str) -> str:
    for header in header_aliases:
        if re.search(rf"^##+\s+{re.escape(header)}\s*$", content, re.MULTILINE):
            return header
    return default


def entry_time_label(entry: dict) -> str:
    for key in ("date", "published_at", "year"):
        value = entry.get(key)
        if value not in (None, "", "?"):
            return str(value)
    return "?"


def entry_type_label(entry: dict) -> str:
    template_id = (entry.get("template_id") or "").strip().lower()
    entry_type = (entry.get("entry_type") or "").strip().lower()
    source_kind = (entry.get("source_kind") or "").strip().lower()
    platform = (entry.get("platform") or "").strip().lower()

    if template_id == "research_paper" or entry_type == "paper" or entry.get("arxiv_id") or entry.get("pdf_url") or entry.get("local_pdf"):
        return "研究论文"
    if template_id == "web_article" or entry_type == "article":
        return "网页文章"
    if template_id == "generic":
        return "通用条目"
    if source_kind == "pdf":
        return "PDF 条目"
    if source_kind == "url":
        if platform:
            return PLATFORM_LABELS.get(platform, platform)
        return "网页条目"
    return "条目"


def entry_owner_label(entry: dict, max_items: int = 2) -> str:
    authors = [str(a).strip() for a in (entry.get("authors") or []) if str(a).strip()]
    if authors:
        label = ", ".join(authors[:max_items])
        if len(authors) > max_items:
            label += " et al."
        return label

    account = str(entry.get("account") or "").strip()
    author = str(entry.get("author") or "").strip()
    if account and author and account != author:
        return f"{account} · {author}"
    if account:
        return account
    if author:
        return author

    url = str(entry.get("url") or entry.get("source_url") or "").strip()
    if url:
        host = urlparse(url).netloc.lower().replace("www.", "")
        platform = str(entry.get("platform") or "").strip().lower()
        if host and platform in {"", "article", "url", "web"}:
            return host

    platform = str(entry.get("platform") or "").strip()
    if platform:
        return PLATFORM_LABELS.get(platform.lower(), platform)

    if url:
        host = urlparse(url).netloc.lower().replace("www.", "")
        if host:
            return host

    return "（未知）"


def entry_meta_badge(entry: dict) -> str:
    parts: list[str] = []
    when = entry_time_label(entry)
    if when != "?":
        parts.append(when)

    entry_kind = entry_type_label(entry)
    if entry_kind:
        parts.append(entry_kind)

    citations = entry.get("citations")
    if isinstance(citations, int) and citations > 0:
        parts.append(f"{citations:,} 引用")

    return ", ".join(parts) if parts else "条目"


def count_entry_kinds(entries: list[dict]) -> dict[str, int]:
    counts = {
        "research": 0,
        "article": 0,
        "generic": 0,
    }
    for entry in entries:
        template_id = (entry.get("template_id") or "").strip().lower()
        entry_type = (entry.get("entry_type") or "").strip().lower()
        if template_id == "research_paper" or entry_type == "paper" or entry.get("arxiv_id") or entry.get("pdf_url") or entry.get("local_pdf"):
            counts["research"] += 1
        elif template_id == "web_article" or entry_type == "article":
            counts["article"] += 1
        else:
            counts["generic"] += 1
    return counts
