#!/usr/bin/env python3
"""
Download paper PDF and optional project resources (GitHub README, project page).

Usage:
    python download_paper.py --arxiv-id 2301.08243 --output-dir ./papers
    python download_paper.py --arxiv-id 2301.08243 --output-dir ./papers --with-project

Output:
    - <output_dir>/<arxiv_id>.pdf
    - <output_dir>/<arxiv_id>_info.json   (metadata)
    - <output_dir>/<arxiv_id>_project.md  (project page / GitHub README, if --with-project)
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# SSL context that skips certificate verification (needed in some server environments)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _fetch_text(url: str, timeout: int = 20) -> str | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  [WARN] fetch failed {url}: {e}", file=sys.stderr)
        return None


def _fetch_binary(url: str, out_path: str, timeout: int = 120) -> bool:
    """Download binary file. Tries urllib first, falls back to curl."""
    # Try urllib with SSL verification disabled
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            data = r.read()
        Path(out_path).write_bytes(data)
        return True
    except Exception as e:
        print(f"  [WARN] urllib download failed: {e}, trying curl...", file=sys.stderr)

    # Fallback: curl (handles redirects and slow connections better)
    try:
        result = subprocess.run(
            ["curl", "-L", "--max-time", str(timeout), "-o", out_path,
             "--user-agent", HEADERS["User-Agent"], "-k",  # -k = skip SSL verify
             url],
            capture_output=True, timeout=timeout + 10
        )
        if result.returncode == 0 and Path(out_path).exists() and Path(out_path).stat().st_size > 1024:
            return True
        print(f"  [WARN] curl failed: {result.stderr.decode()[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] curl failed: {e}", file=sys.stderr)
    return False


def _fetch_arxiv_metadata_html(clean_id: str) -> dict:
    """Fallback to parsing HTML when API rate limits (429) occur."""
    url = f"https://arxiv.org/abs/{clean_id}"
    html = _fetch_text(url, timeout=15)
    if not html:
        return {}
    
    try:
        title_m = re.search(r'<meta name="citation_title" content="(.*?)"/>', html)
        title = title_m.group(1) if title_m else ""
        
        authors = re.findall(r'<meta name="citation_author" content="(.*?)"/>', html)
        
        abs_m = re.search(r'<meta name="citation_abstract" content="(.*?)"/>', html, re.DOTALL)
        abstract = abs_m.group(1).replace('\n', ' ').strip() if abs_m else ""
        
        date_m = re.search(r'<meta name="citation_date" content="([^"]+)"/>', html)
        published = date_m.group(1).replace('/', '-') if date_m else ""
        
        return {
            "arxiv_id": clean_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "published": published,
            "updated": published,
            "categories": [],
            "primary_category": "",
            "project_urls": [],
            "arxiv_url": f"https://arxiv.org/abs/{clean_id}",
            "pdf_url": f"https://arxiv.org/pdf/{clean_id}",
        }
    except Exception as e:
        print(f"  [WARN] HTML parse error: {e}", file=sys.stderr)
        return {}


def fetch_arxiv_metadata(arxiv_id: str) -> dict:
    """Get paper metadata from arXiv API."""
    clean_id = re.sub(r"v\d+$", "", arxiv_id.strip())
    # Try HTTPS first, fall back to HTTP if SSL handshake fails
    xml = None
    for scheme in ["https", "http"]:
        url = f"{scheme}://export.arxiv.org/api/query?id_list={clean_id}&max_results=1"
        xml = _fetch_text(url, timeout=15)
        if xml:
            break
            
    if not xml or "<entry>" not in xml:
        print("  [WARN] API request failed or empty (possibly 429 Rate Limit). Falling back to HTML scraping...", file=sys.stderr)
        return _fetch_arxiv_metadata_html(clean_id)

    import xml.etree.ElementTree as ET
    ns = {"atom": "http://www.w3.org/2005/Atom",
          "arxiv": "http://arxiv.org/schemas/atom"}
    try:
        root = ET.fromstring(xml)
        entry = root.find("atom:entry", ns)
        if entry is None:
            return {}
        title = (entry.findtext("atom:title", namespaces=ns) or "").replace("\n", " ").strip()
        abstract = (entry.findtext("atom:summary", namespaces=ns) or "").replace("\n", " ").strip()
        authors = [a.findtext("atom:name", namespaces=ns) or "" for a in entry.findall("atom:author", ns)]
        published = (entry.findtext("atom:published", namespaces=ns) or "")[:10]
        updated = (entry.findtext("atom:updated", namespaces=ns) or "")[:10]
        categories = [c.get("term", "") for c in entry.findall("atom:category", ns)]

        # Extract project URL from abstract or comment
        comment_el = entry.find("arxiv:comment", ns)
        comment = comment_el.text if comment_el is not None else ""
        text_for_urls = abstract + " " + (comment or "")
        url_pattern = re.compile(r"https?://(?:github\.com|[a-z0-9.-]+\.[a-z]{2,})/[^\s,;\"'<>]+", re.I)
        project_urls = list(dict.fromkeys(url_pattern.findall(text_for_urls)))
        # Filter out arxiv itself
        project_urls = [u for u in project_urls if "arxiv.org" not in u]

        return {
            "arxiv_id": clean_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "published": published,
            "updated": updated,
            "categories": categories,
            "primary_category": categories[0] if categories else "",
            "project_urls": project_urls[:3],
            "arxiv_url": f"https://arxiv.org/abs/{clean_id}",
            "pdf_url": f"https://arxiv.org/pdf/{clean_id}",
        }
    except ET.ParseError as e:
        print(f"  [WARN] XML parse error: {e}", file=sys.stderr)
        return {}


def download_pdf(arxiv_id: str, out_path: str) -> bool:
    clean_id = re.sub(r"v\d+$", "", arxiv_id.strip())
    for scheme in ["https", "http"]:
        pdf_url = f"{scheme}://arxiv.org/pdf/{clean_id}"
        print(f"  Downloading PDF from {pdf_url} ...", file=sys.stderr)
        ok = _fetch_binary(pdf_url, out_path)
        if ok:
            size_kb = Path(out_path).stat().st_size // 1024
            print(f"  ✓ PDF saved: {out_path} ({size_kb} KB)", file=sys.stderr)
            _extract_pdf_text(out_path)
            return True
    return False


def _extract_pdf_text(pdf_path: str) -> None:
    """Extract full text from PDF using pdftotext, save as <pdf_path>.txt.

    The .txt cache avoids re-parsing the PDF on every query — use grep on the
    .txt file to locate relevant passages rather than re-reading the PDF.
    """
    txt_path = re.sub(r"\.pdf$", ".txt", pdf_path)
    if Path(txt_path).exists():
        print(f"  ✓ Text cache already exists: {txt_path}", file=sys.stderr)
        return
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, txt_path],
            capture_output=True, timeout=60
        )
        if result.returncode == 0 and Path(txt_path).exists():
            size_kb = Path(txt_path).stat().st_size // 1024
            print(f"  ✓ Text cache saved: {txt_path} ({size_kb} KB)", file=sys.stderr)
        else:
            print(f"  [WARN] pdftotext failed: {result.stderr.decode()[:200]}", file=sys.stderr)
    except FileNotFoundError:
        print(f"  [WARN] pdftotext not found — trying pypdf fallback...", file=sys.stderr)
        _extract_pdf_text_pypdf(pdf_path, txt_path)
    except Exception as e:
        print(f"  [WARN] text extraction failed: {e}", file=sys.stderr)


def _extract_pdf_text_pypdf(pdf_path: str, txt_path: str) -> None:
    """Fallback: Extract text using pypdf library."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n\n"
        
        if text.strip():
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            size_kb = Path(txt_path).stat().st_size // 1024
            print(f"  ✓ Text cache saved (pypdf): {txt_path} ({size_kb} KB)", file=sys.stderr)
        else:
            print(f"  [WARN] pypdf extracted no text from {pdf_path}", file=sys.stderr)
    except ImportError:
        print(f"  [WARN] pypdf not installed — skipping text cache", file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] pypdf extraction failed: {e}", file=sys.stderr)


def fetch_github_readme(github_url: str) -> str | None:
    """Fetch README.md from a GitHub repo."""
    # Convert to raw README URL
    m = re.match(r"https?://github\.com/([^/]+)/([^/\s?#]+)", github_url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    for branch in ["main", "master"]:
        for readme in ["README.md", "README.MD", "readme.md"]:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme}"
            content = _fetch_text(raw_url)
            if content and len(content) > 100:
                return content
    return None


def fetch_project_page(url: str) -> str | None:
    """Fetch and clean a project page as markdown-like text."""
    if "github.com" in url:
        readme = fetch_github_readme(url)
        if readme:
            return f"# GitHub Project: {url}\n\n{readme[:5000]}"

    if BeautifulSoup is None:
        return None

    html = _fetch_text(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    # Remove nav, footer, scripts
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # Clean up blank lines
    lines = [l for l in text.splitlines() if l.strip()]
    return f"# Project Page: {url}\n\n" + "\n".join(lines[:200])


def main():
    ap = argparse.ArgumentParser(description="Download paper PDF and project resources")
    ap.add_argument("--arxiv-id", required=True, help="ArXiv ID, e.g. 2301.08243")
    ap.add_argument("--output-dir", default="./papers", help="Output directory")
    ap.add_argument("--with-project", action="store_true",
                    help="Also download project page / GitHub README")
    ap.add_argument("--no-pdf", action="store_true", help="Skip PDF download")
    ap.add_argument("--metadata-only", action="store_true", help="Only fetch metadata, no downloads")
    ap.add_argument("--extract-text", action="store_true",
                    help="Extract text from an already-downloaded PDF (no network needed)")
    args = ap.parse_args()

    arxiv_id = re.sub(r"^arxiv[:/]", "", args.arxiv_id, flags=re.I).strip()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --extract-text: offline operation, skip all network requests
    if args.extract_text:
        pdf_path = out_dir / f"{arxiv_id.replace('/', '_')}.pdf"
        if pdf_path.exists():
            _extract_pdf_text(str(pdf_path))
        else:
            print(f"[ERROR] PDF not found: {pdf_path}", file=sys.stderr)
            sys.exit(1)
        return

    print(f"[DL] Fetching metadata for arXiv:{arxiv_id} ...", file=sys.stderr)
    meta = fetch_arxiv_metadata(arxiv_id)
    if not meta:
        print(f"[ERROR] Could not fetch metadata for {arxiv_id}", file=sys.stderr)
        sys.exit(1)

    # Save metadata
    meta_path = out_dir / f"{arxiv_id.replace('/', '_')}_info.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"  ✓ Metadata saved: {meta_path}", file=sys.stderr)

    if not args.metadata_only:
        # Download PDF
        if not args.no_pdf:
            pdf_path = out_dir / f"{arxiv_id.replace('/', '_')}.pdf"
            download_pdf(arxiv_id, str(pdf_path))
            time.sleep(1)

        # Download project resources
        if args.with_project and meta.get("project_urls"):
            for proj_url in meta["project_urls"][:2]:
                print(f"  Fetching project resource: {proj_url} ...", file=sys.stderr)
                content = fetch_project_page(proj_url)
                if content:
                    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", proj_url.split("//")[1][:40])
                    proj_path = out_dir / f"{arxiv_id.replace('/', '_')}_project_{safe_name}.md"
                    proj_path.write_text(content, encoding="utf-8")
                    print(f"  ✓ Project resource saved: {proj_path}", file=sys.stderr)
                time.sleep(1)

    # Print summary
    authors_str = ", ".join(meta.get("authors", [])[:4])
    if len(meta.get("authors", [])) > 4:
        authors_str += " et al."

    print(f"\n## Downloaded: {meta.get('title', arxiv_id)}\n")
    print(f"**ArXiv ID:** `{arxiv_id}`")
    print(f"**Authors:** {authors_str}")
    print(f"**Published:** {meta.get('published', '?')} | **Updated:** {meta.get('updated', '?')}")
    print(f"**Categories:** {' | '.join(meta.get('categories', []))}")
    print(f"**arXiv:** {meta.get('arxiv_url', '')}")
    print(f"**PDF:** {meta.get('pdf_url', '')}")
    if meta.get("project_urls"):
        print(f"**Project URLs:**")
        for u in meta["project_urls"]:
            print(f"  - {u}")
    print(f"\n**Abstract:**")
    print(meta.get("abstract", "")[:500])
    print(f"\n**Files saved to:** `{out_dir.resolve()}/`")


if __name__ == "__main__":
    main()
