#!/usr/bin/env python3
"""
Search the web for blog posts, tutorials, and YouTube videos about a paper/topic.

Usage:
    python search_web_resources.py "JEPA joint embedding predictive architecture"
    python search_web_resources.py "3D Gaussian Splatting" --max-articles 5 --max-videos 5

Output: Markdown-formatted list of articles and videos.
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
}


def _fetch(url: str, timeout: int = 12) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  [WARN] fetch failed for {url}: {e}", file=sys.stderr)
        return None


def search_bing(query: str, max_results: int = 8) -> list[dict]:
    """Search Bing (no API key, HTML scraping)."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&count={max_results}"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for item in soup.select("li.b_algo"):
        title_el = item.select_one("h2 a")
        snippet_el = item.select_one(".b_caption p") or item.select_one("p")
        if not title_el:
            continue
        href = title_el.get("href", "")
        title = title_el.get_text(strip=True)
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        if href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def search_ddg(query: str, max_results: int = 8) -> list[dict]:
    """Use DuckDuckGo HTML search (no API key needed). Falls back to Bing if unreachable."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    html = _fetch(url)
    if not html:
        # DDG blocked in some environments — try Bing
        return search_bing(query, max_results)
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for result in soup.select(".result"):
        title_el = result.select_one(".result__title a")
        snippet_el = result.select_one(".result__snippet")
        if not title_el:
            continue
        href = title_el.get("href", "")
        # DDG wraps URLs
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            href = urllib.parse.unquote(m.group(1))
        title = title_el.get_text(strip=True)
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        if href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break
    if not results:
        # DDG returned no results — fall back to Bing
        return search_bing(query, max_results)
    return results


def search_sogou_weixin(query: str, limit: int = 8) -> list[dict]:
    """Search Sogou WeChat (weixin.sogou.com) for public account articles."""
    encoded = urllib.parse.quote(query)
    url = f"https://weixin.sogou.com/weixin?type=2&query={encoded}&ie=utf8"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    for a in soup.find_all("a", href=lambda h: h and "/link?url=" in h):
        title = a.get_text(strip=True)
        href = "https://weixin.sogou.com" + a.get("href", "") if a.get("href", "").startswith("/") else a.get("href", "")
        if not title or len(title) < 6 or href in seen:
            continue
        seen.add(href)
        # Extract snippet from parent container
        snippet = ""
        parent = a.find_parent(["li", "div"])
        if parent:
            for p in parent.find_all("p"):
                t = p.get_text(strip=True)
                if len(t) > 20 and t != title:
                    snippet = t[:150]
                    break
        results.append({"title": title, "url": href, "snippet": snippet, "source": "weixin"})
        if len(results) >= limit:
            break
    print(f"  [Sogou WeChat] '{query[:40]}' → {len(results)} articles", file=sys.stderr)
    return results


def search_articles(topic: str, max_results: int = cfg("web_resources", "max_articles", 6)) -> list[dict]:
    """
    Multi-source article search. Runs platform-targeted Bing queries to diversify
    results beyond a single search engine's top hits.

    Sources tried (in order):
      EN: Medium, Towards Data Science, HuggingFace Blog, general English blogs
      ZH: WeChat public accounts (mp.weixin.qq.com), Zhihu
      Social: Twitter/X posts (site:x.com)
    """
    # Each tuple: (bing query, source label)
    # setlang=en forces Bing to return English results regardless of query language
    def bing_en(q):
        return f"https://www.bing.com/search?q={urllib.parse.quote_plus(q)}&setlang=en&cc=US&count=5"

    seen_urls: set = set()
    articles: list = []
    skip_domains = {"arxiv.org", ".pdf", "youtube.com", "youtu.be"}

    def _add(r: dict):
        url = r.get("url", "")
        if any(x in url for x in skip_domains) or url in seen_urls:
            return False
        seen_urls.add(url)
        # Infer source from actual URL
        if "medium.com" in url:            r["source"] = "medium"
        elif "towardsdatascience.com" in url: r["source"] = "towardsdatascience"
        elif "huggingface.co" in url:      r["source"] = "huggingface"
        elif "mp.weixin.qq.com" in url or "weixin.sogou.com" in url: r["source"] = "weixin"
        elif "zhihu.com" in url:           r["source"] = "zhihu"
        elif "x.com" in url or "twitter.com" in url: r["source"] = "twitter"
        elif "github.io" in url or "github.com" in url: r["source"] = "blog"
        else:                              r["source"] = r.get("source", "web")
        articles.append(r)
        return True

    # 1. Sogou WeChat — dedicated Chinese public accounts search (most reliable CN source)
    half = max(max_results // 2, cfg("web_resources", "sogou_limit", 3))
    for r in search_sogou_weixin(topic, limit=half):
        if len(articles) >= max_results:
            break
        _add(r)
    time.sleep(cfg("web_resources", "delay_between_sources", 0.5))

    # 2. Bing for English + remaining CN sources
    bing_queries = [
        f'site:medium.com {topic}',
        f'site:huggingface.co {topic}',
        f'site:zhihu.com {topic}',
        f'{topic} paper explained -site:zhihu.com',
    ]
    for q in bing_queries:
        if len(articles) >= max_results:
            break
        for r in search_bing(q, max_results=cfg("web_resources", "articles_per_page", 3)):
            if len(articles) >= max_results:
                break
            _add(r)
        time.sleep(cfg("web_resources", "delay_between_queries", 0.3))

    return articles


def search_youtube_videos(topic: str, max_results: int = cfg("web_resources", "max_videos", 6)) -> list[dict]:
    """Search YouTube for videos about the topic using YouTube search page."""
    encoded = urllib.parse.quote_plus(topic + " paper explained")
    url = f"https://www.youtube.com/results?search_query={encoded}"
    html = _fetch(url)
    if not html:
        return []

    # Extract JSON from page
    videos = []
    m = re.search(r'var ytInitialData\s*=\s*(\{.*?\});</script>', html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            contents = (
                data.get("contents", {})
                .get("twoColumnSearchResultsRenderer", {})
                .get("primaryContents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
            )
            for section in contents:
                items = section.get("itemSectionRenderer", {}).get("contents", [])
                for item in items:
                    vr = item.get("videoRenderer", {})
                    if not vr:
                        continue
                    vid_id = vr.get("videoId", "")
                    title = vr.get("title", {}).get("runs", [{}])[0].get("text", "")
                    channel = (
                        vr.get("ownerText", {}).get("runs", [{}])[0].get("text", "")
                        or vr.get("longBylineText", {}).get("runs", [{}])[0].get("text", "")
                    )
                    duration = vr.get("lengthText", {}).get("simpleText", "")
                    views = vr.get("viewCountText", {}).get("simpleText", "")
                    snippet_runs = vr.get("descriptionSnippet", {}).get("runs", [])
                    snippet = "".join(r.get("text", "") for r in snippet_runs)
                    if vid_id and title:
                        videos.append({
                            "title": title,
                            "url": f"https://www.youtube.com/watch?v={vid_id}",
                            "channel": channel,
                            "duration": duration,
                            "views": views,
                            "snippet": snippet[:200],
                        })
                    if len(videos) >= max_results:
                        return videos
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: regex extraction
    if not videos:
        for m in re.finditer(r'"videoId":"([a-zA-Z0-9_-]{11})".*?"text":"([^"]{10,100})"', html):
            vid_id, title = m.group(1), m.group(2)
            if not any(v["url"].endswith(vid_id) for v in videos):
                videos.append({
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "channel": "", "duration": "", "views": "", "snippet": "",
                })
            if len(videos) >= max_results:
                break

    return videos


def search_github_projects(topic: str, max_results: int = cfg("web_resources", "max_github", 2)) -> list[dict]:
    """Search GitHub for project repos related to the topic."""
    encoded = urllib.parse.quote_plus(topic)
    url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&per_page={max_results}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "paper-search-kg/1.0", "Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        repos = []
        for item in data.get("items", [])[:max_results]:
            repos.append({
                "name": item.get("full_name", ""),
                "url": item.get("html_url", ""),
                "description": (item.get("description") or "")[:150],
                "stars": item.get("stargazers_count", 0),
                "language": item.get("language", ""),
                "updated": (item.get("updated_at") or "")[:10],
            })
        return repos
    except Exception as e:
        print(f"  [WARN] GitHub search failed: {e}", file=sys.stderr)
        return []


def format_output(topic: str, articles: list, videos: list, github: list) -> str:
    lines = [f"## Web Resources: {topic}\n"]

    if articles:
        lines.append("### 📝 Blog Posts & Tutorials\n")
        source_icons = {
            "medium": "📖 Medium", "towardsdatascience": "📖 TDS",
            "huggingface": "🤗 HuggingFace", "lilianweng": "📖 Lilian Weng",
            "blog": "📖 Blog", "general_en": "🌐 Web",
            "weixin": "💬 微信", "zhihu": "💬 知乎",
            "twitter": "🐦 Twitter/X",
        }
        for i, a in enumerate(articles, 1):
            src = source_icons.get(a.get("source", ""), "🌐")
            lines.append(f"{i}. {src} **[{a['title']}]({a['url']})**")
            if a.get("snippet"):
                lines.append(f"   > {a['snippet'][:150]}")
            lines.append("")

    if videos:
        lines.append("### 🎥 YouTube Videos\n")
        for i, v in enumerate(videos, 1):
            meta = []
            if v.get("channel"):
                meta.append(f"by {v['channel']}")
            if v.get("duration"):
                meta.append(v["duration"])
            if v.get("views"):
                meta.append(v["views"])
            meta_str = " · ".join(meta)
            lines.append(f"{i}. **[{v['title']}]({v['url']})**")
            if meta_str:
                lines.append(f"   `{meta_str}`")
            if v.get("snippet"):
                lines.append(f"   > {v['snippet'][:150]}")
            lines.append("")

    if github:
        lines.append("### 💻 GitHub Projects\n")
        for i, g in enumerate(github, 1):
            lines.append(f"{i}. **[{g['name']}]({g['url']})** ⭐ {g['stars']:,}")
            if g.get("language"):
                lines.append(f"   Language: {g['language']}")
            if g.get("description"):
                lines.append(f"   > {g['description']}")
            lines.append("")

    if not articles and not videos and not github:
        lines.append("_No resources found. Try a different query._")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Search web resources for a paper/topic")
    ap.add_argument("topic", help="Paper name or topic to search")
    ap.add_argument("--max-articles", type=int, default=6, help="Max blog articles to return")
    ap.add_argument("--max-videos", type=int, default=5, help="Max YouTube videos to return")
    ap.add_argument("--max-github", type=int, default=4, help="Max GitHub repos to return")
    ap.add_argument("--no-github", action="store_true", help="Skip GitHub search")
    ap.add_argument("--no-videos", action="store_true", help="Skip YouTube search")
    args = ap.parse_args()

    print(f"[WEB] Searching articles for: {args.topic}", file=sys.stderr)
    articles = search_articles(args.topic, args.max_articles)

    videos = []
    if not args.no_videos:
        print(f"[WEB] Searching YouTube for: {args.topic}", file=sys.stderr)
        videos = search_youtube_videos(args.topic, args.max_videos)

    github = []
    if not args.no_github:
        print(f"[WEB] Searching GitHub for: {args.topic}", file=sys.stderr)
        github = search_github_projects(args.topic, args.max_github)

    print(format_output(args.topic, articles, videos, github))


if __name__ == "__main__":
    main()
