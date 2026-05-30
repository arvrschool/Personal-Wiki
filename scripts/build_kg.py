#!/usr/bin/env python3
from __future__ import annotations
"""
Build and export a paper knowledge graph using Semantic Scholar / arXiv data.

Usage:
    # Online mode (needs S2 API access):
    python build_kg.py --seed-ids "ArXiv:2301.08243,ArXiv:2205.01068" --output kg.html
    python build_kg.py --seed-ids "ArXiv:2301.08243" --depth 2 --output kg.html --json kg.json

    # Offline mode (no API needed — provide papers as JSON):
    python build_kg.py --offline-papers papers.json --output kg.html
    python build_kg.py --offline-papers '[{"title":"I-JEPA","arxiv_id":"2301.08243","authors":["Assran"],"year":2023,"citations":1200,"is_seed":true},{"title":"MAE","arxiv_id":"2111.06377","authors":["He"],"year":2021,"citations":14000}]' --edges '[["2301.08243","2111.06377"]]' --output kg.html

    The --offline-papers value can be a JSON string or a path to a .json file.
    Each paper dict: {title, arxiv_id, authors (list), year, citations, is_seed (bool, optional)}
    --edges: list of [source_arxiv_id, target_arxiv_id] pairs (source cites target)

Output:
    - Interactive Plotly HTML graph (--output)
    - Optional JSON graph dump (--json)
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
import re

nx = None
go = None


S2_BASE = "https://api.semanticscholar.org/graph/v1"
ARXIV_API = "https://export.arxiv.org/api/query"
FIELDS = "title,authors,year,citationCount,references,citations,externalIds,abstract,venue"


def _ensure_networkx():
    global nx
    if nx is None:
        try:
            import networkx as _nx
        except ImportError:
            print("ERROR: networkx not installed. Run: pip install networkx", file=sys.stderr)
            sys.exit(1)
        nx = _nx


def _ensure_plotly():
    global go
    if go is None:
        try:
            import plotly.graph_objects as _go
        except ImportError:
            print("ERROR: plotly not installed. Run: pip install plotly", file=sys.stderr)
            sys.exit(1)
        go = _go


def _s2_get(path: str, params: dict, retries: int = 3) -> dict:
    url = f"{S2_BASE}{path}?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-search-kg/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [S2] rate limit, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
            elif e.code == 404:
                return {}
            else:
                time.sleep(2)
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [S2] request failed: {e}", file=sys.stderr)
            time.sleep(2)
    return {}


def resolve_paper_id(raw_id: str) -> str:
    """Convert ArXiv:XXXX or arXiv URL to S2 format."""
    raw_id = raw_id.strip()
    m = re.search(r"arxiv\.org/abs/([0-9.]+v?\d*)", raw_id, re.I)
    if m:
        return f"ArXiv:{m.group(1)}"
    m = re.search(r"([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", raw_id)
    if m:
        return f"ArXiv:{m.group(1)}"
    return raw_id  # assume already S2 format


def fetch_paper(paper_id: str) -> dict | None:
    data = _s2_get(f"/paper/{paper_id}", {"fields": FIELDS})
    if not data or "paperId" not in data:
        return None
    return data


def normalize_paper(p: dict) -> dict:
    """Flatten S2 paper dict to a simple node dict."""
    arxiv_id = (p.get("externalIds") or {}).get("ArXiv", "")
    return {
        "s2_id": p.get("paperId", ""),
        "arxiv_id": arxiv_id,
        "title": p.get("title", "Unknown"),
        "authors": [a.get("name", "") for a in (p.get("authors") or [])],
        "year": p.get("year"),
        "citations": p.get("citationCount", 0),
        "venue": p.get("venue", ""),
        "abstract": (p.get("abstract") or "")[:300],
        "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else f"https://www.semanticscholar.org/paper/{p.get('paperId','')}",
    }


def build_graph(seed_ids: list[str], depth: int = 1, max_refs: int = 15, max_cites: int = 10) -> nx.DiGraph:
    _ensure_networkx()
    G = nx.DiGraph()
    visited = set()
    queue = [(sid, 0) for sid in seed_ids]
    seeds_normalized = {resolve_paper_id(s) for s in seed_ids}

    while queue:
        raw_id, d = queue.pop(0)
        pid = resolve_paper_id(raw_id)
        if pid in visited:
            continue
        visited.add(pid)

        print(f"  Fetching [{d}] {pid} ...", file=sys.stderr)
        p = fetch_paper(pid)
        if not p:
            print(f"  [WARN] could not fetch {pid}", file=sys.stderr)
            continue

        node = normalize_paper(p)
        node["is_seed"] = pid in seeds_normalized
        G.add_node(node["s2_id"], **node)

        if d < depth:
            # References (backward edges: paper -> referenced)
            refs = (p.get("references") or [])[:max_refs]
            for ref in refs:
                ref_id = ref.get("paperId", "")
                if ref_id and ref_id not in visited:
                    queue.append((ref_id, d + 1))
                if ref_id:
                    G.add_edge(node["s2_id"], ref_id, edge_type="cites")

            # Citations (forward edges: cited_by -> paper)
            cites = (p.get("citations") or [])[:max_cites]
            for cit in cites:
                cit_id = cit.get("paperId", "")
                if cit_id and cit_id not in visited:
                    queue.append((cit_id, d + 1))
                if cit_id:
                    G.add_edge(cit_id, node["s2_id"], edge_type="cited_by")

        time.sleep(0.4)  # be polite to S2

    # Fetch metadata for stub nodes (nodes added as neighbors but not yet fetched)
    stubs = [n for n in G.nodes() if "title" not in G.nodes[n]]
    print(f"  Fetching {len(stubs)} stub nodes...", file=sys.stderr)
    for stub in stubs[:40]:  # cap at 40 stubs
        p = fetch_paper(stub)
        if p:
            node = normalize_paper(p)
            node["is_seed"] = stub in seeds_normalized
            G.nodes[stub].update(node)
        time.sleep(0.4)

    return G


def _node_label(G: nx.DiGraph, n: str) -> str:
    d = G.nodes[n]
    title = d.get("title", n)
    short = title[:40] + "…" if len(title) > 40 else title
    year = d.get("year", "")
    return f"{short}<br>{year}"


def _node_hover(G: nx.DiGraph, n: str) -> str:
    d = G.nodes[n]
    authors = ", ".join((d.get("authors") or [])[:3])
    if len(d.get("authors") or []) > 3:
        authors += " et al."
    return (
        f"<b>{d.get('title','?')}</b><br>"
        f"Authors: {authors}<br>"
        f"Year: {d.get('year','?')} | Venue: {d.get('venue','?')}<br>"
        f"Citations: {d.get('citations', 0)}<br>"
        f"ArXiv: {d.get('arxiv_id','N/A')}<br>"
        f"<i>{d.get('abstract','')[:200]}…</i>"
    )


def render_html(G: nx.DiGraph, output_path: str, title: str = "Paper Knowledge Graph"):
    _ensure_networkx()
    _ensure_plotly()
    if len(G.nodes) == 0:
        print("[WARN] Empty graph, nothing to render.", file=sys.stderr)
        return

    pos = nx.spring_layout(G, k=2.5, seed=42)

    # Edge traces
    edge_x, edge_y = [], []
    for u, v in G.edges():
        if u in pos and v in pos:
            x0, y0 = pos[u]
            x1, y1 = pos[v]
            edge_x += [x0, x1, None]
            edge_y += [y0, y1, None]

    edge_trace = go.Scatter(
        x=edge_x, y=edge_y,
        mode="lines",
        line=dict(width=0.8, color="#aaa"),
        hoverinfo="none",
        showlegend=False,
    )

    # Node traces — separate seeds vs non-seeds
    def _node_size(n):
        c = G.nodes[n].get("citations", 0) or 0
        return max(10, min(40, 10 + c ** 0.4))

    def _node_color(n):
        if G.nodes[n].get("is_seed"):
            return "#e74c3c"
        y = G.nodes[n].get("year") or 2000
        # gradient: old=blue, new=orange
        frac = min(1.0, max(0.0, (y - 2015) / 10.0))
        r = int(44 + frac * (243 - 44))
        g = int(62 + frac * (156 - 62))
        b = int(80 + frac * (18 - 80))
        return f"rgb({r},{g},{b})"

    node_x = [pos[n][0] for n in G.nodes() if n in pos]
    node_y = [pos[n][1] for n in G.nodes() if n in pos]
    node_text = [_node_label(G, n) for n in G.nodes() if n in pos]
    node_hover = [_node_hover(G, n) for n in G.nodes() if n in pos]
    node_sizes = [_node_size(n) for n in G.nodes() if n in pos]
    node_colors = [_node_color(n) for n in G.nodes() if n in pos]
    node_urls = [G.nodes[n].get("url", "") for n in G.nodes() if n in pos]

    node_trace = go.Scatter(
        x=node_x, y=node_y,
        mode="markers+text",
        text=node_text,
        textposition="top center",
        textfont=dict(size=8),
        hovertext=node_hover,
        hoverinfo="text",
        customdata=node_urls,
        marker=dict(
            size=node_sizes,
            color=node_colors,
            line=dict(width=1, color="#fff"),
            opacity=0.9,
        ),
        showlegend=False,
    )

    fig = go.Figure(
        data=[edge_trace, node_trace],
        layout=go.Layout(
            title=dict(text=title, font=dict(size=16)),
            showlegend=False,
            hovermode="closest",
            margin=dict(b=20, l=5, r=5, t=50),
            paper_bgcolor="#1a1a2e",
            plot_bgcolor="#1a1a2e",
            font=dict(color="#eee"),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            height=750,
            annotations=[
                dict(
                    text="🔴 Seed papers  |  Node size = citation count  |  Color: blue=older → orange=newer  |  Click node to open paper",
                    xref="paper", yref="paper",
                    x=0.005, y=-0.002,
                    xanchor="left", yanchor="bottom",
                    showarrow=False,
                    font=dict(size=10, color="#aaa"),
                )
            ],
        ),
    )

    # Add click-to-open-URL via JavaScript
    click_js = """
<script>
var plot = document.getElementById('plotly-graph');
plot.on('plotly_click', function(data){
    var pt = data.points[0];
    if(pt.customdata && pt.customdata !== ''){
        window.open(pt.customdata, '_blank');
    }
});
</script>
"""

    html = fig.to_html(full_html=True, include_plotlyjs='cdn')
    html = html.replace("</body>", click_js + "</body>")

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"[KG] Saved interactive graph → {output_path}  ({len(G.nodes())} nodes, {len(G.edges())} edges)", file=sys.stderr)


def build_graph_offline(
    papers_data: list[dict],
    edges_data: list | None = None,
    auto_edges_to_seeds: bool = False,
) -> nx.DiGraph:
    """
    Build graph from a list of paper dicts (no API calls).

    auto_edges_to_seeds=True: automatically add an edge from every non-seed paper
    to every seed paper (representing "is related to / extends seed").
    This is the right default when the user just passes search results — every
    result is related to the seed topic so they should appear connected.

    Each paper dict may include:
      title, arxiv_id, authors, year, citations, is_seed, venue, abstract, url
    url overrides the default arxiv link (useful for OpenReview, project pages, etc.)
    """
    _ensure_networkx()
    G = nx.DiGraph()
    # Support lookup by arxiv_id OR by a short slug (title prefix)
    id_map: dict[str, str] = {}  # arxiv_id / slug → node_id

    for p in papers_data:
        arxiv_id = str(p.get("arxiv_id", "")).strip()
        slug = re.sub(r"[^a-z0-9]", "_", (p.get("title") or "")[:30].lower())
        node_id = p.get("s2_id") or (f"arxiv_{arxiv_id}" if arxiv_id else f"paper_{slug}")

        # URL: explicit > arxiv link > empty
        explicit_url = (p.get("url") or "").strip()
        url = explicit_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")

        node = {
            "s2_id": node_id,
            "arxiv_id": arxiv_id,
            "title": p.get("title", "Unknown"),
            "authors": p.get("authors", []),
            "year": p.get("year"),
            "citations": p.get("citations", 0),
            "venue": p.get("venue", ""),
            "abstract": (p.get("abstract") or "")[:300],
            "is_seed": bool(p.get("is_seed", False)),
            "url": url,
        }
        G.add_node(node_id, **node)
        if arxiv_id:
            id_map[arxiv_id] = node_id
        id_map[slug] = node_id

    # Explicit edges
    if edges_data:
        for edge in edges_data:
            if len(edge) >= 2:
                src_key, tgt_key = str(edge[0]), str(edge[1])
                src = id_map.get(src_key, src_key)
                tgt = id_map.get(tgt_key, tgt_key)
                if src in G and tgt in G:
                    G.add_edge(src, tgt, edge_type="cites")

    # Auto-connect all non-seed papers to seed papers
    if auto_edges_to_seeds:
        seeds = [n for n in G.nodes() if G.nodes[n].get("is_seed")]
        non_seeds = [n for n in G.nodes() if not G.nodes[n].get("is_seed")]
        for ns in non_seeds:
            for s in seeds:
                if not G.has_edge(ns, s):
                    G.add_edge(ns, s, edge_type="related_to")

    return G


def main():
    ap = argparse.ArgumentParser(description="Build paper knowledge graph")
    ap.add_argument("--seed-ids", default=None,
                    help="Comma-separated paper IDs, e.g. 'ArXiv:2301.08243,ArXiv:2205.01068'")
    ap.add_argument("--offline-papers", default=None,
                    help="JSON string or path to .json file with paper list for offline mode. "
                         "Each paper: {title, arxiv_id, authors, year, citations, is_seed}")
    ap.add_argument("--edges", default=None,
                    help="JSON array of [source_arxiv_id, target_arxiv_id] edge pairs for offline mode")
    ap.add_argument("--auto-edges-to-seeds", action="store_true",
                    help="Auto-connect every non-seed paper to every seed paper (default when no --edges given)")
    ap.add_argument("--depth", type=int, default=1,
                    help="Citation graph expansion depth (0=seeds only, 1=neighbors, 2=neighbors-of-neighbors)")
    ap.add_argument("--max-refs", type=int, default=15,
                    help="Max references to expand per paper")
    ap.add_argument("--max-cites", type=int, default=10,
                    help="Max citations to expand per paper")
    ap.add_argument("--output", default="kg.html",
                    help="Output HTML path")
    ap.add_argument("--json", default=None,
                    help="Optional: also save graph as JSON")
    ap.add_argument("--title", default="Paper Knowledge Graph",
                    help="Graph title")
    args = ap.parse_args()

    if args.offline_papers:
        # Offline mode: build from provided paper data, no API calls
        raw = args.offline_papers.strip()
        if raw.endswith(".json") and Path(raw).exists():
            papers_data = json.loads(Path(raw).read_text())
        else:
            papers_data = json.loads(raw)
        edges_data = json.loads(args.edges) if args.edges else None
        # If no explicit edges given, auto-connect everything to seeds
        auto_edges = args.auto_edges_to_seeds or (edges_data is None)
        print(f"[KG] Offline mode: building graph from {len(papers_data)} papers "
              f"(auto_edges_to_seeds={auto_edges})...", file=sys.stderr)
        G = build_graph_offline(papers_data, edges_data, auto_edges_to_seeds=auto_edges)
    elif args.seed_ids:
        seed_ids = [s.strip() for s in args.seed_ids.split(",") if s.strip()]
        print(f"[KG] Building graph for {len(seed_ids)} seeds, depth={args.depth}...", file=sys.stderr)
        G = build_graph(seed_ids, depth=args.depth, max_refs=args.max_refs, max_cites=args.max_cites)
    else:
        print("ERROR: provide either --seed-ids or --offline-papers", file=sys.stderr)
        sys.exit(1)

    render_html(G, args.output, title=args.title)

    if args.json:
        data = {
            "nodes": [{"id": n, **G.nodes[n]} for n in G.nodes()],
            "edges": [{"source": u, "target": v, **G.edges[u, v]} for u, v in G.edges()],
        }
        Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"[KG] JSON saved → {args.json}", file=sys.stderr)

    # Print summary table to stdout
    nodes = sorted(G.nodes(), key=lambda n: G.nodes[n].get("citations", 0) or 0, reverse=True)
    print(f"\n## Knowledge Graph Summary — {len(G.nodes())} papers\n")
    print(f"| # | Title | Authors | Year | Citations | ArXiv |")
    print(f"|---|-------|---------|------|-----------|-------|")
    for i, n in enumerate(nodes[:30], 1):
        d = G.nodes[n]
        title = (d.get("title") or "?")[:50]
        authors = ", ".join((d.get("authors") or [])[:2])
        if len(d.get("authors") or []) > 2:
            authors += " et al."
        year = d.get("year", "?")
        cites = d.get("citations", 0)
        arxiv = d.get("arxiv_id", "")
        arxiv_link = f"[{arxiv}](https://arxiv.org/abs/{arxiv})" if arxiv else "—"
        seed_mark = " 🌱" if d.get("is_seed") else ""
        print(f"| {i} | {title}{seed_mark} | {authors} | {year} | {cites} | {arxiv_link} |")


if __name__ == "__main__":
    main()
