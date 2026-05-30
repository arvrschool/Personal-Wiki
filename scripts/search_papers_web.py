#!/usr/bin/env python3
"""
Multi-dimensional academic paper search via arXiv XML API + Google Scholar.

Instead of a single keyword query, this script searches across multiple
dimensions — keywords, authors, related concepts, specific paper names —
then merges and ranks results by a relevance score.

Usage:
    # Basic keyword search
    python search_papers_web.py "JEPA joint embedding predictive" --limit 30

    # Multi-dimensional: keywords + authors + related topics
    python search_papers_web.py "JEPA joint embedding predictive" \\
        --authors "Yann LeCun" "Assran" "Adrien Bardes" \\
        --related "world model self-supervised" "latent prediction robot" \\
        --limit 40

    # With specific paper names that might not match main query
    python search_papers_web.py "JEPA" \\
        --extra "LeWorldModel" "LeJEPA" "MC-JEPA" \\
        --limit 30

    # Save JSON results for downstream wiki build
    python search_papers_web.py "JEPA" --authors "LeCun" "Bardes" \\
        --related "world model" --limit 40 --json-out papers.json
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional

from config_loader import cfg

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 not installed. Run: pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ARXIV_API = "https://export.arxiv.org/api/query"


def _fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  [WARN] fetch failed: {url[:80]} — {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# arXiv XML API
# ---------------------------------------------------------------------------

def _arxiv_query(q: str, max_results: int = cfg("search", "arxiv_per_query", 25), start: int = 0) -> tuple[list[dict], int]:
    """Run one arXiv API query. Returns (papers, total_available)."""
    params = urllib.parse.urlencode({
        "search_query": q,
        "sortBy": "relevance",
        "sortOrder": "descending",
        "max_results": max_results,
        "start": start,
    })
    url = f"{ARXIV_API}?{params}"
    xml = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-search-kg/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                xml = r.read().decode("utf-8")
            break
        except Exception as e:
            if attempt == 2:
                print(f"  [arXiv] FAIL {q[:50]}: {e}", file=sys.stderr)
                return [], 0
            time.sleep(cfg("search", "search_delay_arxiv", 2.0))

    total_m = re.search(r"<opensearch:totalResults[^>]*>(\d+)</opensearch:totalResults>", xml)
    total = int(total_m.group(1)) if total_m else 0

    papers = []
    for raw in re.split(r"<entry>", xml)[1:]:
        id_m = re.search(r"<id>http://arxiv\.org/abs/([^<v]+)v?\d*</id>", raw)
        arxiv_id = id_m.group(1) if id_m else ""
        title_m = re.search(r"<title>(.+?)</title>", raw, re.DOTALL)
        title = re.sub(r"\s+", " ", title_m.group(1).strip()) if title_m else ""
        authors = re.findall(r"<name>([^<]+)</name>", raw)
        pub_m = re.search(r"<published>(\d{4})", raw)
        year = int(pub_m.group(1)) if pub_m else None
        abs_m = re.search(r"<summary>(.+?)</summary>", raw, re.DOTALL)
        abstract = re.sub(r"\s+", " ", abs_m.group(1).strip()) if abs_m else ""
        # Extract project URLs from abstract + arxiv:comment field
        comment_m = re.search(r"<arxiv:comment[^>]*>(.+?)</arxiv:comment>", raw, re.DOTALL)
        comment = comment_m.group(1).strip() if comment_m else ""
        url_pat = re.compile(r"https?://(?:github\.com|[a-z0-9.-]+\.[a-z]{2,})/[^\s,;\"'<>]+", re.I)
        proj_urls = list(dict.fromkeys(url_pat.findall(abstract + " " + comment)))
        proj_urls = [u for u in proj_urls if "arxiv.org" not in u][:3]
        if title and arxiv_id:
            papers.append({
                "title": title,
                "arxiv_id": arxiv_id,
                "authors": authors[:6],
                "year": year,
                "abstract": abstract[:400],
                "citations": 0,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "project_urls": proj_urls,
                "source": "arxiv_api",
            })
    return papers, total


def search_by_keyword(query: str, limit: int = 25) -> list[dict]:
    """Search arXiv by keyword/phrase (all: field)."""
    q = f"all:{query}"
    papers, total = _arxiv_query(q, min(limit, 100))
    print(f"  [keyword] '{query[:50]}' → {len(papers)} / {total} total", file=sys.stderr)
    return papers[:limit]


def search_by_title(query: str, limit: int = 10) -> list[dict]:
    """Search arXiv by title field (ti:). More precise than all: for model names with version numbers."""
    q = f"ti:{query}"
    papers, total = _arxiv_query(q, min(limit, 50))
    print(f"  [title]   '{query[:50]}' → {len(papers)} / {total} total", file=sys.stderr)
    return papers[:limit]


def search_by_author(author: str, limit: int = cfg("search", "author_limit", 20)) -> list[dict]:
    """Search arXiv for papers by a specific author."""
    q = f"au:{author}"
    papers, total = _arxiv_query(q, min(limit, 50))
    print(f"  [author]  '{author}' → {len(papers)} / {total} total", file=sys.stderr)
    return papers[:limit]


# ---------------------------------------------------------------------------
# OpenReview API
# ---------------------------------------------------------------------------

OPENREVIEW_API = "https://api2.openreview.net"

def search_by_openreview(query: str, limit: int = cfg("search", "openreview_limit", 20)) -> list[dict]:
    """Search OpenReview for papers (covers ICLR, NeurIPS, ICML, etc.)."""
    papers = []
    try:
        params = urllib.parse.urlencode({
            "term": query,
            "limit": min(limit, 25),
            "offset": 0,
        })
        url = f"{OPENREVIEW_API}/notes/search?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "paper-search-kg/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        notes = data.get("notes", [])
        for note in notes:
            content = note.get("content", {})
            title = content.get("title", {})
            title = title.get("value", title) if isinstance(title, dict) else title
            if not title:
                continue
            abstract_raw = content.get("abstract", {})
            abstract = abstract_raw.get("value", abstract_raw) if isinstance(abstract_raw, dict) else abstract_raw
            authors_raw = content.get("authors", {})
            authors_list = authors_raw.get("value", authors_raw) if isinstance(authors_raw, dict) else authors_raw
            if isinstance(authors_list, str):
                authors_list = [a.strip() for a in authors_list.split(",")]
            year = None
            cdate = note.get("cdate") or note.get("pdate") or note.get("mdate")
            if cdate:
                import datetime
                try:
                    year = datetime.datetime.utcfromtimestamp(cdate / 1000).year
                except Exception:
                    pass
            forum_id = note.get("forum", note.get("id", ""))
            paper_url = f"https://openreview.net/forum?id={forum_id}" if forum_id else ""
            papers.append({
                "title": str(title),
                "arxiv_id": "",
                "authors": (authors_list or [])[:6],
                "year": year,
                "abstract": str(abstract or "")[:400],
                "citations": 0,
                "url": paper_url,
                "source": "openreview",
            })
    except Exception as e:
        print(f"  [OpenReview] search failed: {e}", file=sys.stderr)
    print(f"  [openreview] '{query[:50]}' → {len(papers)} results", file=sys.stderr)
    return papers[:limit]


# ---------------------------------------------------------------------------
# Google Scholar (for citation counts)
# ---------------------------------------------------------------------------

def _scholar_search(query: str, limit: int = cfg("search", "scholar_limit", 10)) -> list[dict]:
    """Scrape Google Scholar for citation counts."""
    papers = []
    start = 0
    encoded = urllib.parse.quote_plus(query)

    while len(papers) < limit:
        url = f"https://scholar.google.com/scholar?q={encoded}&hl=en&start={start}"
        html = _fetch_html(url)
        if not html:
            break
        if "unusual traffic" in html.lower() or "captcha" in html.lower():
            print(f"  [Scholar] Captcha at start={start}", file=sys.stderr)
            break

        soup = BeautifulSoup(html, "html.parser")
        items = soup.select(".gs_r.gs_or.gs_scl")
        if not items:
            break

        for item in items:
            title_el = item.select_one(".gs_rt")
            if not title_el:
                continue
            title_text = re.sub(r"^\[.*?\]\s*", "", title_el.get_text(strip=True))
            url_paper = ""
            title_a = title_el.select_one("a")
            if title_a:
                url_paper = title_a.get("href", "")
            arxiv_id = ""
            m = re.search(r"arxiv\.org/abs/([0-9.]+)", url_paper)
            if m:
                arxiv_id = m.group(1)
            authors, year = [], None
            sub_el = item.select_one(".gs_a")
            if sub_el:
                subtitle = sub_el.get_text(strip=True)
                parts = subtitle.split(" - ")
                if parts:
                    authors = [a.strip() for a in re.split(r",|…", parts[0]) if a.strip()]
                m2 = re.search(r"\b(19|20)\d{2}\b", subtitle)
                if m2:
                    year = int(m2.group(0))
            citations = 0
            for link in item.select(".gs_fl a"):
                cm = re.search(r"Cited by (\d+)", link.get_text())
                if cm:
                    citations = int(cm.group(1))
                    break
            papers.append({
                "title": title_text, "arxiv_id": arxiv_id, "authors": authors[:6],
                "year": year, "citations": citations, "url": url_paper, "source": "scholar",
            })
            if len(papers) >= limit:
                break

        start += 10
        time.sleep(cfg("search", "search_delay_scholar", 1.2))

    return papers[:limit]


# ---------------------------------------------------------------------------
# Merge + rank
# ---------------------------------------------------------------------------

def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def merge_and_rank(
    results_by_source: dict[str, list[dict]],
    limit: int = 30,
) -> list[dict]:
    """
    Merge papers from multiple sources. Scoring:
    - +3 for each independent source that returned the paper (appearing in
      multiple query angles means it's highly relevant)
    - +log10(citations+1) for citation count
    - +1 for recency (year >= 2023)

    Returns ranked list, deduplicated by arxiv_id.
    """
    # Collect all papers, track how many sources found each
    by_id: dict[str, dict] = {}     # arxiv_id → merged paper dict
    source_count: dict[str, int] = {}  # arxiv_id → how many sources found it
    source_names: dict[str, list[str]] = {}  # for debugging

    for source_name, papers in results_by_source.items():
        for p in papers:
            aid = p.get("arxiv_id") or _norm_title(p.get("title", ""))[:30]
            if not aid:
                continue
            if aid not in by_id:
                by_id[aid] = dict(p)
                source_count[aid] = 0
                source_names[aid] = []
            else:
                # Merge: keep best citation count and most complete data
                existing = by_id[aid]
                if (p.get("citations") or 0) > (existing.get("citations") or 0):
                    existing["citations"] = p["citations"]
                if not existing.get("abstract") and p.get("abstract"):
                    existing["abstract"] = p["abstract"]
                if not existing.get("year") and p.get("year"):
                    existing["year"] = p["year"]
                if len(p.get("authors") or []) > len(existing.get("authors") or []):
                    existing["authors"] = p["authors"]
            source_count[aid] += 1
            source_names[aid].append(source_name)

    # Score each paper
    import math
    scored = []
    for aid, p in by_id.items():
        score = 0.0
        score += source_count[aid] * 3           # cross-source bonus
        score += math.log10((p.get("citations") or 0) + 1)  # citation weight
        if (p.get("year") or 0) >= 2023:
            score += 1.0                          # recency bonus
        scored.append((score, p))

    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:limit]]


# ---------------------------------------------------------------------------
# Main search pipeline
# ---------------------------------------------------------------------------

def search_papers_multidim(
    query: str,
    authors: list[str] | None = None,
    related: list[str] | None = None,
    extra: list[str] | None = None,
    limit: int = 30,
    enrich_citations: bool = True,
) -> list[dict]:
    """
    Multi-dimensional paper discovery:
    1. Keyword search on main query
    2. Author-based search (finds papers by known experts in the field)
    3. Related topic searches (concept expansion: world models, robot learning, etc.)
    4. Extra specific-name queries (paper names with different naming conventions)
    5. Merge all results, score by cross-source overlap + citations + recency
    6. Optionally enrich top results with Scholar citation counts
    """
    results_by_source: dict[str, list[dict]] = {}

    # 1. Main keyword query (larger budget)
    results_by_source["keyword_main"] = search_by_keyword(query, limit=limit)

    # 2. Author queries
    for author in (authors or []):
        key = f"author_{author[:20]}"
        results_by_source[key] = search_by_author(author, limit=cfg("search", "author_limit", 20))
        time.sleep(cfg("search", "search_delay_batch", 0.5))

    # 3. Related topic queries
    for topic in (related or []):
        key = f"related_{topic[:20]}"
        results_by_source[key] = search_by_keyword(topic, limit=cfg("search", "related_limit", 15))
        time.sleep(cfg("search", "search_delay_batch", 0.5))

    # 4. Extra specific queries (paper names, acronyms)
    for q in (extra or []):
        key = f"extra_{q[:20]}"
        results_by_source[key] = search_by_keyword(q, limit=cfg("search", "extra_limit", 10))
        time.sleep(cfg("search", "search_delay_extra", 0.3))

    # 5. OpenReview search (catches ICLR/NeurIPS/ICML papers not on arXiv)
    or_results = search_by_openreview(query, limit=cfg("search", "openreview_limit", 20))
    if or_results:
        results_by_source["openreview_main"] = or_results
        time.sleep(cfg("search", "search_delay_extra", 0.3))

    total_raw = sum(len(v) for v in results_by_source.values())
    unique = len({p.get("arxiv_id") for v in results_by_source.values() for p in v if p.get("arxiv_id")})
    print(f"\n  [merge] {total_raw} raw results across {len(results_by_source)} queries → {unique} unique papers", file=sys.stderr)

    # Collect extra-query papers — these are guaranteed spots (user asked for them explicitly)
    # They get prepended before ranking so they always appear regardless of score
    extra_guaranteed: list[dict] = []
    extra_ids: set[str] = set()
    for q in (extra or []):
        key = f"extra_{q[:20]}"
        for p in results_by_source.get(key, [])[:1]:  # top-1 result per extra query
            aid = p.get("arxiv_id", "")
            if aid and aid not in extra_ids:
                extra_guaranteed.append(p)
                extra_ids.add(aid)

    # Remove extra papers from ranking pool to avoid duplicates, then rank the rest
    ranking_sources = {k: [p for p in v if p.get("arxiv_id") not in extra_ids]
                       for k, v in results_by_source.items()}
    ranked = merge_and_rank(ranking_sources, limit=max(0, limit - len(extra_guaranteed)))
    papers = extra_guaranteed + ranked

    # 5. Enrich top results with Scholar citation counts
    if enrich_citations and papers:
        try:
            print(f"  [Scholar] Fetching citation counts...", file=sys.stderr)
            scholar = _scholar_search(query, limit=cfg("search", "scholar_limit", 10))
            norm_map = {_norm_title(p["title"]): p.get("citations", 0) for p in scholar if p.get("citations")}
            id_map = {p["arxiv_id"]: p.get("citations", 0) for p in scholar if p.get("arxiv_id") and p.get("citations")}
            for p in papers:
                aid = p.get("arxiv_id", "")
                if aid in id_map and id_map[aid] > (p.get("citations") or 0):
                    p["citations"] = id_map[aid]
                elif _norm_title(p.get("title", "")) in norm_map:
                    p["citations"] = norm_map[_norm_title(p["title"])]
        except Exception as e:
            print(f"  [Scholar] Enrichment failed: {e}", file=sys.stderr)

    return papers


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(papers: list[dict]):
    print(f"\n## Paper Search Results — {len(papers)} papers\n")
    print("| # | Title | Authors | Year | Citations | ArXiv |")
    print("|---|-------|---------|------|-----------|-------|")
    for i, p in enumerate(papers, 1):
        title = (p.get("title") or "?")[:55]
        authors = ", ".join((p.get("authors") or [])[:2])
        if len(p.get("authors") or []) > 2:
            authors += " et al."
        year = p.get("year") or "?"
        cites = p.get("citations") or 0
        arxiv = p.get("arxiv_id") or ""
        arxiv_link = f"[{arxiv}](https://arxiv.org/abs/{arxiv})" if arxiv else p.get("url", "—")[:50]
        print(f"| {i} | {title} | {authors} | {year} | {cites} | {arxiv_link} |")


def main():
    ap = argparse.ArgumentParser(
        description="Multi-dimensional paper search: keywords + authors + related topics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # JEPA: keyword + key authors + related concepts
  python search_papers_web.py "JEPA joint embedding predictive" \\
      --authors "Yann LeCun" "Assran" "Adrien Bardes" "Balestriero" \\
      --related "world model self-supervised learning" "latent prediction robot" \\
      --extra "LeWorldModel" "LeJEPA" --limit 40

  # 3DGS: keyword + key authors + related
  python search_papers_web.py "3D Gaussian Splatting" \\
      --authors "Bernhard Kerbl" "Georgios Kopanas" \\
      --related "neural radiance field real-time rendering" \\
      --limit 30
        """
    )
    ap.add_argument("query", help="Main search query")
    ap.add_argument("--limit", type=int, default=30, help="Max papers to return (default: 30)")
    ap.add_argument("--authors", nargs="+", default=None,
                    help="Author names to search (finds all their papers in this area)")
    ap.add_argument("--related", nargs="+", default=None,
                    help="Related topic queries for concept expansion")
    ap.add_argument("--extra", nargs="+", default=None,
                    help="Extra specific queries (specific paper names, acronyms)")
    ap.add_argument("--source", choices=["auto", "arxiv", "scholar"], default="auto",
                    help="auto=full multi-dim (default), arxiv=no Scholar enrichment, scholar=Scholar only")
    ap.add_argument("--json-out", default=None, help="Save full results to JSON file")
    ap.add_argument("--no-kg-json", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.source == "scholar":
        papers = _scholar_search(args.query, args.limit)
    else:
        enrich = (args.source != "arxiv")
        papers = search_papers_multidim(
            query=args.query,
            authors=args.authors,
            related=args.related,
            extra=args.extra,
            limit=args.limit,
            enrich_citations=enrich,
        )

    print_table(papers)

    if args.json_out:
        import pathlib
        pathlib.Path(args.json_out).write_text(
            json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[JSON] Saved → {args.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
