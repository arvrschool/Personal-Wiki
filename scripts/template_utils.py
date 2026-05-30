#!/usr/bin/env python3
"""Template discovery, selection, and lightweight rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from pathlib import Path


DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


@dataclass(frozen=True)
class TemplateSpec:
    template_id: str
    label: str
    description: str
    entry_type: str
    enrich_mode: str
    sections: dict[str, str]
    template_path: Path

    @property
    def template_text(self) -> str:
        return self.template_path.read_text(encoding="utf-8")


def get_template_dir(template_dir: str | Path | None = None) -> Path:
    if template_dir is None:
        return DEFAULT_TEMPLATE_DIR
    return Path(template_dir).expanduser().resolve()


def _load_manifest(manifest_path: Path) -> dict:
    with manifest_path.open("rb") as f:
        return tomllib.load(f)


def list_templates(template_dir: str | Path | None = None) -> list[str]:
    base_dir = get_template_dir(template_dir)
    if not base_dir.exists():
        return []
    template_ids: list[str] = []
    for child in sorted(base_dir.iterdir()):
        if child.is_dir() and (child / "manifest.toml").exists() and (child / "page.md").exists():
            template_ids.append(child.name)
    return template_ids


def load_template(template_id: str, template_dir: str | Path | None = None) -> TemplateSpec:
    base_dir = get_template_dir(template_dir)
    manifest_path = base_dir / template_id / "manifest.toml"
    template_path = base_dir / template_id / "page.md"
    if not manifest_path.exists() or not template_path.exists():
        available = ", ".join(list_templates(base_dir)) or "none"
        raise FileNotFoundError(f"Template '{template_id}' not found in {base_dir} (available: {available})")

    manifest = _load_manifest(manifest_path)
    return TemplateSpec(
        template_id=manifest.get("id", template_id),
        label=manifest.get("label", template_id),
        description=manifest.get("description", ""),
        entry_type=manifest.get("entry_type", "generic"),
        enrich_mode=manifest.get("enrich_mode", "none"),
        sections={k: v for k, v in (manifest.get("sections") or {}).items()},
        template_path=template_path,
    )


def auto_select_template(item: dict | None) -> str:
    item = item or {}
    entry_type = (item.get("entry_type") or "").lower()
    source_kind = (item.get("source_kind") or "").lower()
    platform = (item.get("platform") or "").lower()
    url = (item.get("url") or item.get("source_url") or "").lower()
    excerpt = " ".join(
        str(item.get(key) or "")
        for key in ("content_excerpt", "abstract", "summary", "content")
    ).strip()
    has_article_like_content = len(excerpt) >= 240 or excerpt.count(" ") >= 40
    generic_url = (
        platform.startswith("github")
        or "github.com" in url
        or "youtu" in url
        or platform in {"youtube", "twitter", "x", "notion"}
    )
    article_url = (
        platform in {"wechat", "weixin", "zhihu", "medium", "substack", "newsletter", "blog", "article", "web", "site", "news"}
        or any(domain in url for domain in ("mp.weixin.qq.com", "zhihu.com", "medium.com", "substack.com"))
    )

    if entry_type == "article":
        if generic_url:
            return "generic"
        return "web_article"

    if (
        entry_type == "paper"
        or item.get("arxiv_id")
        or item.get("pdf_url")
        or source_kind in {"arxiv", "pdf", "paper"}
        or "arxiv.org" in url
        or "openreview.net" in url
    ):
        return "research_paper"

    if source_kind == "url":
        if generic_url:
            return "generic"
        if has_article_like_content or article_url:
            return "web_article"
        return "generic"

    return "generic"


def resolve_template(
    requested: str = "auto",
    item: dict | None = None,
    template_dir: str | Path | None = None,
) -> TemplateSpec:
    template_id = auto_select_template(item) if requested in {"", "auto", None} else requested
    return load_template(template_id, template_dir=template_dir)


def render_template_text(template_text: str, context: dict[str, object]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = context.get(key, "")
        return "" if value is None else str(value)

    return re.sub(r"{{\s*([^{}]+?)\s*}}", repl, template_text)


def render_template(
    template_id: str,
    context: dict[str, object],
    template_dir: str | Path | None = None,
) -> str:
    spec = load_template(template_id, template_dir=template_dir)
    return render_template_text(spec.template_text, context)


def extract_frontmatter_value(content: str, key: str) -> str | None:
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", parts[1], flags=re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip().strip('"').strip("'")
    return value or None


def yaml_string(value: str) -> str:
    return json.dumps(value or "", ensure_ascii=False)


def yaml_array(items: list[str]) -> str:
    return json.dumps(items or [], ensure_ascii=False)


def yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def scalar_or_null(value: object) -> str:
    if value in (None, "", []):
        return "null"
    return str(value)


def markdown_bullets(items: list[str], fallback: str = "（暂无）") -> str:
    cleaned = [item for item in items if item]
    if not cleaned:
        return fallback
    return "\n".join(f"- {item}" for item in cleaned)


def markdown_links(urls: list[str], fallback: str = "（暂无）") -> str:
    cleaned = [url for url in urls if url]
    if not cleaned:
        return fallback
    return "\n".join(f"- [{url}]({url})" for url in cleaned)
