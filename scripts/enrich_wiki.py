#!/usr/bin/env python3
"""
Enrich an existing llm-wiki knowledge base by filling in stub content.

For each source page with empty 核心观点, reads the available text (abstract
from entries.json / papers.json, or full PDF text if available) and fills in:
  - 核心观点  (3-5 bullet points per paper)
  - 与其他论文的关联 (relations to other papers in the wiki)

For each entity page, aggregates observations from all linked source pages into:
  - 简介
  - 在当前知识库中的角色
  - 不同论文中的观点

For each topic page, fills in:
  - 核心贡献 column in the 论文汇总 table
  - 核心观点 (cross-paper synthesis)
  - 研究脉络

Usage:
    # Enrich using abstract text only (no PDF needed, no LLM required)
    python enrich_wiki.py --wiki-dir /path/to/wiki --entries /path/to/entries.json

    # Auto-download missing PDFs then enrich with full text
    python enrich_wiki.py --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --pdf-dir /path/to/pdfs --download-pdfs

    # Point to already-downloaded PDFs (no auto-download)
    python enrich_wiki.py --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --pdf-dir /path/to/pdfs

    # Enrich only source pages (skip entity/topic synthesis)
    python enrich_wiki.py --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --only-sources

    # Force re-enrich already-filled pages
    python enrich_wiki.py --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --force

    # [FUTURE] Use LLM API to generate richer content
    python enrich_wiki.py --wiki-dir /path/to/wiki --entries /path/to/entries.json \\
        --llm-provider anthropic --llm-model claude-opus-4-7
"""

import argparse
import base64
import difflib
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from entry_store import (
    ENTITY_PERSPECTIVES_HEADER,
    ENTITY_PERSPECTIVES_HEADER_ALIASES,
    RELATIONS_HEADER,
    RELATIONS_HEADER_ALIASES,
    add_entries_argument,
    entries_file_label,
    find_first_header,
    load_entries,
    save_entries,
)
from git_utils import git_commit_paths
from llm_cli_utils import (
    anthropic_client_kwargs,
    anthropic_http_api_key,
    call_llm,
    describe_provider_selection,
    openai_client_kwargs,
    resolve_model_arg,
    resolve_provider,
)
from organize_images import organize_pasted_images
from template_utils import extract_frontmatter_value, load_template, markdown_bullets, resolve_template

def _remove_missing_images(content: str, page_path: Path) -> str:
    """Find ![...](path) in content. If path does not exist, remove the image line and the immediately following blockquote caption if present."""
    lines = content.split('\n')
    out_lines = []
    import re
    import sys
    
    skip_next = False
    for i, line in enumerate(lines):
        if skip_next:
            if line.strip().startswith('>'):
                continue
            skip_next = False
            
        m = re.search(r'!\[.*?\]\((.*?)\)', line)
        if m:
            img_ref = m.group(1)
            if img_ref.startswith('http'):
                out_lines.append(line)
                continue
                
            try:
                img_path = (page_path.parent / img_ref).resolve()
                if not img_path.exists():
                    found = False
                    for ext in ['.png', '.jpeg', '.jpg', '.webp']:
                        alt_path = img_path.with_suffix(ext)
                        if alt_path.exists():
                            new_ref = img_ref.rsplit('.', 1)[0] + ext
                            line = line.replace(img_ref, new_ref)
                            out_lines.append(line)
                            found = True
                            break
                    if not found:
                        skip_next = True
                        print(f"  [fix] Removed missing image link: {img_ref}", file=sys.stderr)
                        continue
            except Exception:
                pass
        out_lines.append(line)
        
    return '\n'.join(out_lines)


from config_loader import cfg
from toc_utils import update_toc

TODAY = date.today().isoformat()

# Lazy imports from sibling scripts
def _import_figure_fetcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fetch_paper_figures",
        Path(__file__).parent / "fetch_paper_figures.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_media_fetcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fetch_paper_media",
        Path(__file__).parent / "fetch_paper_media.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_web_searcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "search_web_resources",
        Path(__file__).parent / "search_web_resources.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_web_search_queries(paper: dict, provider: str = "auto", model: str = "") -> tuple[str, str]:
    """
    Ask LLM to generate two search queries based on paper content:
    - query_specific: for finding blog posts/discussions about this exact paper
    - query_topic: for finding related tutorials/videos on the paper's core ideas

    Returns (query_specific, query_topic). Falls back to title-based queries on failure.
    """
    title = paper.get("title") or ""
    arxiv_id = paper.get("arxiv_id") or ""
    abstract = (paper.get("abstract") or "")[:600]
    authors = (paper.get("authors") or [])[:2]
    first_author = authors[0].split()[-1] if authors else ""

    prompt = f"""你是一位研究员，需要为一篇论文搜索相关的博客文章和讲解视频。

论文信息：
标题：{title}
arXiv ID：{arxiv_id}
摘要：{abstract}

请生成两个搜索查询：
1. query_specific：用于找到**专门介绍或引用这篇论文**的博客/视频（包含 arxiv ID 或论文名）
2. query_topic：用于找到**介绍这篇论文核心技术方向**的教程/讲解（不依赖论文名，而是描述其核心问题和方法）

要求：
- 每个查询不超过 10 个单词
- query_topic 应该描述这篇论文真正在解决的问题（如「robot foundation model latent dynamics policy learning」），而不是论文标题的缩写
- 不要生成可能匹配无关同名概念的查询（例如「LDA」会匹配 Latent Dirichlet Allocation）

严格按如下 JSON 格式输出，不要输出其他内容：
{{"query_specific": "...", "query_topic": "..."}}"""

    try:
        raw = call_llm(prompt, provider=provider, model=model)
        m = re.search(r'\{[^}]+\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
            qs = result.get("query_specific", "").strip()
            qt = result.get("query_topic", "").strip()
            if qs and qt:
                return qs, qt
    except Exception:
        pass

    # Fallback: arxiv-anchored query + title keywords
    if arxiv_id:
        return f"arxiv {arxiv_id} {title[:50]}", f"{title[:60]} {first_author}".strip()
    return f"{title[:80]} {first_author}".strip(), title[:80]


def fetch_web_resources_for_paper(
    paper: dict,
    provider: str = "auto",
    model: str = "",
) -> str:
    """
    Search the web for blog posts, YouTube videos, and GitHub projects related to
    a specific paper. Returns a Markdown string for the ## 互联网资源 section,
    or empty string if nothing found.

    Uses arxiv-ID-anchored query to avoid disambiguation (e.g. "LDA" matching
    Latent Dirichlet Allocation). Relevance filter requires arxiv_id match OR
    ≥2 title keyword matches.
    """
    title = paper.get("title") or ""
    arxiv_id = paper.get("arxiv_id") or ""

    # Arxiv-anchored query prevents disambiguation on ambiguous acronyms
    if arxiv_id:
        query = f"arxiv {arxiv_id} {title[:60]}"
    else:
        query = title[:80]

    # Title keywords for relevance filtering
    stopwords = {"the", "a", "an", "of", "in", "for", "and", "or", "via", "with",
                 "from", "to", "on", "by", "is", "are", "its", "this", "that"}
    title_keywords = {
        w.lower().strip(":.,-") for w in title.split()
        if len(w) > 3 and w.lower() not in stopwords
    }
    if arxiv_id:
        title_keywords.add(arxiv_id.lower())

    def _is_relevant(item: dict) -> bool:
        text = " ".join([
            item.get("title") or "",
            item.get("description") or "",
            item.get("snippet") or "",
            item.get("channel") or "",
            item.get("url") or "",
        ]).lower()
        if arxiv_id and arxiv_id.lower() in text:
            return True
        matches = sum(1 for kw in title_keywords if kw in text)
        return matches >= 2

    try:
        sw = _import_web_searcher()
        articles_raw = sw.search_articles(query, max_results=cfg("web_resources", "max_articles", 6))
        videos_raw = sw.search_youtube_videos(query, max_results=cfg("web_resources", "max_videos", 6))
        github = sw.search_github_projects(
            f"{title[:60]} {arxiv_id}", max_results=cfg("web_resources", "max_github", 2)
        ) if arxiv_id else []

        articles = [a for a in articles_raw if _is_relevant(a)][:3]
        videos = [v for v in videos_raw if _is_relevant(v)][:3]
    except Exception as e:
        print(f"  [warn] web resource search failed: {e}", file=sys.stderr)
        return ""

    if not articles and not videos and not github:
        return ""

    lines = ["## 互联网资源\n"]

    if articles:
        lines.append("### 📝 相关博客 / 文章\n")
        source_icons = {
            "medium": "📖 Medium", "towardsdatascience": "📖 TDS",
            "huggingface": "🤗 HuggingFace", "blog": "📖",
            "weixin": "💬 微信", "zhihu": "💬 知乎",
            "general_en": "🌐", "twitter": "🐦",
        }
        for a in articles:
            src = source_icons.get(a.get("source", ""), "🌐")
            lines.append(f"- {src} [{a['title']}]({a['url']})")
            if a.get("snippet"):
                lines.append(f"  > {a['snippet'][:120]}")
        lines.append("")

    if videos:
        lines.append("### 🎥 相关视频\n")
        for v in videos:
            meta_parts = []
            if v.get("channel"):
                meta_parts.append(v["channel"])
            if v.get("duration"):
                meta_parts.append(v["duration"])
            if v.get("views"):
                meta_parts.append(v["views"])
            meta = " · ".join(meta_parts)
            lines.append(f"- [{v['title']}]({v['url']})")
            if meta:
                lines.append(f"  `{meta}`")
        lines.append("")

    if github:
        lines.append("### 💻 相关代码\n")
        for g in github:
            stars = f" ⭐{g['stars']:,}" if g.get("stars") else ""
            lines.append(f"- [{g['name']}]({g['url']}){stars}")
            if g.get("description"):
                lines.append(f"  > {g['description'][:100]}")
        lines.append("")

    return "\n".join(lines)


LLM_AVAILABLE = True


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def download_pdf(arxiv_id: str, out_path: Path, timeout: int = cfg("pdf", "download_timeout", 120)) -> bool:
    """Download a PDF from arXiv. Tries urllib then curl. Returns True on success."""
    for scheme in ["https", "http"]:
        url = f"{scheme}://arxiv.org/pdf/{arxiv_id}"
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
                data = r.read()
            if len(data) > 10240:
                out_path.write_bytes(data)
                return True
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["curl", "-L", "--max-time", str(timeout), "-o", str(out_path),
                 "--user-agent", _HEADERS["User-Agent"], "-k", url],
                capture_output=True, timeout=timeout + 10,
            )
            if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 10240:
                return True
        except Exception:
            pass
    return False


def download_pdf_from_url(url: str, out_path: Path, timeout: int = 60) -> bool:
    """Download a PDF from any direct URL. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            data = r.read()
        if len(data) > 10240 and data[:4] == b"%PDF":
            out_path.write_bytes(data)
            return True
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["curl", "-L", "--max-time", str(timeout), "-o", str(out_path),
             "--user-agent", _HEADERS["User-Agent"], "-k", url],
            capture_output=True, timeout=timeout + 10,
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 10240:
            # Verify it's actually a PDF
            with open(out_path, "rb") as f:
                if f.read(4) == b"%PDF":
                    return True
            out_path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def search_pdf_url(title: str, authors: list[str], year: int | None = None) -> str:
    """
    Search for a PDF URL of a non-arXiv paper.
    Returns the best PDF URL found, or empty string if none found.

    Search strategy (in order):
    1. Bing web search (returns 302 but follows redirects)
    2. Google Scholar search
    3. Venue-specific lookups: OpenReview API, ACL Anthology, NeurIPS proceedings
    """
    author_str = authors[0].split()[-1] if authors else ""

    # --- Strategy 1: Bing ---
    for query in [f'"{title}" pdf', f'{title} {author_str} pdf filetype:pdf']:
        encoded = urllib.parse.quote_plus(query)
        try:
            req = urllib.request.Request(
                f"https://www.bing.com/search?q={encoded}&count=10",
                headers={**_HEADERS, "Accept": "text/html,application/xhtml+xml"},
            )
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as r:
                html = r.read().decode("utf-8", errors="replace")
            found = _extract_pdf_urls_from_html(html)
            for url in found:
                if _is_valid_pdf_url(url, title):
                    return url
        except Exception:
            pass

    # --- Strategy 2: Google Scholar ---
    encoded = urllib.parse.quote_plus(f'"{title}"')
    try:
        req = urllib.request.Request(
            f"https://scholar.google.com/scholar?q={encoded}",
            headers={**_HEADERS, "Accept": "text/html"},
        )
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
        found = _extract_pdf_urls_from_html(html)
        for url in found:
            if _is_valid_pdf_url(url, title):
                return url
    except Exception:
        pass

    # --- Strategy 3: OpenReview API (for ICLR/NeurIPS papers) ---
    try:
        encoded_title = urllib.parse.quote_plus(title[:60])
        req = urllib.request.Request(
            f"https://api2.openreview.net/notes/search?term={encoded_title}&limit=5",
            headers=_HEADERS,
        )
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as r:
            data = json.loads(r.read())
        for note in data.get("notes", []):
            c = note.get("content", {})
            note_title = c.get("title", "")
            if isinstance(note_title, dict):
                note_title = note_title.get("value", "")
            if not note_title or title[:30].lower() not in note_title.lower():
                continue
            pdf = c.get("pdf", "")
            if isinstance(pdf, dict):
                pdf = pdf.get("value", "")
            if pdf:
                pdf_url = pdf if pdf.startswith("http") else f"https://openreview.net{pdf}"
                if _is_valid_pdf_url(pdf_url, title):
                    return pdf_url
            # Try forum ID → PDF URL
            forum_id = note.get("forum") or note.get("id")
            if forum_id:
                candidate = f"https://openreview.net/pdf?id={forum_id}"
                if _is_valid_pdf_url(candidate, title):
                    return candidate
    except Exception:
        pass

    # --- Strategy 4: ACL Anthology (for NLP/CL papers) ---
    try:
        slug = re.sub(r"[^\w\s]", "", title.lower())
        slug = re.sub(r"\s+", "+", slug.strip())[:60]
        req = urllib.request.Request(
            f"https://aclanthology.org/search/?q={urllib.parse.quote_plus(title[:50])}",
            headers=_HEADERS,
        )
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
        # ACL uses paper IDs like YYYY.venue-track.N → PDF at /YYYY.venue-track.N.pdf
        acl_ids = re.findall(r'/(\d{4}\.[a-z]+-[a-z]+\.\d+)\.pdf', html)
        for acl_id in acl_ids[:3]:
            url = f"https://aclanthology.org/{acl_id}.pdf"
            if _is_valid_pdf_url(url, title):
                return url
    except Exception:
        pass

    return ""


def _extract_pdf_urls_from_html(html: str) -> list[str]:
    """Extract candidate PDF URLs from a search result page."""
    urls = []
    # Direct .pdf links
    urls += re.findall(r'https?://[^\s\'"<>]+\.pdf(?:\?[^\s\'"<>]*)?', html, re.I)
    # OpenReview PDF links (may not end in .pdf)
    for forum_id in re.findall(r'openreview\.net/forum\?id=([A-Za-z0-9_-]+)', html):
        urls.append(f"https://openreview.net/pdf?id={forum_id}")
    urls += re.findall(r'openreview\.net/pdf\?id=[A-Za-z0-9_-]+', html)
    # NeurIPS proceedings
    urls += re.findall(r'https?://proceedings\.neurips\.cc/[^\s\'"<>]+\.pdf', html, re.I)
    # Clean up and deduplicate
    seen = set()
    result = []
    for u in urls:
        u = u.rstrip(".,;)\"'")
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _is_valid_pdf_url(url: str, title: str) -> bool:
    """Quick HEAD check to confirm URL returns a PDF."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers=_HEADERS)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=8) as r:
            content_type = r.headers.get("Content-Type", "")
            content_length = int(r.headers.get("Content-Length", 0))
            if "pdf" in content_type.lower() and content_length > 10240:
                return True
            # Some servers return application/octet-stream for PDFs
            if content_length > 50000 and "html" not in content_type.lower():
                return True
    except Exception:
        pass
    # Fall back to GET with small read to check magic bytes
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as r:
            first_bytes = r.read(8)
            return first_bytes[:4] == b"%PDF"
    except Exception:
        return False


def _non_arxiv_pdf_path(paper: dict, pdf_dir: Path) -> Path | None:
    """
    Find or determine the local PDF path for a non-arXiv paper.
    Checks: explicit pdf_path field, title-slug filename, any matching .pdf in pdf_dir.
    """
    # Explicit field set during download
    if paper.get("local_pdf"):
        p = Path(paper["local_pdf"])
        if p.exists():
            return p

    # Title-slug filename
    title = paper.get("title") or ""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:50]
    for candidate in pdf_dir.glob("*.pdf"):
        if slug[:20] in candidate.stem.lower():
            return candidate

    return None


def batch_download_pdfs(papers: list[dict], pdf_dir: Path, delay: float = cfg("pdf", "download_delay", 1.5),
                         search_non_arxiv: bool = True) -> dict[str, bool]:
    """
    Download PDFs for all papers that don't have a local PDF yet.
    - arXiv papers: download directly from arxiv.org/pdf/<id>
    - Non-arXiv papers (no arxiv_id): search Bing/DDG for PDF URL first

    Returns {paper_title_slug: success}.
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    # arXiv papers
    arxiv_papers = [
        p for p in papers
        if p.get("arxiv_id") and not (pdf_dir / f"{p['arxiv_id']}.pdf").exists()
    ]
    # Non-arXiv papers (have a URL but no arxiv_id)
    non_arxiv_papers = [
        p for p in papers
        if not p.get("arxiv_id") and (p.get("url") or p.get("pdf_url"))
        and _non_arxiv_pdf_path(p, pdf_dir) is None
    ] if search_non_arxiv else []

    total = len(arxiv_papers) + len(non_arxiv_papers)
    already = len(papers) - total
    print(f"[download] {len(arxiv_papers)} arXiv + {len(non_arxiv_papers)} non-arXiv PDFs to download "
          f"({already} already present)")

    # --- Download arXiv papers ---
    for i, paper in enumerate(arxiv_papers, 1):
        arxiv_id = paper["arxiv_id"]
        out_path = pdf_dir / f"{arxiv_id}.pdf"
        print(f"  [{i}/{total}] {arxiv_id} — {paper.get('title','')[:50]}", end=" ... ", flush=True)
        ok = download_pdf(arxiv_id, out_path)
        results[arxiv_id] = ok
        print("ok" if ok else "FAILED")
        if ok:
            time.sleep(delay)

    # --- Download non-arXiv papers via web search ---
    for i, paper in enumerate(non_arxiv_papers, len(arxiv_papers) + 1):
        title = paper.get("title") or ""
        slug = re.sub(r"[^\w\s-]", "", title.lower())
        slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:50]
        out_path = pdf_dir / f"{slug}.pdf"
        print(f"  [{i}/{total}] (non-arXiv) {title[:50]}", end=" ... ", flush=True)

        # 1. Try explicit pdf_url first
        pdf_url = paper.get("pdf_url") or ""
        ok = False
        if pdf_url:
            ok = download_pdf_from_url(pdf_url, out_path)
            if ok:
                print(f"ok (pdf_url)")

        # 2. Web search for PDF URL
        if not ok:
            print("searching...", end=" ", flush=True)
            found_url = search_pdf_url(title, paper.get("authors") or [], paper.get("year"))
            if found_url:
                ok = download_pdf_from_url(found_url, out_path)
                if ok:
                    paper["pdf_url"] = found_url  # cache for future runs
                    print(f"ok ({found_url[:60]})")
                else:
                    print(f"FAILED (url={found_url[:50]})")
            else:
                print("FAILED (no url found)")

        results[slug] = ok
        if ok:
            paper["local_pdf"] = str(out_path)
            time.sleep(delay)

    ok_count = sum(results.values())
    print(f"[download] Done: {ok_count}/{total} succeeded")
    return results


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_path: Path, max_chars: int = cfg("pdf", "max_text_chars", 40000)) -> str:
    """
    Extract full paper text from a PDF, stripping References section.
    Returns structured sections where detectable, otherwise raw text.
    max_chars=40000 covers ~20 pages comfortably for LLM context.

    Tries in order: txt cache → pdftotext (poppler) → pdfminer → pdfplumber → pymupdf
    Parsed text is saved as <pdf_path>.txt to skip re-parsing on subsequent runs.
    """
    text = ""

    # Check txt cache first — avoids re-parsing large PDFs on every enrichment run.
    txt_path = pdf_path.with_suffix(".txt")
    if txt_path.exists():
        try:
            return txt_path.read_text(encoding="utf-8")[:max_chars].strip()
        except Exception:
            pass

    # pdftotext (poppler) — fastest, no Python dep, available on most Linux.
    # Use default flow mode (no -layout) so multi-column papers produce clean sentences.
    if not text:
        try:
            result = subprocess.run(
                ["pdftotext", str(pdf_path), "-"],
                capture_output=True, text=True, timeout=cfg("pdf", "pdftotext_timeout", 30),
            )
            if result.returncode == 0 and result.stdout.strip():
                text = result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not text:
        try:
            import pdfminer.high_level as _pdfminer
            text = _pdfminer.extract_text(str(pdf_path))
        except ImportError:
            pass

    if not text:
        try:
            import pdfplumber
            pages_text = []
            with pdfplumber.open(str(pdf_path)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        pages_text.append(t)
            text = "\n".join(pages_text)
        except ImportError:
            pass

    if not text:
        try:
            import fitz  # pymupdf
            doc = fitz.open(str(pdf_path))
            text = "\n".join(page.get_text() for page in doc)
        except ImportError:
            pass

    if not text:
        return ""

    # Strip References / Bibliography and everything after
    for marker in ["\nReferences\n", "\nBibliography\n", "\nREFERENCES\n",
                   "\nACKNOWLEDGMENTS\n", "\nAcknowledgments\n"]:
        idx = text.find(marker)
        if idx > len(text) * 0.4:  # only truncate if References is in latter half
            text = text[:idx]
            break

    # Clean up noise: remove lines that are just page numbers, short fragments
    lines = text.splitlines()
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            clean_lines.append("")
            continue
        if re.match(r"^\d+$", stripped):  # bare page numbers
            continue
        if len(stripped) < 4:
            continue
        clean_lines.append(stripped)

    # Merge soft-wrapped lines: a line ending with a hyphen or lowercase letter
    # that is followed by a non-blank line → join with a space (or no space for hyphen)
    merged = []
    i = 0
    while i < len(clean_lines):
        line = clean_lines[i]
        if not line:
            merged.append("")
            i += 1
            continue
        # Peek ahead: merge if current line ends mid-word (hyphen) or lowercase
        while (i + 1 < len(clean_lines) and clean_lines[i + 1]
               and (line.endswith("-") or (line[-1].islower() and not line.endswith(".")))):
            next_line = clean_lines[i + 1]
            if line.endswith("-"):
                line = line[:-1] + next_line  # remove hyphen, join directly
            else:
                line = line + " " + next_line
            i += 1
        merged.append(line)
        i += 1

    # Collapse runs of 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(merged))

    # Save txt cache so subsequent runs skip re-parsing.
    try:
        txt_path.write_text(text, encoding="utf-8")
    except Exception:
        pass

    return text[:max_chars].strip()


# ---------------------------------------------------------------------------
# Project URL content fetch
# ---------------------------------------------------------------------------

def fetch_url_text(url: str, max_chars: int = cfg("pdf", "url_fetch_max_chars", 8000)) -> str:
    """Fetch plain text from a URL (GitHub README or project page)."""
    try:
        # GitHub repo → fetch raw README
        m = re.match(r"https?://github\.com/([^/]+)/([^/\s?#]+)", url)
        if m:
            owner, repo = m.group(1), m.group(2)
            for branch in ["main", "master"]:
                for fname in ["README.md", "README.MD", "readme.md"]:
                    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fname}"
                    try:
                        req = urllib.request.Request(raw, headers=_HEADERS)
                        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as r:
                            text = r.read().decode("utf-8", errors="replace")
                        if len(text) > 100:
                            return f"[GitHub README: {url}]\n\n{text[:max_chars]}"
                    except Exception:
                        pass

        # Generic webpage → strip HTML tags
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            html = r.read().decode(charset, errors="replace")
        # Remove scripts/styles, collapse tags to spaces
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return f"[Web page: {url}]\n\n{text[:max_chars]}"
    except Exception as e:
        return f"[fetch failed: {url} — {e}]"


def fetch_project_content(paper: dict) -> str:
    """
    Fetch text content from all project_urls in a paper dict.
    Returns a combined string ready to include in an LLM prompt.
    """
    urls = paper.get("project_urls") or []
    if isinstance(urls, dict):
        urls = list(urls.values())
    if not urls:
        return ""
    parts = []
    for url in urls[:3]:  # cap at 3 URLs
        text = fetch_url_text(url)
        if text and not text.startswith("[fetch failed"):
            parts.append(text)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# PDF figure caption extraction
# ---------------------------------------------------------------------------

def extract_figure_captions(pdf_text: str) -> str:
    """
    Extract Figure/Table captions from full paper text.
    These describe the architecture diagrams and result plots — useful context for LLM.
    """
    lines = pdf_text.splitlines()
    captions = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Match "Figure N:" or "Fig. N:" or "Table N:" patterns
        if re.match(r"^(Figure|Fig\.|Table)\s+\d+[\.:]\s*\S", line, re.I):
            # Collect the caption (may span several lines until blank or next Figure)
            caption_lines = [line]
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if not next_line or re.match(r"^(Figure|Fig\.|Table)\s+\d+", next_line, re.I):
                    break
                caption_lines.append(next_line)
                i += 1
            captions.append(" ".join(caption_lines))
        else:
            i += 1
    if not captions:
        return ""
    return "## 图表说明\n\n" + "\n\n".join(f"- {c}" for c in captions[:15])


def extract_formula_candidates(pdf_text: str) -> str:
    """Extract equation-like lines from PDF text as extra LLM context for formula recovery."""
    formulas: list[str] = []
    seen: set[str] = set()

    # Pattern 1: LaTeX-style formulas (math mode, Greek letters, special notation)
    latex_pattern = re.compile(
        r'(?:\\begin\{align\*\}|\\begin\{equation\*\}|\\begin\{aligned\}|\\begin\{eqnarray\*\}'
        r'|\\mathcal|\\mathbf|\\mathrm|\\mathbb|\\boldsymbol|\\mathsf|\\mathit'
        r'|\\times|\\sum|\\prod|\\int|\\partial|\\nabla|\\infty|\\propto'
        r'|\\argmin|\\argmax|\\mathbb{E}|\\text{|\\)'
    )

    # Pattern 2: Common ML formula operators
    math_ops_pattern = re.compile(
        r'(?:log\s*\(|exp\s*\(|softmax|sigmoid|tanh|ReLU|LayerNorm|BatchNorm'
        r'|\\sigma|\\phi|\\theta|\\lambda|\\alpha|\\beta|\\gamma|\\epsilon'
        r'|\\mu|\\Sigma|\\mathcal|\\hat|\\tilde|\\bar)'
    )

    # Pattern 3: Named loss/common ML quantities
    named_terms_pattern = re.compile(
        r'(?:argmin|argmax|s\.t\.|subject\s+to|minimize|maximize'
        r'|loss\s*=|objective\s*=|KL\s*divergence|entropy'
        r'|cross.entropy|MSE|RMSE|MAE|NLL|ELBO|BCE|InfoNCE'
        r'|regularization|weight\s*decay|learning\s*rate'
        r'|temperature|momentum|dropout\s*rate'
        r'|logit|softmax|log.softmax|temperature\s*parameter)',
        re.I,
    )

    # Pattern 4: Lines with strong math syntax indicators
    math_syntax_pattern = re.compile(
        r'(?:[_^{}\\])'  # LaTeX special chars
    )

    for raw_line in pdf_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if len(line) < 6 or len(line) > 350:
            continue

        score = 0

        if latex_pattern.search(line):
            score += 3

        if math_ops_pattern.search(line):
            score += 2

        if named_terms_pattern.search(line):
            score += 2

        if math_syntax_pattern.search(line):
            score += 1

        if re.search(r"[A-Za-z]\s*=", line):
            score += 1

        if re.search(r"[∑∏∫]", line):
            score += 2

        paren_count = line.count("(") + line.count(")")
        if paren_count >= 2 and len(line) > 15:
            score += 1

        if score < 2:
            continue

        normalized = line.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        formulas.append(line)
        if len(formulas) >= cfg("wiki", "max_formulas", 25):
            break

    if not formulas:
        return ""

    # Categorize formulas
    categorized_lines = ["## 核心公式候选\n"]
    categorized_lines.append("（以下是从 PDF 文本中提取的可能公式，供 LLM 恢复完整数学表达式时参考）\n")

    loss_lines = []
    defn_lines = []
    other_lines = []

    for f in formulas:
        lower = f.lower()
        if any(kw in lower for kw in ('loss', 'objective', 'minimize', 'maximize', 'argmin', 'argmax')):
            loss_lines.append(f"- `{f}`")
        elif any(kw in lower for kw in ('where', 'denote', 'let', 'define', 'given')):
            defn_lines.append(f"- `{f}`")
        else:
            other_lines.append(f"- `{f}`")

    if loss_lines:
        categorized_lines.append("\n**可能是损失函数或优化目标：**")
        categorized_lines.extend(loss_lines)
    if defn_lines:
        categorized_lines.append("\n**可能是定义/符号说明：**")
        categorized_lines.extend(defn_lines)
    if other_lines:
        categorized_lines.append("\n**其他数学表达式：**")
        categorized_lines.extend(other_lines)

    return "\n".join(categorized_lines)


def extract_structured_sections(text: str) -> dict[str, str]:
    """
    Split full paper text into named sections.
    Returns a dict. Falls back to splitting by rough position if headers not found.
    """
    section_patterns = [
        ("introduction",  r"\n(?:1[\s.]+)?Introduction\b"),
        ("related_work",  r"\n(?:2[\s.]+)?(?:Related\s+Work|Literature\s+Review|Background)\b"),
        ("method",        r"\n(?:[23][\s.]+)?"
                          r"(?:Method|Approach|Model\s+Architecture|Framework|Proposed\s+Method|"
                          r"Our\s+Model|Architecture|Preliminaries|Formulation|Algorithm"
                          r"|Methodology|Design|Network\s+Structure|Model\s+Design)\b"),
        ("training",      r"\n(?:[34][\s.]+)?"
                          r"(?:Training\s+(\w+\s+)?Strategy|Optimization|Learning\s+Procedure|"
                          r"Training\s+Details|Implementation\s+Details|Training\s+Setup"
                          r"|Objective\s+Function|Loss\s+Function)\b"),
        ("experiments",   r"\n(?:[45][\s.]+)?"
                          r"(?:Experiment|Evaluation|Result|Ablation|Benchmark"
                          r"|Quantitative\s+Result|Qualitative\s+Result|Performance"
                          r"|Comparison|Analysis|Main\s+Result)\w*\b"),
        ("conclusion",    r"\n(?:[56][\s.]+)?(?:Conclusion|Concluding\s+Remarks|Summary"
                          r"|Discussion|Limitation|Future\s+Work|Broader\s+Impact)\b"),
    ]

    positions = {}
    for name, pattern in section_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            positions[name] = m.start()

    if not positions:
        # No headers detected — split by thirds
        n = len(text)
        return {
            "introduction": text[:n // 3],
            "method": text[n // 3: 2 * n // 3],
            "experiments": text[2 * n // 3:],
        }

    sorted_keys = sorted(positions, key=lambda k: positions[k])
    sections = {}
    for i, key in enumerate(sorted_keys):
        start = positions[key]
        end = positions[sorted_keys[i + 1]] if i + 1 < len(sorted_keys) else len(text)
        sections[key] = text[start:end].strip()

    return sections


# ---------------------------------------------------------------------------
# Rule-based key point extraction (fallback when LLM unavailable)
# ---------------------------------------------------------------------------

def extract_key_points(source_text: str, title: str, has_full_text: bool = False) -> list[str]:
    """
    Derive 3-5 structured key points from paper text using heuristics.
    source_text: abstract only, or full paper text if has_full_text=True.
    Returns a list of formatted strings ready for wiki insertion.
    """
    if not source_text:
        return [
            f"提出了基于 JEPA 思想的新方法，应用于 {_domain_hint(title)}",
            "使用联合嵌入预测架构在表征空间进行预测，避免像素重建",
            "在下游任务上验证了方法的有效性",
        ]

    sentences = [s.strip() for s in re.split(r"[.。!?\n]", source_text) if len(s.strip()) > 30]

    def find_sentence(patterns):
        for pat in patterns:
            s = next((s for s in sentences if re.search(pat, s, re.I)), None)
            if s:
                return s.strip()
        return ""

    contribution = find_sentence([
        r"\bwe (propose|introduce|present|develop|design)\b",
        r"\bwe (show|demonstrate|extend|adapt)\b",
        r"\bthis (paper|work) (proposes|presents|introduces)\b",
    ])

    result = find_sentence([
        r"(outperform|state.of.the.art|superior|improve|achieve|surpass)",
        r"(significant|substantial)\w* (gain|improvement|boost)",
    ])

    eval_sent = find_sentence([
        r"(dataset|benchmark)\b.*\b(evaluat|train|test)",
        r"\b(ImageNet|Kinetics|AudioSet|COCO|Something.Something)\b",
        r"\b(linear prob|fine.tun|transfer)\w*\b",
    ])

    points = []
    if contribution:
        points.append(f"**核心贡献**：{contribution[:120]}")
    if result and result != contribution:
        points.append(f"**实验结果**：{result[:120]}")
    if eval_sent and eval_sent not in (contribution, result):
        points.append(f"**评估设置**：{eval_sent[:120]}")

    # With full text: also try to extract a key design detail from Method section
    if has_full_text and len(points) < 4:
        sections = extract_structured_sections(source_text)
        method_text = sections.get("method", "")
        method_sents = [s.strip() for s in re.split(r"[.。\n]", method_text) if len(s.strip()) > 40]
        design_sent = next(
            (s for s in method_sents
             if re.search(r"(mask|patch|token|encoder|predictor|latent|predict)", s, re.I)
             and s not in (contribution, result, eval_sent)),
            "",
        )
        if design_sent:
            points.append(f"**关键设计**：{design_sent[:120]}")

    while len(points) < 3:
        label = "（全文中未识别到，需人工补充）" if has_full_text else "（摘要中未明确提及，需读全文后补充）"
        points.append(label)

    return points[:5]


def _domain_hint(title: str) -> str:
    """Infer application domain from paper title."""
    title_lower = title.lower()
    if any(w in title_lower for w in ["audio", "speech", "sound", "music"]):
        return "音频/语音领域"
    if any(w in title_lower for w in ["video", "temporal", "action"]):
        return "视频理解"
    if any(w in title_lower for w in ["robot", "locomotion", "manipulation"]):
        return "机器人学习"
    if any(w in title_lower for w in ["language", "text", "nlp", "bert", "llm"]):
        return "语言模型"
    if any(w in title_lower for w in ["medical", "eeg", "brain", "clinical"]):
        return "医疗/生物信号"
    if any(w in title_lower for w in ["point cloud", "3d", "lidar"]):
        return "3D 点云"
    if any(w in title_lower for w in ["multimodal", "cross-modal", "vision-language"]):
        return "多模态学习"
    return "视觉表征学习"


# ---------------------------------------------------------------------------
# Markdown field helpers
# ---------------------------------------------------------------------------

STUB_PATTERNS = [
    r"（待消化后填写",
    r"（待补充",
    r"（待填）",
    r"（从论文中提取",
    r"（不同论文对这个概念",
    r"（这个概念在",
    r"（从该分类的多篇",
    r"（按时间线梳理",
    r"（素材中提到但",
]


def _is_stub(text: str) -> bool:
    return any(re.search(p, text) for p in STUB_PATTERNS)


def _replace_section(content: str, section_header: str, new_body: str) -> str:
    """
    Replace everything between `## section_header` and the next `## ` with new_body.
    new_body should NOT include the header line itself.
    """
    # Match the section header (## or ###) and capture until next same-level heading
    level = "##" if content.count(f"\n{section_header}") == 0 else "##"
    pattern = rf"(^|\n)(#{{{len(section_header.split()[0])}}}[ \t]+{re.escape(section_header.split(' ', 1)[-1])}[^\n]*\n)(.*?)(?=\n##[ \t]|\Z)"

    # Simpler line-by-line approach (more robust for varied markdown)
    lines = content.split("\n")
    result = []
    in_target = False
    header_depth = 0

    for line in lines:
        heading_match = re.match(r"^(#+)\s+(.*)", line)
        if heading_match:
            depth = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            if title == section_header.strip():
                in_target = True
                header_depth = depth
                result.append(line)
                result.append(new_body)
                continue
            elif in_target and depth <= header_depth:
                in_target = False
        if not in_target:
            result.append(line)

    return "\n".join(result)


def _set_or_insert_section_before(
    content: str,
    section_header: str,
    new_body: str,
    before_header: str,
) -> str:
    """Replace a section if it exists; otherwise insert it immediately before another section."""
    marker = f"## {section_header}"
    if marker in content:
        return _replace_section(content, section_header, new_body.rstrip("\n") + "\n")

    anchor = f"## {before_header}"
    section_block = f"## {section_header}\n\n{new_body.rstrip()}\n\n"
    if anchor in content:
        return content.replace(anchor, section_block + anchor, 1)
    return content.rstrip() + "\n\n" + section_block


def _get_section(content: str, section_header: str) -> str:
    """Extract the body of a markdown section."""
    lines = content.split("\n")
    capturing = False
    header_depth = 0
    body_lines = []

    for line in lines:
        heading_match = re.match(r"^(#+)\s+(.*)", line)
        if heading_match:
            depth = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            if title == section_header.strip():
                capturing = True
                header_depth = depth
                continue
            elif capturing and depth <= header_depth:
                break
        if capturing:
            body_lines.append(line)

    return "\n".join(body_lines).strip()


def _resolve_source_template_spec(
    page_path: Path,
    paper: dict,
    content: str,
    template_dir: str | Path | None = None,
):
    template_id = (
        paper.get("template_id")
        or extract_frontmatter_value(content, "template_id")
        or resolve_template("auto", item=paper, template_dir=template_dir).template_id
    )
    return load_template(template_id, template_dir=template_dir)


def _generic_facts_body(paper: dict) -> str:
    lines = []
    title = paper.get("title") or ""
    if title:
        lines.append(f"- **标题**：{title}")
    if paper.get("authors"):
        lines.append(f"- **作者**：{', '.join((paper.get('authors') or [])[:5])}")
    if paper.get("year"):
        lines.append(f"- **年份**：{paper.get('year')}")
    if paper.get("category"):
        lines.append(f"- **分类**：{paper.get('category')}")
    if paper.get("url"):
        lines.append(f"- **来源**：[访问原文]({paper.get('url')})")
    if paper.get("project_urls"):
        lines.append(f"- **相关链接数**：{len(paper.get('project_urls') or [])}")
    return "\n".join(lines) if lines else "（待补充）"


def _enrich_generic_source_page(
    page_path: Path,
    paper: dict,
    content: str,
    pdf_dir: Path | None,
    all_papers: list[dict],
    force: bool,
    llm_provider: str,
    llm_model: str,
    direct_input: str | None,
    template_dir: str | Path | None,
) -> bool:
    spec = _resolve_source_template_spec(page_path, paper, content, template_dir=template_dir)
    summary_header = spec.sections.get("summary", "摘要")
    facts_header = spec.sections.get("facts", "关键事实")
    highlights_header = spec.sections.get("highlights", "要点")
    relations_header = spec.sections.get("relations", "关联内容")
    notes_header = spec.sections.get("notes", "我的注释")
    actions_header = spec.sections.get("actions", "行动项")

    summary_section = _get_section(content, summary_header)
    facts_section = _get_section(content, facts_header)
    highlights_section = _get_section(content, highlights_header)
    relations_section = _get_section(content, relations_header)
    actions_section = _get_section(content, actions_header) if actions_header else ""

    needs_enrichment = force or any([
        not summary_section or _is_stub(summary_section),
        not facts_section or _is_stub(facts_section),
        not highlights_section or _is_stub(highlights_section),
        not relations_section or _is_stub(relations_section),
        actions_header and (not actions_section or _is_stub(actions_section)),
    ])
    if not needs_enrichment:
        return False

    title = paper.get("title") or ""
    abstract = paper.get("abstract") or ""
    url = paper.get("url") or ""
    pdf_text = ""
    if pdf_dir:
        arxiv_id = paper.get("arxiv_id", "")
        if arxiv_id:
            pdf_path = pdf_dir / f"{arxiv_id}.pdf"
            if pdf_path.exists():
                pdf_text = extract_pdf_text(pdf_path)
        if not pdf_text:
            pdf_path = _non_arxiv_pdf_path(paper, pdf_dir)
            if pdf_path:
                pdf_text = extract_pdf_text(pdf_path)
    if not pdf_text and url:
        pdf_text = fetch_url_text(url, max_chars=cfg("pdf", "url_fetch_max_chars", 8000))

    source_text = pdf_text or abstract or url
    related = _find_related_papers(paper, all_papers)
    related_titles = "\n".join(
        f"- {r['title']} ({r.get('year','')})"
        for r in related[:cfg("wiki", "related_papers_count", 6)]
    ) or "（暂无）"

    if LLM_AVAILABLE:
        prompt = f"""你正在维护个人知识库中的一个通用条目（可能是技术文档、博客文章、GitHub 项目 README、配置说明等）。

条目信息：
- 标题：{title}
- 分类：{paper.get('category') or '（未分类）'}
- 来源：{url or '（无）'}

条目内容：
{source_text[:6000]}

知识库中可能相关的条目：
{related_titles}

请按以下标题输出内容。**如果条目包含技术架构、API 设计、代码逻辑或实施方法，请在"要点"中尽可能详细地抽取核心设计、模块结构、关键参数或调用方式。**

### {summary_header}
2-4 句话概括这个条目的核心内容。

### {facts_header}
用项目符号列出 4-6 条关键事实、背景、定义、时间、主体或数据点。

### {highlights_header}
列出 3-5 条要点，每条格式：`N. **主题**：内容（≤100字）`。
如果条目是技术类内容，至少包含：
- 核心设计/架构思路
- 关键技术选型或参数
- 使用方式或接入要点

### {relations_header}
列出与知识库中其他条目的关系；若没有把握，写 `（待补充）`。

### {notes_header}
写 2-4 句补充说明，强调理解、限制或上下文。

### {actions_header}
给出 1-3 条后续动作、待查问题或跟进建议；若不适用，写 `（待补充）`。
"""
        try:
            raw = call_llm(prompt, provider=llm_provider, model=llm_model, direct_input=direct_input)
        except (subprocess.TimeoutExpired, RuntimeError, ValueError) as e:
            print(f"  [warn] LLM call failed for {page_path.name}: {e}. Skipping LLM enrichment, prompts saved for agent.", file=sys.stderr)
            raw = None

        if raw is None:
            prompt_file = page_path.parent / f".prompt_{page_path.stem}.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            print(f"  [prompt] {prompt_file.name} saved → ready for agent-based enrichment", file=sys.stderr)
            return False

        summary_body = _parse_llm_section(raw, summary_header)
        facts_body = _parse_llm_section(raw, facts_header)
        highlights_body = _parse_llm_section(raw, highlights_header)
        relations_body = _parse_llm_section(raw, relations_header)
        notes_body = _parse_llm_section(raw, notes_header)
        actions_body = _parse_llm_section(raw, actions_header)
    else:
        points = extract_key_points(source_text, title, has_full_text=bool(pdf_text))
        summary_body = abstract or (pdf_text[:500] if pdf_text else "（待补充）")
        facts_body = _generic_facts_body(paper)
        highlights_body = _fmt_points(points)
        relations_body = markdown_bullets(
            [f"《{r.get('title','')}》：相关主题或背景相近" for r in related[:4]],
            fallback="（待补充）",
        )
        notes_body = "（规则模式未生成注释，请后续补充）"
        actions_body = "（待补充）"

    new_content = _replace_section(content, summary_header, summary_body + "\n")
    new_content = _replace_section(new_content, facts_header, facts_body + "\n")
    new_content = _replace_section(new_content, highlights_header, highlights_body + "\n")
    new_content = _replace_section(new_content, relations_header, relations_body + "\n")
    new_content = _replace_section(new_content, notes_header, notes_body + "\n")
    if actions_header:
        new_content = _replace_section(new_content, actions_header, actions_body + "\n")
    new_content = _update_frontmatter_date(new_content)
    new_content = _remove_missing_images(new_content, page_path)
    page_path.write_text(new_content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Source page enrichment
# ---------------------------------------------------------------------------

def enrich_source_page(
    page_path: Path,
    paper: dict,
    pdf_dir: Path | None,
    all_papers: list[dict],
    force: bool,
    llm_provider: str,
    llm_model: str,
    figures_dir: Path | None = None,
    media_dir: Path | None = None,
    web_resources: bool = False,
    direct_input: str | None = None,
    template_dir: str | Path | None = None,
) -> bool:
    """
    Fully enrich a source page by reading the complete paper (PDF full text +
    figure captions + project URL content) and generating rich wiki content via LLM.
    If figures_dir is provided, downloads paper figures and inserts a 论文图表 section.
    If media_dir is provided, downloads GIFs + YouTube thumbnails and inserts a 演示与视频 section.
    If web_resources is True, searches for related blog posts/videos/GitHub and inserts a 互联网资源 section.
    Returns True if the file was modified.
    """
    content = page_path.read_text(encoding="utf-8")
    template_spec = _resolve_source_template_spec(page_path, paper, content, template_dir=template_dir)
    if template_spec.enrich_mode == "generic":
        return _enrich_generic_source_page(
            page_path,
            paper,
            content,
            pdf_dir,
            all_papers,
            force,
            llm_provider,
            llm_model,
            direct_input,
            template_dir,
        )

    highlights_header = template_spec.sections.get("highlights", "核心观点")
    concepts_header = template_spec.sections.get("concepts", "关键概念")
    relations_header = template_spec.sections.get("relations", "与其他论文的关联")
    citation_header = template_spec.sections.get("citation", "引用关系")
    method_header = template_spec.sections.get("method", "方法摘要")
    detailed_method_header = template_spec.sections.get("detailed_method", "具体方法")
    results_header = template_spec.sections.get("results", "实验与结果")
    figures_header = template_spec.sections.get("figures", "论文图表")

    key_points_section = _get_section(content, highlights_header)
    method_section = _get_section(content, method_header)
    detailed_method_section = _get_section(content, detailed_method_header)
    results_section = _get_section(content, results_header)
    figures_section = _get_section(content, figures_header)
    needs_enrichment = (
        _is_stub(key_points_section)
        or _is_stub(method_section)
        or not method_section  # section missing entirely
        or _is_stub(detailed_method_section)
        or not detailed_method_section
        or _is_stub(results_section)
        or not results_section
    )
    needs_media = template_spec.enrich_mode == "research_paper" and (
        (figures_dir is not None and (not figures_section or _is_stub(figures_section)))
        or (media_dir is not None and "## 演示与视频" not in content)
    )
    needs_web = template_spec.enrich_mode == "research_paper" and web_resources and "## 互联网资源" not in content
    if not needs_enrichment and not needs_media and not needs_web and not force:
        return False

    abstract = paper.get("abstract") or ""
    title = paper.get("title") or ""
    arxiv_id = paper.get("arxiv_id", "")

    new_content = content

    if needs_enrichment or force:
        # --- Gather full text ---
        pdf_text = ""
        if pdf_dir:
            if arxiv_id:
                pdf_path = pdf_dir / f"{arxiv_id}.pdf"
                if pdf_path.exists():
                    pdf_text = extract_pdf_text(pdf_path)
            if not pdf_text:
                pdf_path = _non_arxiv_pdf_path(paper, pdf_dir)
                if pdf_path:
                    pdf_text = extract_pdf_text(pdf_path)

        has_full_text = bool(pdf_text)
        figure_captions = extract_figure_captions(pdf_text) if pdf_text else ""
        formula_candidates = extract_formula_candidates(pdf_text) if pdf_text else ""

        # --- Gather project URL content ---
        project_content = fetch_project_content(paper)

        # --- Build full context for LLM ---
        if has_full_text:
            sections = extract_structured_sections(pdf_text)
            paper_body = "\n\n---\n\n".join(
                f"[{k.upper()}]\n{v[:5000]}" for k, v in sections.items() if v
            )
        else:
            paper_body = f"[ABSTRACT]\n{abstract}"

        related = _find_related_papers(paper, all_papers)
        related_titles = "\n".join(
            f"- {r['title']} ({r.get('year','')})"
            for r in related[:cfg("wiki", "related_papers_count", 6)]
        )

        if LLM_AVAILABLE:
            extra_context = ""
            if figure_captions:
                extra_context += f"\n\n{figure_captions}"
            if formula_candidates:
                extra_context += f"\n\n{formula_candidates}"
            if project_content:
                extra_context += f"\n\n[PROJECT / CODE]\n{project_content[:cfg('wiki', 'project_content_max', 3000)]}"

            figures_desc = figure_captions if figure_captions else "（PDF 中未提取到图表说明，请根据正文中的 Figure/Table 引用进行描述）"

            topic_name = cfg("wiki", "topic", "AI research")
            prompt = f"""你是一位 AI 研究员，正在为研究知识库撰写一篇论文的详细摘要页。

## 论文信息
- 标题：{title}
- arXiv ID：{arxiv_id}
- 年份：{paper.get('year', '?')}
- 作者：{', '.join((paper.get('authors') or [])[:5])}

## 论文全文（按章节）
{paper_body}{extra_context}

## 知识库中最相关的论文（供参考）
{related_titles if related_titles else '（暂无）'}

---

请用中文完整填写以下各个字段。**严格按照下面的格式输出，每个字段用 ### 标题开头，不要添加额外的解释文字，不要用代码块包裹整个输出。**

### 核心观点
列出 4-5 条，每条格式：`N. **主题**：具体内容（≤100字）`
要求：
- 第1条：这篇论文的核心贡献/创新点是什么（要具体）
- 第2条：方法的关键设计——架构、目标函数、训练策略中最重要的一个
- 第3条：主要实验结果——哪个数据集、什么指标、超过了什么baseline、差距多少
- 第4条：这篇论文与知识库中相关工作的异同或关系
- 第5条（可选）：局限性或未来方向

### 与其他论文的关联
用于知识图谱双向导航，每篇一行：`- 与 《论文短标题》 的关系：从本文视角描述关系（继承/扩展/对比/应用，≤50字）`
只写你确实能判断关系的，不确定的跳过。

### 引用关系
记录单向引用事实（不是 wikilink，只是文本记录）：
**本文引用**：逐行列出本文正文中显式引用的知识库内论文，格式 `- 《标题》(arxiv_id)`，找不到的跳过
**被以下论文引用**：逐行列出知识库内引用了本文的论文，格式 `- 《标题》(arxiv_id)`，不确定的跳过

### 方法摘要
用 3-5 句话（中文）描述这篇论文的方法，要求：
- 包含架构组成（编码器、预测器等模块）
- 包含训练目标/损失函数的核心思想
- 如果有图表说明（figure captions），引用具体的架构描述

### 具体方法
围绕论文中的核心方法写成详细、可直接引用的技术说明。**不要翻译 paper background**，直接写方法本身。必须包含以下子标题：

#### 整体架构
- 说明输入/输出数据格式（维度、shape、类型）
- 画一个数据流描述：`Input → [Module A] → [Module B] → ... → Output`，按论文中的阶段划分
- 如果论文有多阶段流程（如 pretrain → finetune, two-stage training），先给出阶段划分

#### 核心模块
- 逐条说明每个关键模块，推荐用伪代码式描述：
  ```
  模块名(X):
      步骤 1：...
      步骤 2：...
      返回 Y
  ```
- 明确每个模块解决什么问题、输入/输出形状、以及模块之间如何衔接
- 保留论文中使用的模块名、变量名、超参数名（如 `context_encoder`, `target_encoder`, `predictor`, `momentum`, `τ`）

#### 关键公式
- 给出 2-6 个最核心的公式、目标函数或优化目标
- 使用 LaTeX 数学模式 `$$ ... $$` 包裹公式，确保可渲染
- 每个公式后写一段解释：
  - 公式中每个符号的定义（$x$ 是什么，$f_\theta$ 是什么）
  - 这个公式在整体方法中的角色（损失函数？正则项？预测目标？约束？）
  - 优化方向（最小化还是最大化）
- **如果 PDF 文本中公式不完整，尝试根据上下文恢复**，但要标注 `（近似恢复）`
- 常见公式写法示例（用 $$ ... $$ 包裹）:"""

            formula_examples = """
  - 损失函数: `$$\\mathcal{L} = \\frac{1}{N}\\sum_i \\|f_\\theta(x_i) - g_\\phi(y_i)\\|_2^2$$`
  - 注意力: `$$\\text{Attention}(Q,K,V) = \\text{softmax}\\left(\\frac{QK^T}{\\sqrt{d_k}}\\right)V$$`
  - 对比损失: `$$\\mathcal{L} = -\\log\\frac{\\exp(\\text{sim}(z_i, z_j)/\\tau)}{\\sum_k\\exp(\\text{sim}(z_i, z_k)/\\tau)}$$`"""

            prompt += formula_examples + """

#### 训练与推理流程
- 分点写训练阶段（每个 epoch/step 做什么、损失项、梯度更新方式、采样策略、数据增强等）
- 分点写推理/测试阶段（是否需要特殊处理、推理速度/显存信息如果有的话）
- 若有蒸馏、EMA、momentum update、stop-gradient、多阶段训练、后处理等，都写清楚

#### 架构描述（基于图表）
- 结合正文描述和以下图表说明中的 figure/table，用自然语言描述论文的架构图：
{figures_desc}
- 优先引用具体图号，如 `Figure 2（整体架构）`、`Figure 3（核心模块细节）`
- 如果一张图中的子图说明不同概念，拆开描述

要求：
- 优先抽取论文真正的核心方法，不是背景介绍或 related work
- 尽量保留论文中的模块名、变量名、损失名、阶段名
- 力求可直接用作笔记引用或代码实现的参考
- 若某子标题下确实没有足够信息，写"（论文正文中该部分信息不足，建议阅读原文补充）"

### 实验与结果
用 2-4 句话总结主要实验，要求：
- 列出具体数据集名称
- 列出具体指标数字（如果有）
- 与哪些方法对比，差距如何
"""
            try:
                raw = call_llm(prompt, provider=llm_provider, model=llm_model, direct_input=direct_input)
            except (subprocess.TimeoutExpired, RuntimeError, ValueError) as e:
                print(f"  [warn] LLM call failed for {page_path.name}: {e}. Skipping LLM enrichment, prompts saved for agent.", file=sys.stderr)
                raw = None

            if raw is None:
                prompt_file = page_path.parent / f".prompt_{page_path.stem}.md"
                prompt_file.write_text(prompt, encoding="utf-8")
                print(f"  [prompt] {prompt_file.name} saved → ready for agent-based enrichment", file=sys.stderr)
                return False

            key_points_body = _parse_llm_section(raw, highlights_header)
            rel_body = _parse_llm_section(raw, relations_header)
            # Resolve short titles to precise Obsidian slugs via fuzzy matching
            rel_body = _resolve_short_titles_to_slugs(rel_body, all_papers)
            citation_body = _parse_llm_section(raw, citation_header)
            method_body = _parse_llm_section(raw, method_header)
            detailed_method_body = _parse_llm_section(raw, detailed_method_header)
            results_body = _parse_llm_section(raw, results_header)
        else:
            points = extract_key_points(pdf_text or abstract, title, has_full_text=has_full_text)
            key_points_body = _fmt_points(points)
            rel_body = _build_relation_body(paper, related)
            citation_body = "（待补充）"
            method_body = "（规则模式无法生成，请使用 --llm-provider auto 重新运行（需配置 API key））"
            detailed_method_body = "\n".join([
                "（规则模式无法详细抽取具体方法，请使用 --llm-provider auto 重新运行（需配置 API key））",
                "",
                "#### 整体架构",
                "（待补充）",
                "",
                "#### 核心模块",
                "（待补充）",
                "",
                "#### 关键公式",
                "（待补充）",
                "",
                "#### 训练与推理流程",
                "（待补充）",
                "",
                "#### 架构描述（基于图表）",
                "（待补充）",
            ])
            results_body = "（规则模式无法生成，请使用 --llm-provider auto 重新运行（需配置 API key））"

        # Apply all section updates
        new_content = _replace_section(content, highlights_header, key_points_body + "\n")
        new_content = _replace_section(new_content, relations_header, rel_body + "\n")
        # Insert 引用关系 section after 与其他论文的关联 if not present
        if f"## {citation_header}" not in new_content:
            new_content = new_content.replace(
                "\n## 项目资源",
                f"\n## {citation_header}\n\n{citation_body}\n\n## 项目资源",
                1,
            )
            if f"## {citation_header}" not in new_content:  # fallback: insert before 相关页面
                new_content = new_content.replace(
                    "\n## 相关页面",
                    f"\n## {citation_header}\n\n{citation_body}\n\n## 相关页面",
                    1,
                )
        else:
            new_content = _replace_section(new_content, citation_header, citation_body + "\n")

        new_content = _set_or_insert_section_before(
            new_content, method_header, method_body, concepts_header
        )
        new_content = _set_or_insert_section_before(
            new_content, detailed_method_header, detailed_method_body, results_header if f"## {results_header}" in new_content else concepts_header
        )
        new_content = _set_or_insert_section_before(
            new_content, results_header, results_body, concepts_header
        )

    # --- Download figures and insert 论文图表 section ---
    if figures_dir is not None and arxiv_id:
        paper_figures_dir = figures_dir / (arxiv_id or page_path.stem)
        # Skip if already downloaded
        figures_meta = paper_figures_dir / "figures.json"
        if figures_meta.exists() and not force:
            existing = json.loads(figures_meta.read_text(encoding="utf-8"))
        else:
            try:
                ff = _import_figure_fetcher()
                pdf_path_for_fig = (pdf_dir / f"{arxiv_id}.pdf") if (pdf_dir and arxiv_id) else None
                if pdf_path_for_fig and not pdf_path_for_fig.exists():
                    pdf_path_for_fig = None
                existing = ff.fetch_figures(
                    arxiv_id=arxiv_id,
                    pdf_path=pdf_path_for_fig,
                    out_dir=paper_figures_dir,
                    max_figures=cfg("figures", "max_figures", 10),
                    min_size_kb=cfg("figures", "min_size_kb", 15),
                )
            except Exception as e:
                print(f"  [warn] figure fetch failed: {e}", file=sys.stderr)
                existing = []

        if existing:
            try:
                ff = _import_figure_fetcher()
                figures_md = ff.figures_to_markdown(
                    existing, paper_figures_dir, page_dir=page_path.parent)
            except Exception:
                figures_md = ""

            if figures_md:
                if f"## {figures_header}" in new_content:
                    # Replace existing section (strip old content up to next ##)
                    # Use lambda to avoid re.sub interpreting backslashes in replacement string
                    _repl = figures_md.rstrip() + "\n"
                    new_content = re.sub(
                        rf"## {re.escape(figures_header)}\n.*?(?=\n## |\Z)", lambda _: _repl,
                        new_content, count=1, flags=re.DOTALL)
                else:
                    anchor = f"## {concepts_header}"
                    if anchor in new_content:
                        new_content = new_content.replace(anchor, figures_md + "\n" + anchor, 1)
                    else:
                        new_content = new_content.rstrip() + "\n\n" + figures_md

    # --- Download GIFs/YouTube and insert 演示与视频 section ---
    if media_dir is not None:
        paper_media_dir = media_dir / (arxiv_id or page_path.stem)
        media_meta = paper_media_dir / "media.json"
        if media_meta.exists() and not force:
            existing_media = json.loads(media_meta.read_text(encoding="utf-8"))
        else:
            try:
                fm = _import_media_fetcher()
                existing_media = fm.fetch_media(
                    paper, paper_media_dir,
                    max_gif_count=cfg("media", "max_gif_count", 3),
                    max_youtube=cfg("media", "max_youtube", 2),
                )
            except Exception as e:
                print(f"  [warn] media fetch failed: {e}", file=sys.stderr)
                existing_media = []

        if existing_media:
            try:
                fm = _import_media_fetcher()
                media_md = fm.media_to_markdown(
                    existing_media, paper_media_dir, page_dir=page_path.parent)
            except Exception:
                media_md = ""

            if media_md:
                if "## 演示与视频" in new_content:
                    _repl = media_md.rstrip() + "\n"
                    new_content = re.sub(
                        r"## 演示与视频\n.*?(?=\n## |\Z)", lambda _: _repl,
                        new_content, count=1, flags=re.DOTALL)
                else:
                    anchor = "## 关键概念"
                    if anchor in new_content:
                        new_content = new_content.replace(anchor, media_md + "\n" + anchor, 1)
                    else:
                        new_content = new_content.rstrip() + "\n\n" + media_md

    # --- Search web resources and insert 互联网资源 section ---
    if needs_web or (force and web_resources):
        web_md = fetch_web_resources_for_paper(paper, provider=llm_provider, model=llm_model)
        if web_md:
            if "## 互联网资源" in new_content:
                _repl = web_md.rstrip() + "\n"
                new_content = re.sub(
                    r"## 互联网资源\n.*?(?=\n## |\Z)", lambda _: _repl,
                    new_content, count=1, flags=re.DOTALL)
            else:
                # Insert before 关键概念, or at end
                anchor = "## 关键概念"
                if anchor in new_content:
                    new_content = new_content.replace(anchor, web_md + "\n" + anchor, 1)
                else:
                    new_content = new_content.rstrip() + "\n\n" + web_md

    new_content = _update_source_frontmatter(new_content, paper)

    # AUTOMATION: Forcibly regenerate the Figures section to ensure correct extensions/paths
    if figures_dir and (figures_dir / "figures.json").exists():
        from fetch_paper_figures import figures_to_markdown
        try:
            fig_cache = json.loads((figures_dir / "figures.json").read_text(encoding="utf-8"))
            # The figures_to_markdown function in fetch_paper_figures.py 
            # now handles forward slashes and correct labels.
            fig_md = figures_to_markdown(fig_cache, figures_dir, page_path.parent)
            if fig_md:
                new_content = _replace_section(new_content, "## 论文图表", fig_md + "\n")
        except Exception as e:
            print(f"  [warn] Failed to auto-regenerate figures section: {e}", file=sys.stderr)

    new_content = _update_frontmatter_date(new_content)
    new_content = update_toc(new_content)
    new_content = _remove_missing_images(new_content, page_path)
    page_path.write_text(new_content, encoding="utf-8")
    return True


def _fmt_points(points: list[str]) -> str:
    return "\n".join(f"{i+1}. {p}" for i, p in enumerate(points))


def _resolve_short_titles_to_slugs(rel_body: str, all_papers: list[dict]) -> str:
    """
    Finds lines matching `- 与 《Short Title》 的关系：...` 
    and replaces 《Short Title》 with the exact Obsidian [[slug]].
    """
    if not rel_body:
        return rel_body

    lines = rel_body.split('\n')
    resolved_lines = []
    
    # Build a lookup list for fuzzy matching
    paper_titles = [p.get('title', '') for p in all_papers]
    
    for line in lines:
        m = re.search(r"- 与 《(.*?)》 的关系：(.*)", line)
        if m:
            short_title = m.group(1)
            relation_desc = m.group(2)
            
            # Find the best match
            matches = difflib.get_close_matches(short_title, paper_titles, n=1, cutoff=0.3)
            if matches:
                best_match_title = matches[0]
                # Find the corresponding slug
                best_paper = next((p for p in all_papers if p.get('title') == best_match_title), None)
                if best_paper and 'slug' in best_paper:
                    resolved_lines.append(f"- 与 [[{best_paper['slug']}]] 的关系：{relation_desc}")
                else:
                     resolved_lines.append(line)
            else:
                resolved_lines.append(line)
        else:
            resolved_lines.append(line)
            
    return "\n".join(resolved_lines)

def _parse_llm_section(raw: str, section_name: str) -> str:
    """
    Extract a named section from LLM free-form output.

    Handles the full range of formats any agent CLI may produce:
    - Fenced code blocks (```markdown ... ```) wrapping the entire output
    - ANSI escape codes in the raw string
    - Leading preamble text before the first section header
    - Any heading depth (##, ###, ####) for section delimiters
    - Bold-label style (**section_name**)
    - Trailing whitespace / blank lines

    NOTE: patterns are built with string concatenation, NOT f-strings, so that
    regex quantifiers like {2,6} are not accidentally interpreted as f-string
    interpolation expressions (which would silently produce "(2, 6)" instead).
    """
    # Strip ANSI escape codes that some CLIs inject into stdout
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    text = ansi_escape.sub("", raw)

    # Unwrap top-level fenced code block if the entire response is wrapped
    fenced = re.match(r"^\s*```[a-z]*\n(.*?)```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)

    esc = re.escape(section_name)
    # Build patterns with concatenation so {2,6} stays as a regex quantifier,
    # not a Python f-string expression that evaluates to the tuple (2, 6).
    patterns = [
        # Any heading depth (## / ### / ####), stop at next same-or-higher heading
        r"(?m)^#{2,6}\s+" + esc + r"\s*$\n(.*?)(?=\n#{2,6}\s|\Z)",
        # Bold label on its own line: **section_name**
        r"(?m)^\*\*" + esc + r"\*\*\s*$\n(.*?)(?=\n\*\*[^\n]+\*\*|\Z)",
        # Heading inside a per-section fenced block
        r"```[a-z]*\n#{2,6}\s+" + esc + r"\s*\n(.*?)```",
        # Colon-terminated label: "核心观点：\ncontent"
        r"(?m)^" + esc + r"[：:]\s*\n(.*?)(?=\n[^\n]{2,}[：:]|\Z)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            body = m.group(1).strip()
            # Strip an inner fenced block if present
            inner = re.match(r"^\s*```[a-z]*\n(.*?)```\s*$", body, re.DOTALL)
            if inner:
                body = inner.group(1).strip()
            return body

    raise RuntimeError(
        f"Section '{section_name}' not found in LLM response "
        f"(tried {len(patterns)} patterns; first 300 chars: {text[:300]!r})"
    )


def _find_related_papers(paper: dict, all_papers: list[dict]) -> list[dict]:
    """Heuristically find related papers (same category, or shared keywords in title)."""
    cat = paper.get("category", "")
    title_words = set(re.findall(r"\b[a-z]{4,}\b", (paper.get("title") or "").lower()))
    scored = []
    for other in all_papers:
        if other.get("arxiv_id") == paper.get("arxiv_id") and other.get("title") == paper.get("title"):
            continue
        score = 0
        if other.get("category") == cat:
            score += 2
        other_words = set(re.findall(r"\b[a-z]{4,}\b", (other.get("title") or "").lower()))
        score += len(title_words & other_words)
        if other.get("is_seed"):
            score += 1
        scored.append((score, other))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:5] if _ > 0]


def _fix_backlinks(sources_dir: Path) -> tuple[int, list[Path]]:
    """
    Ensure 与其他论文的关联 is bidirectional across all source pages.

    For every link  A → [[B-slug]]  found in page A's 关联 section,
    check whether page B's 关联 section contains [[A-stem]] (or an alias).
    If not, append a minimal reverse-link line to page B.

    Returns the total number of reverse-link lines added and the modified files.
    """
    source_files = list(sources_dir.glob("*.md"))

    # Build stem → path and aliases → path maps
    stem_to_path: dict[str, Path] = {f.stem: f for f in source_files}
    alias_to_stem: dict[str, str] = {}
    for f in source_files:
        content = f.read_text(encoding="utf-8")
        alias_m = re.search(r"^aliases:\s*\[([^\]]*)\]", content, re.MULTILINE)
        if alias_m:
            for alias in re.findall(r'"([^"]+)"', alias_m.group(1)):
                alias_to_stem[alias.lower()] = f.stem
                alias_to_stem[alias] = f.stem

    def _resolve_slug(slug: str) -> str | None:
        """Return canonical stem for a wikilink slug, or None."""
        if slug in stem_to_path:
            return slug
        if slug.lower() in stem_to_path:
            return slug.lower()
        if slug in alias_to_stem:
            return alias_to_stem[slug]
        if slug.lower() in alias_to_stem:
            return alias_to_stem[slug.lower()]
        return None

    def _extract_assoc_links(content: str) -> list[str]:
        """Return [[slug]] targets from the relation section only."""
        relation_header = find_first_header(content, RELATIONS_HEADER_ALIASES, RELATIONS_HEADER)
        assoc_m = re.search(
            rf"## {re.escape(relation_header)}\s*\n(.*?)(?=\n## |\Z)", content, re.DOTALL
        )
        if not assoc_m:
            return []
        return re.findall(r"\[\[([^\]|]+)\]\]", assoc_m.group(1))

    def _links_in_assoc(content: str) -> set[str]:
        """All slugs/stems mentioned in 关联 section (raw, not resolved)."""
        return set(_extract_assoc_links(content))

    # First pass: collect all A→B links
    # outlinks[A_stem] = list of resolved B_stems
    outlinks: dict[str, list[str]] = {}
    for f in source_files:
        content = f.read_text(encoding="utf-8")
        targets = []
        for slug in _extract_assoc_links(content):
            resolved = _resolve_slug(slug)
            if resolved and resolved != f.stem:
                targets.append(resolved)
        outlinks[f.stem] = targets

    # Second pass: for each A→B, check B has any link back to A
    fixed = 0
    modified_paths: set[Path] = set()
    for a_stem, b_stems in outlinks.items():
        a_path = stem_to_path[a_stem]
        a_title_m = re.search(r"^# (.+)$", a_path.read_text(encoding="utf-8"), re.MULTILINE)
        a_display = a_title_m.group(1).strip() if a_title_m else a_stem

        for b_stem in b_stems:
            b_path = stem_to_path.get(b_stem)
            if not b_path:
                continue
            b_content = b_path.read_text(encoding="utf-8")

            # Check if B already references A in any form
            b_assoc_links = _links_in_assoc(b_content)
            already_linked = any(
                _resolve_slug(lnk) == a_stem for lnk in b_assoc_links
            )
            if already_linked:
                continue

            # Need to add reverse link to B's 关联 section
            reverse_line = f"- 与 [[{a_stem}]] 的关系：与《{a_display}》存在关联（见对方页面描述）\n"

            # Insert into 关联 section
            relation_header = find_first_header(b_content, RELATIONS_HEADER_ALIASES, RELATIONS_HEADER)
            new_b = _replace_section(
                b_content,
                relation_header,
                _get_section_body(b_content, relation_header) + reverse_line,
            )
            if new_b != b_content:
                b_path.write_text(new_b, encoding="utf-8")
                fixed += 1
                modified_paths.add(b_path)
                print(f"  [backlink] {b_stem[:50]} ← [[{a_stem[:40]}]]")

    return fixed, sorted(modified_paths)


def _get_section_body(content: str, section_header: str) -> str:
    """Extract current body of a ## section (without the header line)."""
    lines = content.split("\n")
    in_target = False
    header_depth = 0
    body_lines = []
    for line in lines:
        m = re.match(r"^(#+)\s+(.*)", line)
        if m:
            depth = len(m.group(1))
            title = m.group(2).strip()
            if title == section_header.strip():
                in_target = True
                header_depth = depth
                continue
            elif in_target and depth <= header_depth:
                break
        if in_target:
            body_lines.append(line)
    return "\n".join(body_lines)


def _build_relation_body(paper: dict, related: list[dict]) -> str:
    """Build the 与其他论文的关联 section body (rule-based)."""
    if not related:
        return "（暂未识别到直接关联论文，需人工补充）\n"
    lines = []
    for r in related:
        slug = _paper_slug(r)
        title = r.get("title", "")[:40]
        if r.get("is_seed"):
            lines.append(f"- 与 [[{slug}]] 的关系：扩展了种子论文 {title} 的核心框架")
        elif r.get("category") == paper.get("category"):
            lines.append(f"- 与 [[{slug}]] 的关系：同属 {paper.get('category','')} 方向，可对比分析")
        else:
            lines.append(f"- 与 [[{slug}]] 的关系：（待补充）")
    return "\n".join(lines) + "\n"


def _paper_slug(paper: dict) -> str:
    arxiv_id = paper.get("arxiv_id", "")
    title = paper.get("title") or ""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:150]
    if arxiv_id:
        return f"{slug}-{arxiv_id}"
    return slug


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
        # Also update tags if it contains the old category
        fm = re.sub(r"^tags:\s*\[(.*?)(,\s*[^,\]]+)?\]$", lambda m: f"tags: [{m.group(1)}, {cat}]" if cat not in m.group(0) else m.group(0), fm, flags=re.MULTILINE)

    # Update year
    year = paper.get("year")
    if year:
        fm = re.sub(r"^year:\s*.*$", f"year: {year}", fm, flags=re.MULTILINE)
        
    return f"---{fm}---{parts[2]}"

def _update_frontmatter_date(content: str) -> str:
    return re.sub(r"^updated: .*$", f"updated: {TODAY}", content, flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# Entity page enrichment
# ---------------------------------------------------------------------------

def enrich_entity_page(
    page_path: Path,
    papers_by_concept: list[dict],
    concept_name: str,
    force: bool,
    llm_provider: str,
    llm_model: str,
) -> bool:
    """Fill 简介, 在当前知识库中的角色, 不同条目中的观点 for an entity page."""
    content = page_path.read_text(encoding="utf-8")
    perspectives_header = find_first_header(
        content,
        ENTITY_PERSPECTIVES_HEADER_ALIASES,
        ENTITY_PERSPECTIVES_HEADER,
    )

    intro_section = _get_section(content, "简介")
    if not _is_stub(intro_section) and not force:
        return False

    # Collect abstracts from linked papers
    context_snippets = []
    for p in papers_by_concept[:cfg("wiki", "entity_papers_max", 8)]:
        ab = (p.get("abstract") or "")[:cfg("wiki", "abstract_truncate", 300)]
        if ab:
            context_snippets.append(f"《{p.get('title','')}》({p.get('year','')}): {ab}")

    context = "\n\n".join(context_snippets)

    if LLM_AVAILABLE:
        prompt = f"""你正在维护一个个人知识库的概念实体页。

概念名称：{concept_name}

以下是涉及该概念的条目摘要（按时间排序）：
{context[:cfg("wiki", "entity_context_max", 5000)]}

请用中文完成以下三个字段，直接输出内容（不要重复字段名）：

### 简介
2-3 句话定义该概念的核心含义，解释它在机器学习/深度学习中的基本思想。要有实质内容，不能只说"是一个重要概念"。

        ### 在当前知识库中的角色
        2-3 句话说明这个概念在当前知识库中扮演什么角色——它主要关联哪些条目、问题或方法？后续内容如何沿用或改写它？

### {perspectives_header}
每个条目一行，格式：`- 《条目名（时间）》：该条目对该概念的具体处理方式或独特观点（≤60字）`
只写你能从上面摘要中确认的内容。
"""
        try:
            raw = call_llm(prompt, provider=llm_provider, model=llm_model)
        except (subprocess.TimeoutExpired, RuntimeError, ValueError) as e:
            print(f"  [warn] LLM call failed for {page_path.name}: {e}. Skipping.", file=sys.stderr)
            return False

        if raw is None:
            print(f"  [skip] {page_path.name}: Waiting for direct-inference manual application.", file=sys.stderr)
            return False

        intro = _parse_llm_section(raw, "简介")
        role = _parse_llm_section(raw, "在当前知识库中的角色")
        perspectives = _parse_llm_section(raw, perspectives_header)
    else:
        intro, role, perspectives = _rule_based_entity_content(concept_name, papers_by_concept)

    new_content = _replace_section(content, "简介", intro + "\n")
    new_content = _replace_section(new_content, "在当前知识库中的角色", role + "\n")
    new_content = _replace_section(new_content, perspectives_header, perspectives + "\n")
    new_content = _update_frontmatter_date(new_content)

    new_content = _remove_missing_images(new_content, page_path)
    page_path.write_text(new_content, encoding="utf-8")
    return True


def _rule_based_entity_content(concept: str, papers: list[dict]) -> tuple[str, str, str]:
    """Generate entity page content from paper metadata (no LLM)."""
    years = sorted(set(p["year"] for p in papers if p.get("year")))
    year_range = f"{years[0]}–{years[-1]}" if len(years) > 1 else (str(years[0]) if years else "")
    count = len(papers)

    intro = (
        f"{concept} 是当前知识库中的一个核心概念，"
        f"在 {year_range} 间被 {count} 个相关条目所涉及。"
    )

    seed_papers = [p for p in papers if p.get("is_seed")]
    if seed_papers:
        seed_titles = "、".join(f"《{p['title'][:30]}》" for p in seed_papers[:2])
        role = f"作为 {concept} 相关内容的重要切入点，它在 {seed_titles} 等条目中被重点讨论，随后被更多内容继承和扩展。"
    else:
        role = f"{concept} 在多个条目中被反复提及，是当前知识库中的一个共同基础概念。"

    # Perspectives: list 3-5 papers with their abstract first sentence
    lines = []
    for p in papers[:5]:
        ab = (p.get("abstract") or "").strip()
        first_sentence = re.split(r"[.。]", ab)[0] if ab else ""
        if first_sentence:
            lines.append(f"- 《{p.get('title','')[:40]}》（{p.get('year','')}）：{first_sentence[:80]}")
    perspectives = "\n".join(lines) if lines else "（暂无摘要信息，需读全文后补充）"

    return intro, role, perspectives


# ---------------------------------------------------------------------------
# Topic page enrichment
# ---------------------------------------------------------------------------

def enrich_topic_page(
    page_path: Path,
    papers_in_topic: list[dict],
    force: bool,
    llm_provider: str,
    llm_model: str,
    do_compare: bool = True,
) -> bool:
    """Fill 核心贡献 column and 核心观点 / 研究脉络 sections for a topic page."""
    content = page_path.read_text(encoding="utf-8")

    core_section = _get_section(content, "核心观点")
    if not _is_stub(core_section) and not force:
        return False

    if not papers_in_topic:
        return False

    research_topic = all(_is_research_entry(p) for p in papers_in_topic)

    # Fill 核心贡献 in table rows
    def fill_contribution(match):
        row = match.group(0)
        if "（待填）" not in row:
            return row
        # Find which paper this row refers to by matching wikilink
        wl_match = re.search(r"\[\[([^\]]+)\]\]", row)
        if not wl_match:
            return row
        page_slug = wl_match.group(1)
        # Match slug to paper
        for p in papers_in_topic:
            if _paper_slug(p) in page_slug or page_slug in _paper_slug(p):
                contrib = _one_line_contribution(p)
                return row.replace("（待填）", contrib)
        return row

    new_content = re.sub(r"\|[^\n]+（待填）[^\n]+\|", fill_contribution, content)

    # Fill 核心观点 (cross-paper synthesis)
    if LLM_AVAILABLE:
        context = "\n".join(
            f"- {p.get('title','')} ({p.get('date') or p.get('published_at') or p.get('year') or '未知时间'}): {(p.get('abstract') or '')[:300]}"
            for p in sorted(papers_in_topic, key=lambda p: str(p.get("date") or p.get("published_at") or p.get("year") or ""))
        )
        topic_name = page_path.stem
        if research_topic:
            prompt = f"""你正在为个人知识库的主题页「{topic_name}」撰写综合分析。

该主题下按时间顺序收录了以下论文：
{context[:cfg("wiki", "topic_context_max", 5000)]}

请用中文完成以下两个字段，直接输出内容：

### 核心观点
3-4 句话，概括这个研究方向的共同认知：
- 这些论文都在解决什么核心问题？
- 它们共同的思路/范式是什么？
- 该方向与知识库中其他相邻主题有什么区别？

### 研究脉络
按时间线 3-4 句话，描述这个方向的演进：
- 最早的工作奠定了什么基础？
- 中间有什么关键的转折点或突破？
- 最新的工作把方向推向了哪里？
请尽量引用具体论文名称。

### 未解决的问题
列出 2-3 个该方向仍然开放的问题或挑战（从论文的 limitation/future work 中提炼）。
"""
        else:
            prompt = f"""你正在为个人知识库的主题页「{topic_name}」撰写综合分析。

该主题下按时间顺序收录了以下条目：
{context[:cfg("wiki", "topic_context_max", 5000)]}

请用中文完成以下三个字段，直接输出内容：

### 核心观点
3-4 句话，概括这个主题下条目的共同关注点：
- 这些条目主要在讨论什么问题、对象或主题？
- 它们之间有哪些互补、对比或递进关系？
- 该主题与知识库中其他相邻主题有什么区别？

### 研究脉络
按时间线 3-4 句话，描述这个主题下内容的演进：
- 最早的条目提供了什么背景或起点？
- 中间有哪些明显的变化、扩展或争议？
- 最新的条目把讨论推进到了哪里？
请尽量引用具体条目名称。

### 未解决的问题
列出 2-3 个仍值得继续补充、追踪或验证的问题，可以是资料空白、观点分歧、行动建议或后续线索。
"""
        try:
            raw = call_llm(prompt, provider=llm_provider, model=llm_model)
        except (subprocess.TimeoutExpired, RuntimeError, ValueError) as e:
            print(f"  [warn] LLM call failed for {page_path.name}: {e}. Skipping.", file=sys.stderr)
            return False

        if raw is None:
            print(f"  [skip] {page_path.name}: Waiting for direct-inference manual application.", file=sys.stderr)
            return False

        core_view = _parse_llm_section(raw, "核心观点")
        trajectory = _parse_llm_section(raw, "研究脉络")
        open_questions = _parse_llm_section(raw, "未解决的问题")
        # Also update open questions section
        new_content = _replace_section(new_content, "未解决的问题", open_questions + "\n")
    else:
        core_view, trajectory = _rule_based_topic_content(papers_in_topic, page_path.stem)

    new_content = _replace_section(new_content, "核心观点", core_view + "\n")
    new_content = _replace_section(new_content, "研究脉络", trajectory + "\n")

    # Fill / insert 对比分析 table
    compare_section = _get_section(new_content, "对比分析")
    need_compare = do_compare and (force or not compare_section or _is_stub(compare_section) or "（待填）" in compare_section)
    if need_compare:
        compare_table = _build_comparison_table(papers_in_topic, llm_provider, llm_model)
        if compare_table:
            if "## 对比分析" in new_content:
                new_content = _replace_section(new_content, "对比分析", compare_table + "\n")
            else:
                # Section doesn't exist yet — insert before 研究脉络
                insert_marker = "\n## 研究脉络"
                if insert_marker in new_content:
                    new_content = new_content.replace(
                        insert_marker,
                        f"\n## 对比分析\n\n{compare_table}\n{insert_marker}",
                        1,
                    )
                else:
                    new_content = new_content.rstrip() + f"\n\n## 对比分析\n\n{compare_table}\n"

    new_content = _update_frontmatter_date(new_content)

    new_content = _remove_missing_images(new_content, page_path)
    page_path.write_text(new_content, encoding="utf-8")
    return True


def _is_research_entry(entry: dict) -> bool:
    template_id = (entry.get("template_id") or "").strip().lower()
    entry_type = (entry.get("entry_type") or "").strip().lower()
    return template_id == "research_paper" or entry_type == "paper" or bool(entry.get("arxiv_id"))


def _build_comparison_table(
    papers: list[dict],
    llm_provider: str,
    llm_model: str,
) -> str:
    """Ask LLM to build a structured comparison table across entries in a topic."""
    if not papers or not LLM_AVAILABLE:
        return ""

    research_topic = all(_is_research_entry(p) for p in papers)

    paper_summaries = []
    for p in sorted(papers, key=lambda x: str(x.get("date") or x.get("published_at") or x.get("year") or "")):
        title = (p.get("title") or "?")[:60]
        when = p.get("date") or p.get("published_at") or p.get("year") or "?"
        abstract = (p.get("abstract") or "")[:400]
        owner = ", ".join((p.get("authors") or [])[:2]) or p.get("author") or p.get("account") or p.get("platform") or "未知来源"
        entry_kind = p.get("template_id") or p.get("entry_type") or p.get("source_kind") or "entry"
        paper_summaries.append(f"- **{title}** ({when}, {owner}, {entry_kind}): {abstract}")

    context = "\n".join(paper_summaries)
    if research_topic:
        prompt = f"""你正在为个人知识库的主题页撰写对比分析表格。

以下是该主题下的论文列表（按年份排序）：
{context[:cfg("wiki", "compare_table_context_max", 6000)]}

请生成一个 Markdown 表格，对比这些论文在以下维度的异同。
每行一篇论文，按年份从早到晚排序。

表格列：
| 论文（简称） | 年份 | 核心架构 | 训练目标 | 数据要求 | 核心贡献（一句话） | 主要局限 |

要求：
- 论文简称：用缩写或 3-4 个词（如 "I-JEPA"、"LDA-1B"、"ThinkJEPA"）
- 核心架构：模型结构的核心设计词（如 "ViT + EMA predictor"、"Diffusion Transformer"）
- 训练目标：损失函数或学习任务（如 "masked patch prediction"、"forward dynamics + policy"）
- 数据要求：对标注/数据类型的依赖（如 "无标注图像"、"动作标注轨迹"、"异构视频+轨迹"）
- 核心贡献：15 字以内的中文一句话
- 主要局限：10 字以内的中文

只输出 Markdown 表格，不要其他说明文字。"""
    else:
        prompt = f"""你正在为个人知识库的主题页撰写对比分析表格。

以下是该主题下的条目列表（按时间排序）：
{context[:cfg("wiki", "compare_table_context_max", 6000)]}

请生成一个 Markdown 表格，对比这些条目在以下维度的异同。
每行一个条目，按时间从早到晚排序。

表格列：
| 条目 | 时间 | 来源类型 | 核心关注点 | 关键信息 | 备注 |

要求：
- 条目：使用简短名称，控制在 3-6 个词以内
- 时间：直接使用条目中可确认的时间
- 来源类型：如 研究论文 / 网页文章 / 通用条目 / GitHub / 视频
- 核心关注点：该条目主要讨论的问题、对象或主题
- 关键信息：15 字以内总结最值得保留的信息
- 备注：10 字以内写使用限制、场景或补充说明

只输出 Markdown 表格，不要其他说明文字。"""

    try:
        raw = call_llm(prompt, provider=llm_provider, model=llm_model)
        # Extract just the table lines
        lines = raw.strip().split("\n")
        table_lines = [l for l in lines if l.strip().startswith("|")]
        if len(table_lines) >= 3:  # header + separator + at least one row
            return "\n".join(table_lines)
    except Exception as e:
        print(f"  [warn] comparison table LLM call failed: {e}", file=sys.stderr)

    return ""


def generate_survey(
    wiki_dir: Path,
    papers: list[dict],
    topic_name: str,
    llm_provider: str,
    llm_model: str,
) -> None:
    """
    Generate wiki/survey.md — a top-down narrative synthesizing all topics.

    Structure:
    - 全局综述 (overview paragraph)
    - One section per topic with: narrative paragraph + key insight
    - 跨主题洞察 (cross-topic synthesis)
    - 未来方向 (open questions across all topics)
    """
    topics_dir = wiki_dir / "wiki" / "topics"
    sources_dir = wiki_dir / "wiki" / "sources"
    survey_path = wiki_dir / "wiki" / "survey.md"

    if not topics_dir.exists():
        print("[survey] no topics directory found, skipping", file=sys.stderr)
        return

    # Collect per-topic content from topic pages
    topic_contents: list[tuple[str, str, str]] = []  # (topic_name, 核心观点, 研究脉络)
    for tp in sorted(topics_dir.glob("*.md")):
        content = tp.read_text(encoding="utf-8")
        core = _get_section(content, "核心观点").strip()
        traj = _get_section(content, "研究脉络").strip()
        questions = _get_section(content, "未解决的问题").strip()
        if core and not _is_stub(core):
            topic_contents.append((tp.stem, core, traj, questions))

    if not topic_contents:
        print("[survey] no enriched topic pages found, run --only-topics first", file=sys.stderr)
        return

    # Build context for LLM
    topic_context = "\n\n".join(
        f"### {name}\n**核心观点**: {core}\n**研究脉络**: {traj}"
        for name, core, traj, _ in topic_contents
    )
    all_questions = "\n".join(
        f"- [{name}] {q}" for name, _, _, q in topic_contents if q and not _is_stub(q)
    )
    paper_count = len(papers)
    research_wiki = bool(papers) and all(_is_research_entry(p) for p in papers)
    seed_titles = [p.get("title", "") for p in papers if p.get("is_seed")]
    if research_wiki:
        prompt = f"""你正在为一个关于「{topic_name}」的研究 wiki 撰写全局综述文档 survey.md。

这个 wiki 收录了 {paper_count} 篇论文，分为以下主题：
{topic_context[:cfg("wiki", "survey_topic_context_max", 8000)]}

各主题的未解决问题：
{all_questions[:cfg("wiki", "survey_questions_max", 2000)]}

核心种子论文：{', '.join(seed_titles[:6])}

请生成一篇结构化的综述，格式如下（用中文，直接输出 Markdown）：

## 概览

（3-4 句话：这个研究领域在解决什么根本问题？为什么现在值得关注？整体研究格局是什么？）

## 研究全景

（2-3 句话：这 {paper_count} 篇论文覆盖了哪些维度？各主题之间的关系是什么？）

## 各方向解读

（为每个主题写一段 3-4 句话的叙事段落，以「**主题名**：」开头，描述该方向的核心洞察和代表性进展）

## 跨主题洞察

（3-5 个 bullet points，从「问题→方案→局限」视角，提炼跨越多个主题的核心模式或张力。例如：潜空间预测 vs 像素重建的权衡在多个方向反复出现）

## 未来方向

（综合各主题的开放问题，列出 3-5 个最值得攻克的研究方向，每条说明为什么重要）
"""
    else:
        prompt = f"""你正在为一个关于「{topic_name}」的个人知识库撰写全局综述文档 survey.md。

这个知识库收录了 {paper_count} 个条目，分为以下主题：
{topic_context[:cfg("wiki", "survey_topic_context_max", 8000)]}

各主题的未解决问题：
{all_questions[:cfg("wiki", "survey_questions_max", 2000)]}

重要种子条目：{', '.join(seed_titles[:6]) or '（暂无）'}

请生成一篇结构化的综述，格式如下（用中文，直接输出 Markdown）：

## 概览

（3-4 句话：这个知识库当前主要覆盖哪些问题域、资料类型或关注方向？为什么这些内容值得持续维护？）

## 知识全景

（2-3 句话：这 {paper_count} 个条目覆盖了哪些维度？各主题之间的关系是什么？）

## 各方向解读

（为每个主题写一段 3-4 句话的叙事段落，以「**主题名**：」开头，描述该方向的核心内容、代表性条目和主要价值）

## 跨主题洞察

（3-5 个 bullet points，总结跨越多个主题反复出现的模式、张力、共识、争议或行动线索）

## 后续方向

（综合各主题的开放问题，列出 3-5 个最值得继续补充、验证、追踪或整理的方向，每条说明为什么重要）
"""

    try:
        raw = call_llm(prompt, provider=llm_provider, model=llm_model)
    except Exception as e:
        print(f"[survey] LLM call failed: {e}", file=sys.stderr)
        return

    from datetime import date
    today = date.today().isoformat()

    # Build related pages list from actual topic files
    related_links = ["- [[index]]"]
    for tp in sorted(topics_dir.glob("*.md")):
        related_links.append(f"- [[{tp.stem}]]")
    related_pages = "\n".join(related_links)

    content = f"""---
tags: [综述, {topic_name}]
created: {today}
updated: {today}
---

# {topic_name} {"研究综述" if research_wiki else "知识综述"}

> 自动生成的全局叙事综述，基于 {paper_count} {"篇论文" if research_wiki else "个条目"} 的 wiki 内容。手工编辑的内容请在 `## {"研究全景" if research_wiki else "知识全景"}` 以下各节添加。

{raw.strip()}

---

## 相关页面

{related_pages}
"""

    survey_path.write_text(content, encoding="utf-8")
    print(f"[survey] Written to {survey_path}")


def _one_line_contribution(paper: dict) -> str:
    """Generate a one-line contribution description from abstract (rule-based)."""
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
    title = paper.get("title") or ""
    # Try "we propose/introduce X" sentence
    m = re.search(r"[Ww]e (propose|introduce|present|develop|design)[^.]{10,80}", abstract)
    if m:
        return m.group(0).strip()[:60]
    summary = _first_summary_sentence()
    if summary:
        return summary
    if not _is_research_entry(paper):
        return f"围绕《{title[:20] or '该条目'}》整理资料与线索"
    # Fallback: domain hint from title
    return f"聚焦于{_domain_hint(title)}相关问题"


def _rule_based_topic_content(papers: list[dict], topic_name: str) -> tuple[str, str]:
    """Rule-based topic synthesis."""
    research_topic = bool(papers) and all(_is_research_entry(p) for p in papers)
    years_papers = sorted(
        [
            (str(p.get("date") or p.get("published_at") or p.get("year") or ""), p)
            for p in papers
            if p.get("date") or p.get("published_at") or p.get("year")
        ],
        key=lambda x: x[0],
    )
    count = len(papers)
    year_range = f"{years_papers[0][0]}–{years_papers[-1][0]}" if years_papers else ""

    if research_topic:
        core_view = (
            f"「{topic_name}」方向共收录 {count} 篇论文（{year_range}），"
            f"共同围绕这一主题展开，但切入角度、方法设计与应用场景各不相同。"
            f"这些条目一起展示了该主题从基础概念到具体应用的展开方式。"
        )
    else:
        core_view = (
            f"「{topic_name}」主题共收录 {count} 个条目（{year_range}），"
            f"共同围绕相近的问题域、对象或资料线索展开。"
            f"这些条目一起构成了该主题从背景资料到具体案例的知识脉络。"
        )

    if len(years_papers) >= 2:
        earliest = years_papers[0][1]
        latest = years_papers[-1][1]
        if research_topic:
            trajectory = (
                f"该方向研究始于 {years_papers[0][0]} 年的《{earliest.get('title','')[:30]}》，"
                f"到 {years_papers[-1][0]} 年已发展至《{latest.get('title','')[:30]}》等工作，"
                f"整体呈现从基础方法到多样化应用的演进趋势。"
            )
        else:
            trajectory = (
                f"该主题最早可追溯到《{earliest.get('title','')[:30]}》所提供的背景或起点，"
                f"到《{latest.get('title','')[:30]}》等较新条目时，内容已扩展到新的案例、视角或应用。"
            )
    else:
        trajectory = "（条目数量不足，难以梳理演进脉络）"

    return core_view, trajectory


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_papers(papers_path: Path) -> list[dict]:
    return load_entries(papers_path)


# ---------------------------------------------------------------------------
# classify_papers — LLM assigns category + concepts, writes back to papers.json
# ---------------------------------------------------------------------------

def _build_taxonomy_hints(wiki_dir: Path) -> tuple[str, str]:
    """
    Dynamically build topic and concept hints from the wiki directory.
    Topics come from wiki/wiki/topics/*.md (name + blockquote description).
    Concepts come from wiki/wiki/entities/*.md filenames.
    Falls back to minimal hardcoded defaults if directories don't exist yet.
    """
    FALLBACK_CATEGORY = cfg("wiki", "fallback_category", "其他")

    # --- Topics: read from topics/*.md ---
    topics_dir = wiki_dir / "wiki" / "topics"
    topic_lines = []
    if topics_dir.exists():
        for tp in sorted(topics_dir.glob("*.md")):
            name = tp.stem
            # Extract description from first blockquote line
            content = tp.read_text(encoding="utf-8", errors="ignore")
            desc_match = re.search(r"^>\s*(.+)", content, re.MULTILINE)
            desc = desc_match.group(1).strip() if desc_match else ""
            topic_lines.append(f"- {name}：{desc}" if desc else f"- {name}")
    if not topic_lines:
        topic_lines = [f"- {FALLBACK_CATEGORY}：默认分类"]

    topic_hint = "可用分类（从中选一个最合适的，必须与列表中的名称完全一致）：\n" + "\n".join(topic_lines)

    # --- Concepts: read entity filenames ---
    entities_dir = wiki_dir / "wiki" / "entities"
    concept_names = []
    if entities_dir.exists():
        concept_names = [f.stem for f in sorted(entities_dir.glob("*.md"))]
    if not concept_names:
        concept_names = ["系统设计", "方法论", "工具链"]

    concept_hint = "常见概念（从中选 2-5 个最相关的，也可提出列表之外的概念）：\n" + ", ".join(concept_names)

    return topic_hint, concept_hint


def classify_papers(
    papers: list[dict],
    papers_path: Path,
    wiki_dir: Path,
    provider: str = "auto",
    model: str = "claude-opus-4-7",
    force: bool = False,
) -> int:
    """
    For each paper missing 'category' or 'concepts', call LLM to classify.
    Topics and concepts are read dynamically from wiki/topics/ and wiki/entities/.
    Writes results back to papers.json. Returns number of papers classified.
    """
    to_classify = [
        p for p in papers
        if force or not p.get("category") or not p.get("concepts")
    ]
    if not to_classify:
        print("[classify] All papers already classified.", file=sys.stderr)
        return 0

    # Build hints dynamically from the wiki directory (no hardcoding)
    topic_hint, concept_hint = _build_taxonomy_hints(wiki_dir)

    print(f"[classify] Classifying {len(to_classify)} papers...", file=sys.stderr)
    classified = 0

    for paper in to_classify:
        title = paper.get("title") or ""
        abstract = (paper.get("abstract") or "")[:cfg("wiki", "source_abstract_truncate", 800)]
        aid = paper.get("arxiv_id") or ""

        prompt = f"""你正在维护一个个人知识库。
请根据以下论文信息，为这篇论文分配：
1. category：从可用分类中选一个最合适的（必须与列表完全一致）
2. concepts：选 2-5 个最相关的关键概念（尽量从提示列表中选，也可补充列表外的）

{topic_hint}

{concept_hint}

论文信息：
标题：{title}
摘要：{abstract}

请严格按以下 JSON 格式输出，不要输出其他内容：
{{
  "category": "...",
  "concepts": ["概念1", "概念2", ...]
}}"""

        raw = call_llm(prompt, provider=provider, model=model)
        if not raw:
            print(f"  [skip] {aid} {title[:40]}: LLM returned empty", file=sys.stderr)
            continue

        # Parse JSON from response
        json_match = re.search(r"\{[\s\S]*?\}", raw)
        if not json_match:
            print(f"  [skip] {aid} {title[:40]}: could not parse JSON", file=sys.stderr)
            continue
        try:
            result = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            print(f"  [skip] {aid} {title[:40]}: invalid JSON", file=sys.stderr)
            continue

        cat = result.get("category", "").strip()
        concepts = result.get("concepts", [])

        if not cat or not concepts:
            print(f"  [skip] {aid} {title[:40]}: missing fields in LLM response", file=sys.stderr)
            continue

        paper["category"] = cat
        paper["concepts"] = concepts
        classified += 1
        print(f"  [ok]   {aid} → {cat} | {concepts}", file=sys.stderr)

    if classified:
        with open(papers_path, "w", encoding="utf-8") as f:
            json.dump(papers, f, ensure_ascii=False, indent=2)
        print(f"[classify] {classified} papers classified and saved to {papers_path}", file=sys.stderr)

        # Sync category back to source page frontmatters so topic page builder can read them
        sources_dir = wiki_dir / "wiki" / "sources"
        if sources_dir.exists():
            papers_by_arxiv_local = {p["arxiv_id"]: p for p in papers if p.get("arxiv_id")}
            papers_by_slug_local = {_paper_slug(p): p for p in papers}
            # also index by title-only slug for non-arXiv papers
            for p in papers:
                title = p.get("title") or ""
                slug = re.sub(r"[^\w\s-]", "", title.lower())
                slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:60]
                papers_by_slug_local.setdefault(slug, p)
            synced = 0
            for page_path in sources_dir.glob("*.md"):
                # Try arXiv ID prefix first
                arxiv_m = re.match(r"^(\d{4}\.\d{4,5})", page_path.name)
                if arxiv_m:
                    paper = papers_by_arxiv_local.get(arxiv_m.group(1))
                else:
                    # Non-arXiv: match by stem slug
                    stem = page_path.stem
                    paper = papers_by_slug_local.get(stem) or next(
                        (p for slug, p in papers_by_slug_local.items()
                         if slug and (slug in stem or stem in slug)), None
                    )
                if not paper or not paper.get("category"):
                    continue
                content = page_path.read_text(encoding="utf-8")
                new_content = re.sub(
                    r"^category:.*$",
                    f"category: {paper['category']}",
                    content,
                    flags=re.MULTILINE,
                )
                if new_content != content:
                    new_content = _remove_missing_images(new_content, page_path)
                    page_path.write_text(new_content, encoding="utf-8")
                    synced += 1
            if synced:
                print(f"[classify] synced category to {synced} source pages", file=sys.stderr)

    return classified


def _papers_by_category(papers: list[dict]) -> dict[str, list[dict]]:
    cat_map: dict[str, list[dict]] = {}
    for p in papers:
        cat = p.get("category", "其他")
        cat_map.setdefault(cat, []).append(p)
    return cat_map


def _papers_for_concept(concept_name: str, all_papers: list[dict]) -> list[dict]:
    """Find papers that mention the concept in title, abstract, keywords, or authors."""
    keywords = concept_name.lower().split()
    results = []
    for p in all_papers:
        text = " ".join([
            p.get("title") or "",
            p.get("abstract") or "",
            " ".join(p.get("keywords") or []),
            " ".join(p.get("authors") or []),
            p.get("affiliation") or "",
        ]).lower()
        if all(kw in text for kw in keywords):
            results.append(p)
    # For concepts with no results, try any-word match (e.g. "Meta AI" → papers from Meta)
    if not results and len(keywords) > 1:
        for p in all_papers:
            text = " ".join([
                p.get("title") or "",
                p.get("abstract") or "",
                " ".join(p.get("authors") or []),
            ]).lower()
            if any(kw in text for kw in keywords if len(kw) > 3):
                results.append(p)
    return results


def main():
    parser = argparse.ArgumentParser(description="Enrich llm-wiki knowledge base with source entry content")
    parser.add_argument("--wiki-dir", required=True, help="Path to wiki root directory")
    add_entries_argument(parser, required=True)
    parser.add_argument("--template-dir", default=None,
                        help="Directory containing editable page templates (default: repo templates/)")
    parser.add_argument("--pdf-dir", help="Path to directory containing downloaded PDFs")
    parser.add_argument("--download-pdfs", action="store_true",
                        help="Auto-download missing PDFs from arXiv before enriching (requires --pdf-dir)")
    parser.add_argument("--only-sources", action="store_true", help="Only enrich source pages")
    parser.add_argument("--only-entities", action="store_true", help="Only enrich entity pages")
    parser.add_argument("--only-topics", action="store_true", help="Only enrich topic pages")
    parser.add_argument("--force", action="store_true", help="Re-enrich already-filled pages")
    parser.add_argument("--search-non-arxiv", action="store_true",
                        help="Search web (Bing/DDG) for PDF URLs of non-arXiv papers during --download-pdfs")
    parser.add_argument("--figures", action="store_true",
                        help="Download paper figures from ar5iv and insert 论文图表 section into source pages")
    parser.add_argument("--figures-dir",
                        help="Directory to store downloaded figures (default: <wiki-dir>/figures)")
    parser.add_argument("--media", action="store_true",
                        help="Download GIFs from GitHub README and YouTube video thumbnails, insert 演示与视频 section")
    parser.add_argument("--media-dir",
                        help="Directory to store downloaded media (default: <wiki-dir>/media)")
    parser.add_argument("--classify", action="store_true",
                        help="Use LLM to assign category + concepts to entries missing these fields, then write back to the entries file")
    parser.add_argument("--fix-backlinks", action="store_true",
                        help="Scan all source pages and add missing reverse links to the relation section")
    parser.add_argument("--web-resources", action="store_true",
                        help="Search web for related blog posts, YouTube videos, GitHub projects and insert 互联网资源 section")
    parser.add_argument("--survey", action="store_true",
                        help="Generate wiki/survey.md: a top-down narrative synthesizing all topics")
    parser.add_argument("--page-slug", help="Process only a specific source page slug")
    parser.add_argument("--compare", action="store_true",
                        help="Generate 对比分析 comparison table in topic pages (requires --only-topics or combined run)")
    parser.add_argument("--topic", default=cfg("wiki", "topic", "Personal Wiki"),
                        help="Wiki topic name used in survey.md title")
    parser.add_argument("--post-ingest", action="store_true",
                        help="After source enrichment, automatically enrich topic pages "
                             "(核心观点/对比分析/研究脉络) and regenerate survey.md. "
                             "Implies --compare and --survey. Use this at the end of every ingest.")
    parser.add_argument("--direct-input", help="File path containing raw LLM output to bypass the direct-inference pause.")
    git_group = parser.add_mutually_exclusive_group()
    git_group.add_argument("--git-commit", dest="git_commit", action="store_true",
                           help="Create a git commit after writing files")
    git_group.add_argument("--no-git-commit", dest="git_commit", action="store_false",
                           help="Disable git commit for this run")
    parser.set_defaults(git_commit=None)
    # LLM options
    parser.add_argument("--llm-provider", default=cfg("llm", "provider", "auto"),
                        choices=["auto", "direct-inference", "anthropic", "openai", "ollama"],
                        help="LLM provider: auto, direct-inference, anthropic, openai, ollama")
    parser.add_argument("--llm-model", default=cfg("llm", "model", ""),
                        help="Model name for the LLM provider")
    args = parser.parse_args()
    print(
        f"[llm] provider: {describe_provider_selection(args.llm_provider, allowed={'direct-inference', 'anthropic', 'openai', 'ollama'})}",
        file=sys.stderr,
        flush=True,
    )

    wiki_dir = Path(args.wiki_dir)
    entries_path = Path(args.entries_path)
    sources_dir = wiki_dir / "wiki" / "sources"
    entities_dir = wiki_dir / "wiki" / "entities"
    topics_dir = wiki_dir / "wiki" / "topics"
    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else None
    figures_dir = Path(args.figures_dir) if args.figures_dir else (wiki_dir / "figures") if args.figures else None
    media_dir = Path(args.media_dir) if args.media_dir else (wiki_dir / "media") if args.media else None
    should_git_commit = args.git_commit if args.git_commit is not None else cfg("git", "auto_commit", False)
    touched_files: list[Path] = []

    if not entries_path.exists():
        print(f"[error] entries file not found: {entries_path}", file=sys.stderr)
        sys.exit(1)

    # Auto-organize images before starting enrichment
    organize_pasted_images(wiki_dir, fix=True)

    papers = _load_papers(entries_path)
    papers_by_arxiv = {p["arxiv_id"]: p for p in papers if p.get("arxiv_id")}

    # --- Auto-download PDFs if requested ---
    if args.download_pdfs:
        if not pdf_dir:
            print("[error] --download-pdfs requires --pdf-dir", file=sys.stderr)
            sys.exit(1)
        batch_download_pdfs(papers, pdf_dir, search_non_arxiv=args.search_non_arxiv)
        # Persist any pdf_url/local_pdf updates back to the entries store
        save_entries(papers, entries_path)
        touched_files.append(entries_path)

    # --- Classify papers (assign category + concepts to papers.json) ---
    if args.classify:
        classify_papers(
            papers, entries_path, wiki_dir,
            provider=args.llm_provider, model=args.llm_model,
            force=args.force,
        )
        touched_files.append(entries_path)
        # Reload after write-back so downstream steps see updated fields
        papers = _load_papers(entries_path)
        papers_by_arxiv = {p["arxiv_id"]: p for p in papers if p.get("arxiv_id")}

    do_sources = not (args.only_entities or args.only_topics)
    do_entities = not (args.only_sources or args.only_topics)
    # --post-ingest: after source enrichment, also enrich topics + survey automatically
    post_ingest = getattr(args, "post_ingest", False)
    if post_ingest:
        args.survey = True
        args.compare = True
    do_topics = not (args.only_sources or args.only_entities) or post_ingest

    stats = {"sources": 0, "entities": 0, "topics": 0}

    # --- Enrich source pages ---
    if do_sources and sources_dir.exists():
        source_files = list(sources_dir.glob("*.md"))
        if args.page_slug:
            source_files = [f for f in source_files if f.stem == args.page_slug]
            if not source_files:
                print(f"[sources] No source page found for slug: {args.page_slug}", file=sys.stderr)
        
        print(f"[sources] Found {len(source_files)} source pages")
        for page_path in source_files:
            # Match page to paper via arxiv_id in filename
            arxiv_match = re.match(r"^(\d{4}\.\d{4,5})", page_path.name)
            arxiv_id = arxiv_match.group(1) if arxiv_match else ""
            paper = papers_by_arxiv.get(arxiv_id)
            if not paper:
                # Try matching by title slug (with or without arXiv prefix)
                def _title_slug_only(p: dict) -> str:
                    title = p.get("title") or ""
                    slug = re.sub(r"[^\w\s-]", "", title.lower())
                    return re.sub(r"[\s_]+", "-", slug).strip("-")[:60]
                paper = next(
                    (p for p in papers if _paper_slug(p) in page_path.stem
                     or _title_slug_only(p) in page_path.stem),
                    None
                )
            if not paper:
                print(f"  [skip] {page_path.name}: no matching entry in {entries_file_label(entries_path)}")
                continue
            modified = enrich_source_page(
                page_path, paper, pdf_dir, papers, args.force,
                args.llm_provider, args.llm_model,
                figures_dir=figures_dir,
                media_dir=media_dir,
                web_resources=args.web_resources,
                direct_input=args.direct_input,
                template_dir=args.template_dir,
            )
            if modified:
                stats["sources"] += 1
                touched_files.append(page_path)
                print(f"  [ok]   {page_path.name}")

    # --- Enrich entity pages ---
    if do_entities and entities_dir.exists():
        entity_files = list(entities_dir.glob("*.md"))
        print(f"[entities] Found {len(entity_files)} entity pages")
        for page_path in entity_files:
            concept_name = page_path.stem.replace("-", " ").title()
            concept_papers = _papers_for_concept(concept_name, papers)
            modified = enrich_entity_page(
                page_path, concept_papers, concept_name, args.force,
                args.llm_provider, args.llm_model,
            )
            if modified:
                stats["entities"] += 1
                touched_files.append(page_path)
                print(f"  [ok]   {page_path.name}")

    # --- Enrich topic pages ---
    if do_topics and topics_dir.exists():
        topic_files = list(topics_dir.glob("*.md"))
        print(f"[topics] Found {len(topic_files)} topic pages")
        # Build category → papers map (read category from source page frontmatter)
        cat_map: dict[str, list[dict]] = {}
        papers_by_slug = {_paper_slug(p): p for p in papers}
        for page_path in sources_dir.glob("*.md") if sources_dir.exists() else []:
            fc = page_path.read_text(encoding="utf-8")
            cm = re.search(r"^category:\s*(.+)$", fc, re.MULTILINE)
            arxiv_m = re.match(r"^(\d{4}\.\d{4,5})", page_path.name)
            arxiv_id = arxiv_m.group(1) if arxiv_m else ""
            paper = papers_by_arxiv.get(arxiv_id)
            # Also match non-arxiv papers by slug
            if not paper:
                paper = papers_by_slug.get(page_path.stem) or next(
                    (p for p in papers if _paper_slug(p) in page_path.stem or page_path.stem in _paper_slug(p)),
                    None
                )
            if paper and cm:
                cat = cm.group(1).strip()
                cat_map.setdefault(cat, []).append(paper)

        def _normalize(s):
            return re.sub(r"[&\s_-]", "", s).lower()

        norm_cat_map = {_normalize(k): v for k, v in cat_map.items()}

        for page_path in topic_files:
            topic_name = page_path.stem
            # Try exact match first, then normalized match
            topic_papers = cat_map.get(topic_name) or norm_cat_map.get(_normalize(topic_name), [])
            modified = enrich_topic_page(
                page_path, topic_papers, args.force,
                args.llm_provider, args.llm_model,
                do_compare=getattr(args, "compare", True),
            )
            if modified:
                stats["topics"] += 1
                touched_files.append(page_path)
                print(f"  [ok]   {page_path.name}")

    print(f"\nDone. Enriched: {stats['sources']} source pages, "
          f"{stats['entities']} entity pages, {stats['topics']} topic pages.")

    # --- Fix backlinks (bidirectional 与其他论文的关联) ---
    if args.fix_backlinks and sources_dir.exists():
        fixed, fixed_paths = _fix_backlinks(sources_dir)
        touched_files.extend(fixed_paths)
        print(f"[backlinks] Added {fixed} missing reverse links.")

    # --- Generate survey.md (runs after topics are enriched so content is available) ---
    if args.survey:
        wiki_topic = getattr(args, "topic", None) or cfg("wiki", "topic", "Personal Wiki")
        generate_survey(wiki_dir, papers, wiki_topic, args.llm_provider, args.llm_model)
        touched_files.append(wiki_dir / "survey.md")

    # Append to log.md
    log_path = wiki_dir / "log.md"
    if log_path.exists():
        log_content = log_path.read_text(encoding="utf-8")
        entry = (
            f"\n## {TODAY} enrich | 批量填充占位符内容\n\n"
            f"- 更新素材摘要页：{stats['sources']}\n"
            f"- 更新实体页：{stats['entities']}\n"
            f"- 更新主题页：{stats['topics']}\n"
            f"- LLM 模式：{'开启 (' + args.llm_provider + '/' + args.llm_model + ')' if LLM_AVAILABLE else '关闭（规则模式）'}\n"
            f"- 操作类型：enrich\n\n---\n"
        )
        log_path.write_text(log_content + entry, encoding="utf-8")
        touched_files.append(log_path)

    # --- Optional git commit scoped to touched files only ---
    if should_git_commit:
        slug = getattr(args, "page_slug", None)
        if slug:
            msg = f"enrich: {slug}"
        else:
            msg = (f"enrich: {stats['sources']} source pages, "
                   f"{stats['entities']} entity pages, {stats['topics']} topic pages")
        committed, detail = git_commit_paths(wiki_dir, touched_files, msg)
        if committed:
            print(f"[git] committed: {detail}")
        else:
            print(f"[git] commit skipped: {detail}")


if __name__ == "__main__":
    main()
