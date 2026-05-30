"""
fetch_paper_media.py — Download GIFs from GitHub README and find YouTube videos for a paper.

Complements fetch_paper_figures.py (which handles static figures from ar5iv/PDF).
This script handles:
  1. GIFs — animated demo results from GitHub README (downloaded locally)
  2. YouTube videos — search by paper title, download thumbnail + metadata only
                       (no MP4 download; Markdown embeds thumbnail + clickable link)

Output layout:
    <media_dir>/
        gif-1.gif
        gif-2.gif
        ...
        youtube-1.jpg          ← video thumbnail
        media.json             ← [{type, filename/url, title, caption, source}, ...]

Usage as library:
    from fetch_paper_media import fetch_media
    items = fetch_media(paper, out_dir=Path("wiki/media/2301.08243"))
    # items: list of media dicts

Usage as CLI:
    python fetch_paper_media.py --arxiv-id 2301.08243 \
        --title "Self-Supervised Learning from Images with a Joint-Embedding Predictive Architecture" \
        --github https://github.com/facebookresearch/ijepa \
        --out-dir wiki/media/2301.08243
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from config_loader import cfg

try:
    from bs4 import BeautifulSoup
    _BS4 = True
except ImportError:
    _BS4 = False

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

_MAX_GIF_MB = cfg("media", "max_gif_mb", 20)       # skip GIFs larger than this
_MAX_GIF_COUNT = cfg("media", "max_gif_count", 3)
_MAX_YOUTUBE = cfg("media", "max_youtube", 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_media(
    paper: dict,
    out_dir: Path,
    fetch_gifs: bool = True,
    fetch_youtube: bool = True,
    max_gif_count: int = _MAX_GIF_COUNT,
    max_youtube: int = _MAX_YOUTUBE,
) -> list[dict]:
    """
    Download GIFs from GitHub README and find YouTube videos for a paper.

    paper dict fields used:
        arxiv_id, title, authors, year, project_urls

    Returns list of media dicts:
        GIF:     {type:"gif",  filename:str, caption:str, source_url:str, width:int, height:int}
        YouTube: {type:"youtube", video_id:str, title:str, channel:str, duration:int,
                  views:int, thumbnail_url:str, thumbnail_file:str, youtube_url:str}
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    items: list[dict] = []

    # --- GIFs from GitHub README ---
    if fetch_gifs:
        github_url = _find_github_url(paper)
        if github_url:
            items += _fetch_github_gifs(github_url, out_dir, max_gif_count)

    # --- YouTube videos ---
    if fetch_youtube:
        items += _fetch_youtube_videos(paper, out_dir, max_youtube)

    meta_path = out_dir / "media.json"
    meta_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return items


def media_to_markdown(items: list[dict], media_dir: Path, wiki_root: Path | None = None, page_dir: Path | None = None) -> str:
    """
    Convert media metadata to a Markdown section for insertion into a wiki source page.

    GIFs and thumbnails use paths relative to page_dir (the directory of the page being
    written), so links resolve correctly in Obsidian and standard Markdown renderers.

    Returns a Markdown string starting with '## 演示与视频', or "" if no items.
    """
    import os
    if not items:
        return ""

    gifs = [i for i in items if i.get("type") == "gif"]
    videos = [i for i in items if i.get("type") == "youtube"]

    def _rel(path: Path) -> str:
        abs_path = path.resolve()
        if page_dir:
            return os.path.relpath(abs_path, Path(page_dir).resolve())
        if wiki_root:
            try:
                return str(abs_path.relative_to(Path(wiki_root).resolve()))
            except ValueError:
                pass
        return str(abs_path)

    lines = ["## 演示与视频\n"]

    if gifs:
        lines.append("### Demo GIF\n")
        for item in gifs:
            img_ref = _rel(media_dir / item["filename"])
            caption = item.get("caption", "").strip()
            lines.append(f"![{caption or 'Demo'}]({img_ref})\n")
            if caption:
                lines.append(f"> {caption}\n")
            lines.append("")

    if videos:
        lines.append("### 相关视频\n")
        for item in videos:
            yt_url = item["youtube_url"]
            title = item.get("title", "Video")
            channel = item.get("channel", "")
            duration = item.get("duration", 0)
            views = item.get("views", 0)
            mins = duration // 60
            secs = duration % 60

            thumb_file = item.get("thumbnail_file", "")
            thumb_url = item.get("thumbnail_url", "")
            if thumb_file:
                thumb_ref = _rel(media_dir / thumb_file)
            else:
                thumb_ref = thumb_url

            meta = []
            if channel:
                meta.append(channel)
            if duration:
                meta.append(f"{mins}:{secs:02d}")
            if views:
                meta.append(f"{views:,} views")
            meta_str = " · ".join(meta)

            lines.append(f"[![{title}]({thumb_ref})]({yt_url})")
            lines.append(f"> [{title}]({yt_url})")
            if meta_str:
                lines.append(f"> {meta_str}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GIF fetching
# ---------------------------------------------------------------------------

def _find_github_url(paper: dict) -> str:
    """Extract GitHub repo URL from paper's project_urls."""
    for url in (paper.get("project_urls") or []):
        if "github.com" in url:
            # Normalize to https://github.com/owner/repo
            m = re.match(r"https?://github\.com/([^/]+/[^/]+)", url)
            if m:
                return f"https://github.com/{m.group(1).rstrip('/')}"
    return ""


def _fetch_github_gifs(github_url: str, out_dir: Path, max_count: int) -> list[dict]:
    """Download GIFs referenced in a GitHub repo's README."""
    # Build raw README URL
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)", github_url)
    if not m:
        return []
    owner, repo = m.group(1), m.group(2)
    raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/main"

    readme_url = f"{raw_base}/README.md"
    readme = _fetch_text(readme_url)
    if not readme:
        # Try master branch
        readme = _fetch_text(readme_url.replace("/main/", "/master/"))
    if not readme:
        return []

    gif_urls = _extract_gif_urls(readme, raw_base, github_url)

    items = []
    for i, (gif_url, caption) in enumerate(gif_urls[:max_count]):
        filename = f"gif-{i+1}.gif"
        out_path = out_dir / filename
        ok = _download_binary(gif_url, out_path, max_mb=_MAX_GIF_MB)
        if not ok:
            continue
        items.append({
            "type": "gif",
            "filename": filename,
            "caption": caption,
            "source_url": gif_url,
            "width": 0,
            "height": 0,
        })
    return items


def _extract_gif_urls(readme: str, raw_base: str, github_url: str) -> list[tuple[str, str]]:
    """Extract (gif_url, caption) pairs from README text."""
    results = []
    seen = set()

    # Pattern 1: ![caption](url.gif)
    for m in re.finditer(r'!\[([^\]]*)\]\(([^)]+\.gif[^)]*)\)', readme, re.I):
        caption, url = m.group(1).strip(), m.group(2).strip()
        url = _resolve_url(url, raw_base, github_url)
        if url and url not in seen:
            seen.add(url)
            results.append((url, caption))

    # Pattern 2: <img src="...gif"
    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+\.gif[^"\']*)["\']', readme, re.I):
        url = m.group(1).strip()
        url = _resolve_url(url, raw_base, github_url)
        if url and url not in seen:
            seen.add(url)
            # Try to find nearby alt text
            results.append((url, ""))

    # Pattern 3: bare https link ending in .gif
    for m in re.finditer(r'https?://\S+\.gif\b', readme, re.I):
        url = m.group(0).rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            results.append((url, ""))

    return results


def _resolve_url(url: str, raw_base: str, github_url: str) -> str:
    """Convert relative URL to absolute raw githubusercontent URL."""
    if url.startswith("http"):
        # Handle github.com/blob/ links → raw
        url = url.replace("github.com", "raw.githubusercontent.com")
        url = re.sub(r"/blob/", "/", url)
        return url
    # Relative path
    url = url.lstrip("./")
    return f"{raw_base}/{url}"


# ---------------------------------------------------------------------------
# YouTube fetching
# ---------------------------------------------------------------------------

def _fetch_youtube_videos(paper: dict, out_dir: Path, max_count: int) -> list[dict]:
    """Search YouTube for videos about this paper and download thumbnails."""
    title = paper.get("title") or ""
    authors = (paper.get("authors") or [])[:2]
    year = paper.get("year") or ""

    # Build search queries: title-based + author-based
    queries = [
        f'"{title[:60]}" paper',
        f'{title[:50]} {" ".join(authors[:1])} {year}',
    ]

    video_ids = []
    seen_ids: set[str] = set()
    for query in queries:
        ids = _youtube_search(query, limit=cfg("media", "youtube_search_limit", 5))
        for vid in ids:
            if vid not in seen_ids:
                seen_ids.add(vid)
                video_ids.append(vid)
        if len(video_ids) >= max_count:
            break

    items = []
    for i, vid_id in enumerate(video_ids[:max_count]):
        item = _get_video_metadata(vid_id, out_dir, i + 1)
        if item:
            items.append(item)
        time.sleep(cfg("media", "delay_between_videos", 0.5))

    return items


def _youtube_search(query: str, limit: int = 5) -> list[str]:
    """Search YouTube and return video IDs."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.youtube.com/results?search_query={encoded}"
    html = _fetch_text(url)
    if not html:
        return []
    ids = re.findall(r'"videoId":"([\w-]{11})"', html)
    seen = set()
    unique = []
    for v in ids:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique[:limit]


def _get_video_metadata(video_id: str, out_dir: Path, index: int) -> dict | None:
    """Use yt-dlp to get video metadata + thumbnail (no video download)."""
    thumb_filename = f"youtube-{index}.jpg"
    thumb_path = out_dir / thumb_filename
    info_path = out_dir / f"youtube-{index}.info.json"

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--write-thumbnail",
                "--write-info-json",
                "--convert-thumbnails", "jpg",
                "--extractor-args", "youtube:player_client=default",
                "-o", str(out_dir / f"youtube-{index}"),
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # yt-dlp not available or timed out — just store the URL + thumbnail URL
        thumb_url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        return {
            "type": "youtube",
            "video_id": video_id,
            "title": "",
            "channel": "",
            "duration": 0,
            "views": 0,
            "thumbnail_url": thumb_url,
            "thumbnail_file": "",
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        }

    # Parse info json
    info = {}
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Find the downloaded thumbnail (may be .jpg or .webp)
    thumb_file = ""
    for ext in ("jpg", "webp", "png"):
        candidate = out_dir / f"youtube-{index}.{ext}"
        if candidate.exists():
            # Rename to consistent .jpg name
            if ext != "jpg":
                candidate.rename(thumb_path)
            thumb_file = thumb_filename
            break

    return {
        "type": "youtube",
        "video_id": video_id,
        "title": info.get("title", ""),
        "channel": info.get("channel", info.get("uploader", "")),
        "duration": info.get("duration", 0),
        "views": info.get("view_count", 0),
        "thumbnail_url": info.get("thumbnail", f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"),
        "thumbnail_file": thumb_file,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import urllib.parse  # needed by _youtube_search


def _fetch_text(url: str, timeout: int = 12) -> str:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _download_binary(url: str, out_path: Path, max_mb: int = 20, timeout: int = 30) -> bool:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            # Check Content-Length before reading
            cl = int(r.headers.get("Content-Length", 0))
            if cl > max_mb * 1024 * 1024:
                return False
            data = r.read(max_mb * 1024 * 1024 + 1)
        if len(data) < 512 or len(data) > max_mb * 1024 * 1024:
            return False
        out_path.write_bytes(data)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Download paper GIFs and YouTube video info")
    parser.add_argument("--arxiv-id", default="")
    parser.add_argument("--title", default="", help="Paper title (for YouTube search)")
    parser.add_argument("--authors", nargs="*", default=[])
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--github", default="", help="GitHub repo URL")
    parser.add_argument("--project-urls", nargs="*", default=[])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--no-gifs", action="store_true")
    parser.add_argument("--no-youtube", action="store_true")
    parser.add_argument("--max-gifs", type=int, default=_MAX_GIF_COUNT)
    parser.add_argument("--max-youtube", type=int, default=_MAX_YOUTUBE)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()

    project_urls = args.project_urls or []
    if args.github:
        project_urls.append(args.github)

    paper = {
        "arxiv_id": args.arxiv_id,
        "title": args.title,
        "authors": args.authors,
        "year": args.year,
        "project_urls": project_urls,
    }

    items = fetch_media(
        paper, args.out_dir,
        fetch_gifs=not args.no_gifs,
        fetch_youtube=not args.no_youtube,
        max_gif_count=args.max_gifs,
        max_youtube=args.max_youtube,
    )

    gifs = [i for i in items if i["type"] == "gif"]
    vids = [i for i in items if i["type"] == "youtube"]
    print(f"Downloaded {len(gifs)} GIFs, {len(vids)} YouTube videos → {args.out_dir}/media.json")
    for i in items:
        if i["type"] == "gif":
            print(f"  [GIF] {i['filename']}  {i.get('caption','')[:50]}")
        else:
            print(f"  [YT]  {i['video_id']}  {i.get('title','')[:50]}")

    if args.markdown:
        md = media_to_markdown(items, args.out_dir)
        print("\n" + md)


if __name__ == "__main__":
    main()
