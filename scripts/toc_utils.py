"""
Markdown Table-of-Contents utilities for wiki source pages.

Public API
----------
inject_toc(content)  -> str   Insert TOC if not yet present; no-op otherwise.
update_toc(content)  -> str   Remove existing TOC and regenerate from current headings.
                               Use after enrichment, which may add new sections.
"""

import re

# ── anchor generation ──────────────────────────────────────────────────────────

def _make_anchor(heading: str) -> str:
    """Convert a heading string to a GitHub/Obsidian compatible anchor fragment."""
    # Strip inline markdown (bold, italic, code, links)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', heading)   # [label](url) → label
    text = re.sub(r'[`*_]', '', text)
    # Remove emoji and other non-word, non-CJK chars (keep alphanumeric, CJK, spaces, hyphens)
    text = re.sub(r'[^\w\s一-鿿぀-ゟ゠-ヿ-]', ' ', text)
    text = text.strip().lower()
    # Collapse whitespace → single hyphen
    text = re.sub(r'\s+', '-', text)
    text = text.strip('-')
    return text


# ── TOC builder ────────────────────────────────────────────────────────────────

_TOC_HEADER = "## 目录"

def _build_toc_block(content: str) -> str:
    """
    Scan content for ## and ### headings (skipping frontmatter and the TOC
    section itself) and return the full `## 目录` markdown block.
    """
    lines = content.splitlines()
    in_frontmatter = False
    in_toc = False
    entries: list[str] = []

    for i, line in enumerate(lines):
        if i == 0 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
            continue

        # Skip the TOC section itself
        if line.strip() == _TOC_HEADER:
            in_toc = True
            continue
        if in_toc:
            if re.match(r"^## ", line) and line.strip() != _TOC_HEADER:
                in_toc = False
            else:
                continue

        m2 = re.match(r"^## (.+)", line)
        m3 = re.match(r"^### (.+)", line)
        if m2:
            title = m2.group(1).strip()
            anchor = _make_anchor(title)
            entries.append(f"- [{title}](#{anchor})")
        elif m3:
            title = m3.group(1).strip()
            anchor = _make_anchor(title)
            entries.append(f"  - [{title}](#{anchor})")

    if not entries:
        return ""
    return _TOC_HEADER + "\n\n" + "\n".join(entries) + "\n"


# ── public helpers ─────────────────────────────────────────────────────────────

def _has_toc(content: str) -> bool:
    return bool(re.search(r"^## 目录\s*$", content, re.MULTILINE))


def _first_h2_pos(content: str) -> int | None:
    """Return the character offset of the first ## heading after frontmatter."""
    in_fm = False
    for m in re.finditer(r"^(---\s*$|## .+)", content, re.MULTILINE):
        if m.group().strip() == "---":
            in_fm = not in_fm
            continue
        if not in_fm and m.group().startswith("## "):
            return m.start()
    return None


def inject_toc(content: str) -> str:
    """Insert TOC before the first ## heading. No-op if TOC already present."""
    if _has_toc(content):
        return content
    toc = _build_toc_block(content)
    if not toc:
        return content
    pos = _first_h2_pos(content)
    if pos is None:
        return content
    return content[:pos] + toc + "\n" + content[pos:]


def update_toc(content: str) -> str:
    """
    Regenerate the TOC section to reflect the current heading structure.
    If no TOC exists yet, insert one (same as inject_toc).
    """
    toc = _build_toc_block(content)
    if not toc:
        return content

    if _has_toc(content):
        # Replace old TOC block
        content = re.sub(
            r"## 目录\n.*?(?=\n## (?!目录)|\Z)",
            toc,
            content,
            count=1,
            flags=re.DOTALL,
        )
        # Ensure a blank line follows the TOC
        content = re.sub(r"(## 目录\n.*?\n)(\n*)(## (?!目录))", r"\1\n\3",
                         content, count=1, flags=re.DOTALL)
        return content
    else:
        return inject_toc(content)
