#!/usr/bin/env python3
"""
ingest_article.py — Ingest web articles (WeChat / Zhihu / Xiaohongshu) into the wiki.

Usage:
    # Fetch and ingest a WeChat article
    python ingest_article.py --url "https://mp.weixin.qq.com/s/..." \\
        --wiki-dir /path/to/wiki --entries /path/to/entries.json

    # Use already-fetched markdown (skip dokobot)
    python ingest_article.py --no-fetch --input article.md \\
        --wiki-dir /path/to/wiki --entries /path/to/entries.json

    # Dry run (analyze only, no file writes)
    python ingest_article.py --url "..." --wiki-dir ... --entries ... --dry-run
"""

import argparse
import base64
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path
from entry_semantics import DEFAULT_FALLBACK_CATEGORY, looks_research_entry
from entry_store import (
    add_entries_argument,
    entries_file_label,
    entry_type_label,
    load_entries,
    save_entries,
)
from llm_cli_utils import (
    anthropic_client_kwargs,
    call_llm,
    describe_provider_selection,
    resolve_model_arg,
    resolve_provider,
)
from organize_images import organize_pasted_images
from template_utils import (
    markdown_bullets,
    render_template,
    resolve_template,
    yaml_string,
)

from config_loader import cfg, get_wiki_paths

TODAY = date.today().isoformat()
SCRIPT_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Dynamic imports from sibling scripts
# ---------------------------------------------------------------------------

def _import_enrich_wiki():
    spec = importlib.util.spec_from_file_location(
        "enrich_wiki", SCRIPT_DIR / "enrich_wiki.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_search_papers_web():
    spec = importlib.util.spec_from_file_location(
        "search_papers_web", SCRIPT_DIR / "search_papers_web.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_search_web_resources():
    spec = importlib.util.spec_from_file_location(
        "search_web_resources", SCRIPT_DIR / "search_web_resources.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_build_paper_wiki():
    spec = importlib.util.spec_from_file_location(
        "build_paper_wiki", SCRIPT_DIR / "build_paper_wiki.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_ingest_paper():
    spec = importlib.util.spec_from_file_location(
        "ingest_paper", SCRIPT_DIR / "ingest_paper.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def _detect_platform(url: str) -> str:
    if "mp.weixin.qq.com" in url:
        return "weixin"
    if "zhuanlan.zhihu.com" in url or "zhihu.com" in url:
        return "zhihu"
    if "xiaohongshu.com" in url or "xhslink.com" in url:
        return "xiaohongshu"
    if "x.com" in url or "twitter.com" in url:
        return "twitter"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "github.com" in url:
        # Distinguish: repo page vs issue/PR/discussion vs release
        if "/issues/" in url:
            return "github_issue"
        if "/pull/" in url:
            return "github_pr"
        if "/discussions/" in url:
            return "github_discussion"
        return "github"
    if "substack.com" in url:
        return "substack"
    if "medium.com" in url:
        return "medium"
    if "notion.so" in url or "notion.site" in url:
        return "notion"
    if "huggingface.co" in url:
        return "huggingface"
    return "article"


PLATFORM_DISPLAY = {
    "weixin": "微信公众号",
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


# ---------------------------------------------------------------------------
# Step 1: Fetch article via dokobot
# ---------------------------------------------------------------------------

def _fetch_youtube_as_markdown(url: str) -> str:
    """Fetch YouTube video metadata + transcript as Markdown using yt-dlp."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        print(
            "[error] yt-dlp is not installed.\n"
            "  Install: pip install yt-dlp  or  brew install yt-dlp",
            file=sys.stderr,
        )
        sys.exit(1)

    import tempfile, os
    with tempfile.TemporaryDirectory() as tmpdir:
        # Get video info JSON
        info_result = subprocess.run(
            [yt_dlp, "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=60,
        )
        if info_result.returncode != 0:
            print(f"[error] yt-dlp failed:\n{info_result.stderr[:400]}", file=sys.stderr)
            sys.exit(1)

        try:
            info = json.loads(info_result.stdout)
        except json.JSONDecodeError:
            info = {}

        title = info.get("title", "YouTube Video")
        channel = info.get("channel", info.get("uploader", ""))
        upload_date = info.get("upload_date", "")
        if len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        duration = info.get("duration", 0)
        view_count = info.get("view_count", 0)
        description = (info.get("description") or "")[:2000]
        tags = ", ".join((info.get("tags") or [])[:10])
        webpage_url = info.get("webpage_url", url)

        # Try to get auto-generated subtitles / captions
        subtitle_path = None
        sub_result = subprocess.run(
            [yt_dlp, "--write-auto-sub", "--sub-lang", "zh-Hans,zh,en",
             "--skip-download", "--no-playlist",
             "-o", os.path.join(tmpdir, "sub"), url],
            capture_output=True, text=True, timeout=90,
        )
        for ext in [".zh-Hans.vtt", ".zh.vtt", ".en.vtt", ".zh-Hans.srt", ".zh.srt", ".en.srt"]:
            candidate = Path(tmpdir) / f"sub{ext}"
            if candidate.exists():
                subtitle_path = candidate
                break

        transcript = ""
        if subtitle_path:
            raw = subtitle_path.read_text(encoding="utf-8", errors="ignore")
            # Strip VTT/SRT headers and timing lines
            lines = []
            for line in raw.splitlines():
                if re.match(r"^\d+$", line.strip()):
                    continue
                if re.match(r"[\d:,.]+\s*-->\s*[\d:,.]+", line):
                    continue
                if line.strip() in ("WEBVTT", ""):
                    continue
                # Strip VTT cue tags like <00:00:01.000><c>text</c>
                line = re.sub(r"<[^>]+>", "", line)
                if line.strip():
                    lines.append(line.strip())
            # Deduplicate adjacent identical lines (common in auto-subs)
            deduped = []
            prev = None
            for l in lines:
                if l != prev:
                    deduped.append(l)
                prev = l
            transcript = "\n".join(deduped[:600])  # cap at ~600 lines

    md = f"# {title}\n\n> {webpage_url}\n\n"
    md += f"**频道**: {channel}  \n"
    md += f"**发布日期**: {upload_date}  \n"
    md += f"**时长**: {duration // 60}分{duration % 60}秒  \n"
    md += f"**播放量**: {view_count:,}  \n"
    if tags:
        md += f"**标签**: {tags}  \n"
    md += f"\n## 视频描述\n\n{description}\n"
    if transcript:
        md += f"\n## 字幕/转录文本\n\n{transcript}\n"
    return md


def _fetch_github_as_markdown(url: str) -> str:
    """Fetch GitHub repo README / issue / PR / discussion as Markdown."""
    platform = _detect_platform(url)

    if platform == "github":
        # Repo main page — fetch README via raw API
        m = re.match(r"https?://github\.com/([^/]+)/([^/\s?#]+)", url)
        if not m:
            return _fetch_via_dokobot(url)
        owner, repo = m.group(1), m.group(2).rstrip("/")
        md = f"# GitHub: {owner}/{repo}\n\n> {url}\n\n"
        # Try main then master for README
        for branch in ["main", "master"]:
            for readme_name in ["README.md", "readme.md", "README.rst"]:
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme_name}"
                try:
                    req = urllib.request.Request(raw_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        readme = resp.read().decode("utf-8", errors="replace")
                    md += f"## README\n\n{readme[:8000]}\n"
                    return md
                except Exception:
                    continue
        # Fall back to repo description via GitHub API
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                repo_info = json.loads(resp.read())
            md += f"**描述**: {repo_info.get('description', '—')}\n"
            md += f"**Stars**: {repo_info.get('stargazers_count', 0):,}\n"
            md += f"**语言**: {repo_info.get('language', '—')}\n"
        except Exception:
            pass
        return md

    if platform in ("github_issue", "github_pr"):
        # Extract owner/repo/number from URL
        m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/(issues|pull)/(\d+)", url)
        if not m:
            return _fetch_via_dokobot(url)
        owner, repo, kind, number = m.group(1), m.group(2), m.group(3), m.group(4)
        api_kind = "issues" if kind == "issues" else "pulls"
        api_url = f"https://api.github.com/repos/{owner}/{repo}/{api_kind}/{number}"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                issue = json.loads(resp.read())
            title = issue.get("title", "")
            body = (issue.get("body") or "")[:4000]
            state = issue.get("state", "")
            user = issue.get("user", {}).get("login", "")
            created = (issue.get("created_at") or "")[:10]
            md = f"# {title}\n\n> {url}\n\n"
            md += f"**状态**: {state}  \n**作者**: {user}  \n**创建日期**: {created}\n\n"
            md += f"## 内容\n\n{body}\n"
            # Fetch comments
            comments_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments"
            try:
                req2 = urllib.request.Request(comments_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req2, timeout=15) as resp2:
                    comments = json.loads(resp2.read())
                if comments:
                    md += "\n## 评论\n\n"
                    for c in comments[:20]:
                        cuser = c.get("user", {}).get("login", "")
                        cbody = (c.get("body") or "")[:800]
                        md += f"**{cuser}**: {cbody}\n\n---\n\n"
            except Exception:
                pass
            return md
        except Exception:
            return _fetch_via_dokobot(url)

    if platform == "github_discussion":
        # GitHub Discussions don't have a public JSON API without auth — fall back to dokobot
        return _fetch_via_dokobot(url)

    return _fetch_via_dokobot(url)


def _fetch_via_dokobot(url: str) -> str:
    """Generic fetch via dokobot (handles JS-rendered pages, auth walls)."""
    dokobot_bin = shutil.which("dokobot")
    if not dokobot_bin:
        print(
            "[error] dokobot is not installed or not on PATH.\n"
            "  Install: pip install dokobot  or  npm install -g dokobot",
            file=sys.stderr,
        )
        sys.exit(1)

    # On Windows, we often need shell=True and double-quoting to handle complex URLs
    is_windows = sys.platform == "win32"
    if is_windows:
        command = f'"{dokobot_bin}" read --local "{url}"'
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            shell=True,
            timeout=cfg("llm", "timeout", 300),
        )
    else:
        result = subprocess.run(
            [dokobot_bin, "read", "--local", url],
            capture_output=True,
            text=True,
            timeout=cfg("llm", "timeout", 300),
        )

    if result.returncode != 0:
        print(
            f"[error] dokobot failed (exit {result.returncode}):\n{result.stderr[:500]}",
            file=sys.stderr,
        )
        sys.exit(1)
    return result.stdout


def _fetch_via_baoyu_url_to_markdown(url: str) -> str | None:
    """Fetch article using baoyu-url-to-markdown vendor tool."""
    vendor_script = SCRIPT_DIR.parent / "vendor" / "baoyu-url-to-markdown" / "scripts" / "main.ts"
    if not vendor_script.exists():
        return None

    bun_bin = shutil.which("bun")
    cmd = ([bun_bin, str(vendor_script)] if bun_bin
           else ["npx", "-y", "bun", str(vendor_script)])

    tmp_file = Path(tempfile.mktemp(suffix=".md"))
    print(f"[fetch] using baoyu-url-to-markdown (vendor)...")
    try:
        result = subprocess.run(
            cmd + [url, "-o", str(tmp_file)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and tmp_file.exists():
            content = tmp_file.read_text(encoding="utf-8")
            if content.strip():
                return content
        print(f"[fetch] baoyu script failed (exit {result.returncode})")
    except Exception as e:
        print(f"[fetch] baoyu script error ({e})")
    finally:
        tmp_file.unlink(missing_ok=True)
    return None


def _fetch_twitter_as_markdown(url: str) -> str:
    """
    Fetch a Twitter/X tweet or thread as Markdown.

    Primary: vendor/baoyu-danger-x-to-markdown (reverse-engineered API, needs bun).
    Fallback: dokobot (needs browser login session).
    """
    # Resolve vendor script relative to this file — no hardcoded absolute paths
    vendor_script = SCRIPT_DIR.parent / "vendor" / "baoyu-danger-x-to-markdown" / "scripts" / "main.ts"

    if vendor_script.exists():
        # Resolve bun: PATH first, then npx -y bun as fallback
        bun_bin = shutil.which("bun")
        cmd = ([bun_bin, str(vendor_script)] if bun_bin
               else ["npx", "-y", "bun", str(vendor_script)])

        tmp_file = Path(tempfile.mktemp(suffix=".md"))
        print(f"[fetch] Twitter: using baoyu-danger-x-to-markdown (vendor)...")
        try:
            result = subprocess.run(
                cmd + [url, "-o", str(tmp_file)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0 and tmp_file.exists():
                content = tmp_file.read_text(encoding="utf-8")
                if content.strip():
                    return content
            # Non-zero exit or empty output — fall through to dokobot
            print(f"[fetch] baoyu script failed (exit {result.returncode}), falling back to dokobot...")
        except Exception as e:
            print(f"[fetch] baoyu script error ({e}), falling back to dokobot...")
        finally:
            tmp_file.unlink(missing_ok=True)
    else:
        print(f"[fetch] vendor/baoyu-danger-x-to-markdown not found at {vendor_script}, falling back to dokobot...")

    # Fallback: dokobot (requires browser login to x.com)
    return _fetch_via_dokobot(url)


def fetch_article_markdown(url: str) -> str:
    """Fetch article as Markdown. Routes to platform-specific fetcher or dokobot."""
    platform = _detect_platform(url)

    if platform == "twitter":
        return _fetch_twitter_as_markdown(url)

    if platform == "youtube":
        print(f"[fetch] YouTube URL detected — using yt-dlp for metadata + transcript...")
        return _fetch_youtube_as_markdown(url)

    if platform in ("github", "github_issue", "github_pr", "github_discussion"):
        print(f"[fetch] GitHub URL detected ({platform}) — using GitHub API + raw content...")
        return _fetch_github_as_markdown(url)

    # Primary for generic web articles: baoyu-url-to-markdown
    content = _fetch_via_baoyu_url_to_markdown(url)
    if content:
        return content

    # Fallback: dokobot
    return _fetch_via_dokobot(url)


def parse_markdown_meta(md: str, fallback_url: str = "") -> tuple[str, str]:
    """
    Extract title and source URL from fetched Markdown.
    Looks for:
      - First `# Title` line → title
      - `> url` blockquote line → source URL
    """
    title = ""
    source_url = fallback_url

    for line in md.splitlines():
        line_stripped = line.strip()
        if not title and line_stripped.startswith("# "):
            title = line_stripped[2:].strip()
        if line_stripped.startswith("> ") and not title.startswith(">"):
            candidate = line_stripped[2:].strip()
            if candidate.startswith("http"):
                source_url = candidate
                break

    return title, source_url


# ---------------------------------------------------------------------------
# Step 1b: Image extraction — download inline images and OCR with Claude vision
# ---------------------------------------------------------------------------

# CDN patterns that indicate inline article images (not icons/avatars)
_IMAGE_URL_PATTERNS = [
    r'https?://[^\s\)\]"\']+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s\)\]"\']*)?',
    r'https?://[^\s\)\]"\']+/notes_pre_post/[^\s\)\]"\']+',  # Xiaohongshu CDN
    r'https?://[^\s\)\]"\']+mmbiz[^\s\)\]"\']+',             # WeChat image CDN
]

_AVATAR_PATTERNS = [
    "avatar", "profile", "icon", "logo", "thumb", "head", "face",
    "wx_fmt=gif", "tp=webp&wxfrom", "mmbiz_gif",
]

_MIN_IMAGE_SIZE_KB = 10  # skip tiny images (icons, decorations)


def _extract_image_urls(md: str) -> list[str]:
    """Extract unique image URLs from Markdown text."""
    seen = set()
    urls = []
    combined = "|".join(_IMAGE_URL_PATTERNS)
    for m in re.finditer(combined, md):
        u = m.group(0).rstrip(".,;)")
        if u in seen:
            continue
        seen.add(u)
        # Skip avatars and decorative images
        if any(p in u.lower() for p in _AVATAR_PATTERNS):
            continue
        urls.append(u)
    return urls


def _download_image(url: str, timeout: int = 20) -> bytes | None:
    """Download image bytes from URL. Returns None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if len(data) < _MIN_IMAGE_SIZE_KB * 1024:
            return None
        return data
    except Exception:
        return None


def _to_png_bytes(image_data: bytes) -> bytes | None:
    """Convert image bytes (any format) to PNG bytes using PIL."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_data))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _ocr_image_with_llm(png_bytes: bytes, provider: str, model: str) -> str:
    """Use a vision-capable API provider to extract text from a PNG image."""

    prompt = (
        "请完整提取这张图片中的所有文字内容，保持原有的结构和层次。"
        "如果是信息图/图表，用Markdown格式描述各部分内容。"
        "只输出文字内容，不要说明这是图片分析。"
    )

    try:
        try:
            provider = resolve_provider(
                provider,
                vision=True,
                allowed={"openai", "anthropic"},
            )
        except RuntimeError:
            return ""
        effective_model = resolve_model_arg(provider, model)

        if provider == "anthropic":
            import anthropic
            b64 = base64.standard_b64encode(png_bytes).decode()
            client_kwargs = anthropic_client_kwargs()
            if not client_kwargs:
                return ""
            client = anthropic.Anthropic(**client_kwargs)
            resp = client.messages.create(
                model=effective_model or cfg("llm", "model", "claude-sonnet-4-6"),
                max_tokens=2000,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": b64
                    }},
                    {"type": "text", "text": prompt},
                ]}],
            )
            return resp.content[0].text.strip()

        if provider == "openai":
            from openai import OpenAI
            from llm_cli_utils import openai_client_kwargs
            b64 = base64.standard_b64encode(png_bytes).decode()
            client_kwargs = openai_client_kwargs(model=effective_model)
            if not client_kwargs.get("api_key"):
                return ""
            client = OpenAI(**client_kwargs)
            resp = client.chat.completions.create(
                model=effective_model or "gpt-4o",
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{b64}"
                    }},
                    {"type": "text", "text": prompt},
                ]}],
            )
            return resp.choices[0].message.content.strip()

        return ""
    except Exception as e:
        print(f"  [ocr] vision call failed: {e}", file=sys.stderr)
        return ""


def extract_images_from_article(
    article_md: str,
    platform: str,
    provider: str = "auto",
    model: str = "",
    save_dir: Path | None = None,
) -> tuple[str, list[Path]]:
    """
    Find inline images in article_md, download + OCR each one.

    If save_dir is provided, save each PNG there as img-1.png, img-2.png, ...

    Returns:
        (ocr_text, saved_paths)
        - ocr_text: Markdown block with extracted text (to append to article_md for LLM)
        - saved_paths: list of Path objects of saved PNG files
    """
    urls = _extract_image_urls(article_md)
    if not urls:
        return "", []

    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)

    print(f"[images] Found {len(urls)} image(s), downloading + OCR...", flush=True)
    extracted_blocks = []
    saved_paths: list[Path] = []

    for i, url in enumerate(urls, 1):
        print(f"  [img {i}/{len(urls)}] downloading...", flush=True)
        data = _download_image(url)
        if data is None:
            print(f"  [img {i}] skip (too small or download failed)")
            continue

        png = _to_png_bytes(data)
        if png is None:
            print(f"  [img {i}] skip (could not decode image)")
            continue

        # Save PNG to wiki figures dir
        if save_dir is not None:
            img_path = save_dir / f"img-{i}.png"
            img_path.write_bytes(png)
            saved_paths.append(img_path)
            print(f"  [img {i}] saved → {img_path.name}")

        print(f"  [img {i}] OCR ({len(png)//1024}KB)...")
        text = _ocr_image_with_llm(png, provider=provider, model=model)
        if text:
            extracted_blocks.append(f"### 图片 {i}\n\n{text}")
            print(f"  [img {i}] extracted {len(text)} chars")
        else:
            print(f"  [img {i}] no text extracted")

    ocr_text = ""
    if extracted_blocks:
        ocr_text = "\n\n---\n\n## 图片内容（自动提取）\n\n" + "\n\n".join(extracted_blocks)

    return ocr_text, saved_paths


# ---------------------------------------------------------------------------
# Step 2: LLM extraction
# ---------------------------------------------------------------------------

EXTRACT_PROMPT_TEMPLATE = """你是一位信息提取专家。请从以下文章的 Markdown 内容中，提取结构化信息，严格按照 JSON 格式输出。

---

{article_md}

---

请提取以下字段，输出**纯 JSON**（不要有 markdown 代码块，不要有其他说明文字）：

{{
  "title": "文章标题",
  "author": "文章作者（个人笔名，如"渣大米"）",
  "account": "公众号/账号全名（如"石麻笔记"）。微信文章头部显示 'Original 作者  公众号符号'，但正文中常出现完整公众号名（如"欢迎关注XX""XX的读者"），优先取正文中出现的完整名称；无法确认时填 null",
  "date": "发布日期（YYYY-MM-DD 或 YYYY-MM 或 YYYY，未知填 null）",
  "platform": "weixin/zhihu/xiaohongshu/twitter/youtube/github/github_issue/github_pr/github_discussion/substack/medium/notion/huggingface/article",
  "summary": "2-3句话总结文章主要内容（≤150字）",
  "key_insights": [
    "观点1（具体简练，≤50字，勿用箭头或特殊符号）",
    "观点2"
  ],
  "entries_mentioned": [
    {{
      "title": "外部条目标题（尽量完整，从文章中提取原文）",
      "entry_type": "paper/article/repo/note/bookmark/generic 之一",
      "authors": ["作者1", "作者2"],
      "author": "单一作者名；没有可填 null",
      "account": "账号/组织/发布主体；没有可填 null",
      "year": 2024,
      "date": "YYYY-MM-DD 或 YYYY-MM 或 YYYY；未知填 null",
      "arxiv_id": "2301.08243",
      "url": "https://...",
      "platform": "github/youtube/weixin/zhihu/article/...；未知填 null",
      "description": "文章中如何描述这篇论文（原文片段或总结）",
      "mention_stance": "支持/对比/批评/中立/引用",
      "key_claim": "文章对这个外部条目提出的最重要观点（≤60字）"
    }}
  ]
}}

要求：
- entries_mentioned 包括文章明确引用、对比、批评、重点推荐的外部条目
- 只提取“被文章当作内容对象讨论”的外部条目；不要把普通导航链接、广告链接、页脚链接算进去
- 如果是研究论文，entry_type 填 paper；如果是仓库/项目页填 repo；如果是外部文章填 article；无法判断时填 generic
- 即使没有 arXiv ID，只要有足够信息（标题+作者/机构+年份或 URL）就包括进来
- arxiv_id 只在文章明确提到时填写，否则填 null
- url 只在文章明确提到该条目的链接时填写，否则填 null
- key_claim 是文章*对该条目*说的最重要一句话（用于写入该条目的知识库页面）
- mention_stance 是文章对该条目的总体态度：支持/对比/批评/中立/引用
"""


def _extract_article_info(
    article_md: str,
    provider: str = "auto",
    model: str = "",
    direct_input: str | None = None,
) -> dict:
    """Call LLM to extract structured info from article Markdown."""
    # Truncate article to avoid overwhelming the context

    max_chars = cfg("article_ingest", "article_max_chars", 30000)
    truncated = article_md[:max_chars]
    if len(article_md) > max_chars:
        truncated += "\n\n[...文章已截断...]"

    prompt = EXTRACT_PROMPT_TEMPLATE.format(article_md=truncated)

    try:
        raw = call_llm(prompt, provider=provider, model=model, direct_input=direct_input)
    except Exception as e:
        print(f"[error] LLM call failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Try to parse JSON from response
    # Strip markdown code blocks if present
    raw_clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw_clean = re.sub(r"\s*```$", "", raw_clean.strip(), flags=re.MULTILINE)

    # Find the outermost JSON object
    m = re.search(r"\{[\s\S]+\}", raw_clean)
    if not m:
        print(f"[error] Could not parse JSON from LLM response:\n{raw[:500]}", file=sys.stderr)
        sys.exit(1)

    json_str = m.group(0)
    try:
        info = json.loads(json_str)
    except json.JSONDecodeError:
        # Fix unescaped control chars and bare quotes inside JSON string values.
        # Also replaces curly/CJK quotes with ASCII equivalents before parsing.
        def _fix_json_strings(s: str) -> str:
            # Replace curly/typographic quotes with ASCII equivalents
            s = s.replace("\u201c", '\\"').replace("\u201d", '\\"')  # " "
            s = s.replace("\u2018", "\\'").replace("\u2019", "\\'")  # ' '
            result = []
            in_string = False
            i = 0
            while i < len(s):
                c = s[i]
                prev_backslash = i > 0 and s[i - 1] == "\\"
                if c == '"' and not prev_backslash:
                    if in_string:
                        # Check if this quote is a legit string-closer:
                        # look ahead for JSON structural chars (:,]}  or whitespace+those)
                        j = i + 1
                        while j < len(s) and s[j] in " \t\r\n":
                            j += 1
                        next_structural = j < len(s) and s[j] in ':,]}'
                        if next_structural:
                            in_string = False
                            result.append(c)
                        else:
                            # Bare quote inside a string value — escape it
                            result.append('\\"')
                    else:
                        in_string = True
                        result.append(c)
                elif in_string and c == "\n":
                    result.append("\\n")
                elif in_string and c == "\r":
                    result.append("\\r")
                elif in_string and c == "\t":
                    result.append("\\t")
                else:
                    result.append(c)
                i += 1
            return "".join(result)

        try:
            info = json.loads(_fix_json_strings(json_str))
        except json.JSONDecodeError as e:
            # Second repair: truncated JSON (LLM stopped mid-output).
            # Close open strings, arrays, and objects to make it parseable.
            def _heal_truncated_json(s: str) -> str:
                fixed = _fix_json_strings(s)
                # Count unclosed braces/brackets
                depth_brace = 0
                depth_bracket = 0
                in_str = False
                for idx, ch in enumerate(fixed):
                    if ch == '"' and (idx == 0 or fixed[idx - 1] != "\\"):
                        in_str = not in_str
                    if in_str:
                        continue
                    if ch == "{":
                        depth_brace += 1
                    elif ch == "}":
                        depth_brace -= 1
                    elif ch == "[":
                        depth_bracket += 1
                    elif ch == "]":
                        depth_bracket -= 1
                # If we're still inside a string, close it
                suffix = ""
                if in_str:
                    suffix += '"'
                # Close any unclosed arrays/objects (in reverse order of opening)
                suffix += "]" * max(0, depth_bracket) + "}" * max(0, depth_brace)
                return fixed + suffix

            try:
                info = json.loads(_heal_truncated_json(json_str))
            except json.JSONDecodeError as e2:
                print(
                    f"[error] JSON parse error after repair: {e2}\nRaw: {raw[:500]}",
                    file=sys.stderr,
                )
                sys.exit(1)

    # Normalize fields
    info.setdefault("title", "")
    info.setdefault("author", "")
    info.setdefault("account", None)
    info.setdefault("date", None)
    info.setdefault("platform", "article")
    info.setdefault("summary", "")
    info.setdefault("key_insights", [])
    info.setdefault("entries_mentioned", [])

    if not info.get("entries_mentioned") and info.get("papers_mentioned"):
        info["entries_mentioned"] = info.get("papers_mentioned", [])
    info["entries_mentioned"] = _normalize_mentioned_entries(info.get("entries_mentioned", []))
    info["papers_mentioned"] = [entry for entry in info["entries_mentioned"] if _is_research_reference(entry)]

    return info


def _normalize_mentioned_entry_type(entry: dict) -> str:
    raw = str(entry.get("entry_type") or "").strip().lower()
    if raw in {"paper", "research", "research_paper", "论文"}:
        return "paper"
    if raw in {"article", "web_article", "blog", "post", "文章"}:
        return "article"
    if raw in {"repo", "repository", "github", "project"}:
        return "generic"
    if raw in {"note", "bookmark", "generic", "url", "doc", "documentation"}:
        return "generic"
    if looks_research_entry(entry):
        return "paper"
    platform = str(entry.get("platform") or "").strip().lower()
    if platform in {"weixin", "zhihu", "xiaohongshu", "twitter", "youtube", "substack", "medium", "notion", "article"}:
        return "article"
    return "generic"


def _normalize_mentioned_entries(raw_entries: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    seen: set[str] = set()
    for item in raw_entries or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        arxiv_id = str(item.get("arxiv_id") or "").strip()
        if not any([title, url, arxiv_id]):
            continue

        authors = item.get("authors") or []
        if not isinstance(authors, list):
            authors = [str(authors)]

        entry = {
            "title": title or url or arxiv_id,
            "entry_type": _normalize_mentioned_entry_type(item),
            "authors": [str(a).strip() for a in authors if str(a).strip()],
            "author": str(item.get("author") or "").strip(),
            "account": str(item.get("account") or "").strip(),
            "year": item.get("year"),
            "date": item.get("date"),
            "arxiv_id": arxiv_id,
            "url": url,
            "source_kind": "url" if url else "meta",
            "platform": str(item.get("platform") or "").strip(),
            "description": str(item.get("description") or "").strip(),
            "mention_stance": str(item.get("mention_stance") or item.get("article_stance") or "引用").strip(),
            "key_claim": str(item.get("key_claim") or "").strip(),
        }
        dedupe_key = arxiv_id or url or entry["title"].casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(entry)
    return normalized


def _is_research_reference(entry: dict) -> bool:
    return _normalize_mentioned_entry_type(entry) == "paper" or looks_research_entry(entry)


# ---------------------------------------------------------------------------
# Step 3: Paper matching and enrichment
# ---------------------------------------------------------------------------

_UNICODE_ASCII_MAP = str.maketrans({
    "π": "pi", "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "θ": "theta", "λ": "lambda", "σ": "sigma", "τ": "tau", "φ": "phi",
    "ω": "omega", "μ": "mu", "ε": "epsilon", "η": "eta", "ρ": "rho",
    "∞": "inf", "≈": "", "→": "", "↔": "", "⊕": "",
})


def _normalize_title(title: str) -> str:
    """Lowercase, normalize unicode math symbols, strip punctuation."""
    t = title.translate(_UNICODE_ASCII_MAP)
    return re.sub(r"[^\w\s]", "", t.lower())


def _title_word_overlap(t1: str, t2: str) -> float:
    """Compute word overlap ratio between two titles (Jaccard on word sets)."""
    words1 = set(_normalize_title(t1).split())
    words2 = set(_normalize_title(t2).split())
    # Remove very common stopwords that inflate false matches
    stopwords = {"a", "an", "the", "of", "for", "in", "on", "and", "or", "with", "to"}
    words1 -= stopwords
    words2 -= stopwords
    if not words1 or not words2:
        return 0.0
    intersection = words1 & words2
    union = words1 | words2
    jaccard = len(intersection) / len(union)
    # Short-title boost: if query has ≤ 2 significant words and all appear in result → strong match
    if len(words1) <= 2 and words1.issubset(words2):
        jaccard = max(jaccard, 0.6)
    return jaccard


def _find_entry_in_kb(entry_info: dict, all_entries: list[dict]) -> dict | None:
    """
    Find an entry in the knowledge base.
    1. If arxiv_id → match by arxiv_id
    2. If url → match by url/source_url
    3. Otherwise → fuzzy title match (>= configured overlap)
    """
    arxiv_id = entry_info.get("arxiv_id")
    title = entry_info.get("title", "")
    url = (entry_info.get("url") or entry_info.get("source_url") or "").strip().rstrip("/")

    if arxiv_id:
        for entry in all_entries:
            if entry.get("arxiv_id") == arxiv_id:
                return entry

    if url:
        for entry in all_entries:
            existing_url = (entry.get("url") or entry.get("source_url") or "").strip().rstrip("/")
            if existing_url and existing_url == url:
                return entry

    if title:
        best_score = 0.0
        best_entry = None
        for entry in all_entries:
            score = _title_word_overlap(title, entry.get("title", ""))
            if score > best_score:
                best_score = score
                best_entry = entry
        if best_score >= cfg("article_ingest", "kb_match_threshold", 0.6):
            return best_entry

    return None


def _find_paper_in_kb(paper_info: dict, all_papers: list[dict]) -> dict | None:
    return _find_entry_in_kb(paper_info, all_papers)


def _entry_slug(entry: dict) -> str:
    """Generate wiki slug for an entry: title-arxiv_id when available."""
    arxiv_id = entry.get("arxiv_id", "")
    title = entry.get("title") or "entry"

    # Exact match of build_paper_wiki.py's _slug function
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:150]

    if arxiv_id:
        return f"{slug}-{arxiv_id}"
    return slug


def _paper_slug_from_paper(paper: dict) -> str:
    return _entry_slug(paper)


def _detect_conflict(
    key_claim: str,
    source_page_path: Path,
    provider: str = "auto",
    model: str = "",
) -> tuple[bool, str]:
    """
    Detect if key_claim conflicts with the entry's 核心观点 section.
    Returns (is_conflict, reason).
    """
    if not source_page_path.exists():
        return False, ""

    ew = _import_enrich_wiki()
    content = source_page_path.read_text(encoding="utf-8")
    core_section = ew._get_section(content, "核心观点")

    # Only check if section is not a stub
    if not core_section or ew._is_stub(core_section):
        return False, ""

    prompt = f"""判断以下「文章观点」是否与「当前条目核心观点」存在**明显矛盾**或**强烈批评**。

当前条目核心观点：
{core_section[:1500]}

文章观点（来自外部文章）：
{key_claim}

请严格按如下 JSON 格式输出，不要输出其他内容：
{{"conflict": true/false, "reason": "简要说明（≤50字，无冲突时填空字符串）"}}"""

    try:
        raw = call_llm(prompt, provider=provider, model=model)
        m = re.search(r"\{[^}]+\}", raw, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
            return bool(result.get("conflict", False)), result.get("reason", "")
    except Exception:
        pass

    return False, ""


def _enrich_existing_entry(
    source_page_path: Path,
    entry_info: dict,
    article_title: str,
    article_url: str,
    article_date: str,
    article_platform: str,
    provider: str = "auto",
    model: str = "",
    dry_run: bool = False,
) -> bool:
    """
    Add external evaluation entry to an existing source page's ## 外部评价 section.
    Returns True if the page was modified.
    """
    if not source_page_path.exists():
        print(f"  [warn] source page not found: {source_page_path}", file=sys.stderr)
        return False

    key_claim = entry_info.get("key_claim", "")
    stance = entry_info.get("mention_stance") or entry_info.get("article_stance", "中立")
    description = entry_info.get("description", "")

    # Detect conflict
    is_conflict, conflict_reason = _detect_conflict(
        key_claim, source_page_path, provider=provider, model=model
    )

    # Build the evaluation entry
    conflict_prefix = ""
    if is_conflict or stance == "批评":
        conflict_prefix = f"⚡ **观点冲突**：{conflict_reason}\n\n" if conflict_reason else "⚡ **观点冲突**\n\n"

    entry_lines = [
        f"### 来自 [{article_title}]({article_url}) · {article_platform} · {article_date}\n",
        f"{conflict_prefix}**文章中的提法**：{key_claim}\n",
        f"\n**态度**：{stance}\n",
    ]
    if description:
        entry_lines.append(f"\n> 原文片段：{description}\n")

    new_entry = "\n".join(entry_lines)

    content = source_page_path.read_text(encoding="utf-8")
    ew = _import_enrich_wiki()

    if "## 外部评价" in content:
        # Append to existing section
        existing_body = ew._get_section(content, "外部评价")
        new_body = (existing_body.rstrip() + "\n\n" + new_entry).strip()
        new_content = ew._replace_section(content, "外部评价", new_body + "\n")
    else:
        # Insert before ## 相关页面 or at end
        new_section = f"\n## 外部评价\n\n{new_entry}\n"
        if "\n## 相关页面" in content:
            new_content = content.replace(
                "\n## 相关页面",
                new_section + "\n## 相关页面",
                1,
            )
        else:
            new_content = content.rstrip() + new_section

    new_content = ew._update_frontmatter_date(new_content)

    if dry_run:
        print(f"  [dry-run] would update 外部评价 in {source_page_path.name}")
        print(f"    entry: {new_entry[:120]}...")
        return True

    source_page_path.write_text(new_content, encoding="utf-8")
    return True


def _enrich_existing_paper(*args, **kwargs) -> bool:
    return _enrich_existing_entry(*args, **kwargs)


def _fetch_official_blog(title: str, paper_info: dict, trusted_domains: tuple) -> dict | None:
    """Fallback 3: use Bing to find official blog/page, then dokobot read to extract arxiv/pdf link."""
    import urllib.request, urllib.parse as urlparse
    try:
        # Bing search for official page
        q = urlparse.quote(f"{title} official paper blog arxiv 2025 OR 2026")
        req = urllib.request.Request(
            f"https://www.bing.com/search?q={q}&count=5",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        )
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
        import re as _re
        links = _re.findall(r'<h2[^>]*>.*?<a[^>]*href="(https://[^"]+)"', html, _re.DOTALL)
        # Filter to trusted lab/company domains
        lab_domains = ("physicalintelligence.company", "pi.website", "openai.com",
                       "deepmind.com", "anthropic.com", "nvidia.com", "developer.nvidia.com",
                       "beingbeyond", "generalist.com", "huggingface.co", "github.io",
                       "arxiv.org", "openreview.net")
        blog_url = next((u for u in links if any(d in u for d in lab_domains)), None)
        if not blog_url:
            return None

        print(f"  [blog]    fetching: {blog_url[:80]}")
        # Use dokobot to read the page
        dokobot_bin = shutil.which("dokobot") or "dokobot"
        result = subprocess.run(
            [dokobot_bin, "read", "--local", blog_url],
            capture_output=True, text=True, timeout=30,
        )
        page_md = result.stdout or ""

        # Extract arxiv ID
        arxiv_match = _re.search(r'arxiv\.org/abs/([\d.]+)', page_md)
        if arxiv_match:
            arxiv_id = arxiv_match.group(1)
            print(f"  [blog]    found arXiv: {arxiv_id}")
            return {
                "title": title,
                "arxiv_id": arxiv_id,
                "authors": paper_info.get("authors", []),
                "year": paper_info.get("year"),
                "citations": 0,
                "url": blog_url,
                "source": "blog",
            }

        # Extract PDF link
        pdf_match = _re.search(r'https://[^\s\)\]"\']+\.pdf', page_md)
        if pdf_match:
            pdf_url = pdf_match.group(0)
            print(f"  [blog]    found PDF: {pdf_url[:80]}")
            return {
                "title": title,
                "arxiv_id": "",
                "authors": paper_info.get("authors", []),
                "year": paper_info.get("year"),
                "citations": 0,
                "url": blog_url,
                "pdf_url": pdf_url,
                "source": "blog",
            }
    except Exception as e:
        print(f"  [blog]    error: {e}", file=sys.stderr)
    return None


def _direct_entry_from_mention(entry_info: dict) -> dict:
    entry_type = _normalize_mentioned_entry_type(entry_info)
    source_url = entry_info.get("url") or ""
    platform = entry_info.get("platform") or _detect_platform(source_url) if source_url else ""
    abstract = entry_info.get("description") or entry_info.get("key_claim") or ""
    new_entry = {
        "title": entry_info.get("title", ""),
        "arxiv_id": entry_info.get("arxiv_id", ""),
        "authors": entry_info.get("authors", []),
        "author": entry_info.get("author", ""),
        "account": entry_info.get("account", ""),
        "year": entry_info.get("year"),
        "date": entry_info.get("date"),
        "abstract": abstract,
        "summary": abstract,
        "citations": 0,
        "url": source_url,
        "platform": platform or "",
        "source_kind": "url" if source_url else "meta",
        "entry_type": "article" if entry_type == "article" else "generic",
        "category": entry_info.get("category") or cfg("wiki", "fallback_category", DEFAULT_FALLBACK_CATEGORY),
    }
    return new_entry


def _ingest_direct_entry(
    entry_info: dict,
    entries: list[dict],
    entries_path: Path,
    wiki_dir: Path,
    dry_run: bool = False,
    template_dir: str | Path | None = None,
) -> tuple[str, dict | None]:
    title = entry_info.get("title", "")
    if not title and not entry_info.get("url"):
        return "not_found", None

    existing = _find_entry_in_kb(entry_info, entries)
    if existing is not None:
        return "already_exists", existing

    new_entry = _direct_entry_from_mention(entry_info)
    template_spec = resolve_template("auto", item=new_entry, template_dir=template_dir)
    new_entry["template_id"] = template_spec.template_id

    if dry_run:
        label = new_entry.get("url") or new_entry.get("title", "")
        print(f"  [dry-run] would ingest entry {label[:80]}")
        return "ingested", new_entry

    entries.append(new_entry)
    save_entries(entries, entries_path)

    sources_dir = wiki_dir / "wiki" / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    page_path = sources_dir / f"{_entry_slug(new_entry)}.md"
    if not page_path.exists():
        _create_source_page_stub(page_path, new_entry, template_dir=template_dir)
    _integrate_new_entry_into_wiki(new_entry, wiki_dir, entries_path)

    return "ingested", new_entry


def _mentioned_entry_type_label(entry: dict) -> str:
    return entry_type_label({
        "template_id": entry.get("template_id"),
        "entry_type": entry.get("entry_type"),
        "source_kind": entry.get("source_kind"),
        "platform": entry.get("platform"),
        "url": entry.get("url"),
        "source_url": entry.get("source_url"),
    })


def _search_and_ingest_paper(
    paper_info: dict,
    papers: list[dict],
    papers_path: Path,
    wiki_dir: Path,
    provider: str = "auto",
    model: str = "",
    dry_run: bool = False,
    template_dir: str | Path | None = None,
) -> tuple[str, dict | None]:
    """
    Search research sources for a paper-like entry not yet in the KB. If found, ingest it.
    Returns ('ingested', paper_dict) | ('not_found', None)
    """
    spw = _import_search_papers_web()
    title = paper_info.get("title", "")

    # Clean title for search: strip parenthesized subtitles, normalize unicode
    search_query = re.sub(r"\s*\([^)]*\)", "", title).strip()  # remove "(subtitle)"
    search_query = search_query.translate(_UNICODE_ASCII_MAP)

    _arxiv_limit   = cfg("article_ingest", "arxiv_search_limit", 5)
    _arxiv_thresh  = cfg("article_ingest", "arxiv_match_threshold", 0.5)
    _or_limit      = cfg("article_ingest", "openreview_search_limit", 5)
    _or_thresh     = cfg("article_ingest", "openreview_match_threshold", 0.5)
    _web_limit     = cfg("article_ingest", "web_search_limit", 5)
    _web_thresh    = cfg("article_ingest", "web_match_threshold", 0.4)
    _web_title_min = cfg("article_ingest", "web_title_min", 0.3)
    _web_boost     = cfg("article_ingest", "web_domain_boost", 0.15)
    _top1_score    = cfg("article_ingest", "arxiv_top1_trust_score", 0.45)
    _trusted_domains  = tuple(cfg("article_ingest", "trusted_domains",
        "github.io,openreview.net,.google,deepmind.com,arxiv.org,nips.cc,"
        "proceedings.mlr.press,openai.com,anthropic.com,pytorch.org,huggingface.co"
    ).split(","))
    _research_signals = tuple(cfg("article_ingest", "research_signals",
        "arxiv,paper,model,neural,robot,diffusion,transformer,learning,conference,proceedings,github.io"
    ).split(","))

    def _best_from(results):
        b, s = None, 0.0
        for r in results:
            sc = _title_word_overlap(title, r.get("title", ""))
            if sc > s:
                s, b = sc, r
        return b, s

    # Layer 0: arXiv title field (ti:) — precise, best for versioned model names
    results_ti = spw.search_by_title(search_query, limit=_arxiv_limit)
    if not results_ti and search_query != title:
        results_ti = spw.search_by_title(title, limit=_arxiv_limit)
    best, best_score = _best_from(results_ti)

    # Layer 1: arXiv all: field — broader, catches abstract mentions
    if best_score < _arxiv_thresh:
        results_all = spw.search_by_keyword(search_query, limit=_arxiv_limit)
        if not results_all and search_query != title:
            results_all = spw.search_by_keyword(title, limit=_arxiv_limit)
        b2, s2 = _best_from(results_all)
        if s2 > best_score:
            best, best_score = b2, s2
        # Top-1 arXiv rank trust: for CamelCase single-word model names
        if best_score < _arxiv_thresh and len(results_all) == 1:
            original_query_clean = re.sub(r"\s*\([^)]*\)", "", title).strip()
            if bool(re.search(r"[a-z][A-Z]", original_query_clean)):
                best = results_all[0]
                best_score = _top1_score

    if best_score < _top1_score or best is None:
        # ── Fallback 1: OpenReview (ICLR / NeurIPS / ICML papers) ──────────
        print(f"  [fallback] arXiv miss, trying OpenReview: {search_query[:50]}")
        or_results = spw.search_by_openreview(search_query, limit=_or_limit)
        or_best = None
        or_best_score = 0.0
        for r in or_results:
            score = _title_word_overlap(title, r.get("title", ""))
            if score > or_best_score:
                or_best_score = score
                or_best = r
        if or_best_score >= _or_thresh and or_best:
            best = or_best
            best_score = or_best_score
        else:
            # ── Fallback 2: web search for project page / blog post ────────
            print(f"  [fallback] trying web search: {title[:50]}")
            swr = _import_search_web_resources()
            web_results = swr.search_articles(f"{title} paper site", max_results=_web_limit)
            for r in web_results:
                r_url = r.get("url", "")
                r_title = r.get("title", "") or r.get("snippet", "")
                r_snippet = r.get("snippet", "").lower()
                title_score = _title_word_overlap(title, r_title)
                is_trusted = any(d in r_url for d in _trusted_domains)
                has_research_signal = any(
                    s in r_url.lower() or s in r_snippet or s in r_title.lower()
                    for s in _research_signals
                )
                # Require: meaningful title match AND (trusted domain OR research signal)
                if title_score < _web_title_min or not (is_trusted or has_research_signal):
                    continue
                combined = min(1.0, title_score + (_web_boost if is_trusted else 0.0))
                if combined > or_best_score:
                    or_best_score = combined
                    or_best = {
                        "title": r_title or title,
                        "arxiv_id": "",
                        "authors": paper_info.get("authors", []),
                        "year": paper_info.get("year"),
                        "abstract": r.get("snippet", ""),
                        "citations": 0,
                        "url": r_url,
                        "source": "web",
                    }
            if or_best_score >= _web_thresh and or_best:
                best = or_best
                best_score = or_best_score
            else:
                # ── Fallback 3: official blog via dokobot ──────────────────
                # Use Bing to find lab/company page, then dokobot read to extract
                # arxiv/pdf link. Handles product releases that predate arXiv papers.
                print(f"  [fallback] trying official blog via dokobot: {title[:50]}")
                blog_entry = _fetch_official_blog(title, paper_info, _trusted_domains)
                if blog_entry:
                    best = blog_entry
                    best_score = _web_thresh
                else:
                    return "not_found", None

    arxiv_id = best.get("arxiv_id", "")
    # Allow entries without arXiv ID if they have a URL (e.g., project pages / OpenReview)
    paper_url = best.get("url", "")
    if not arxiv_id and not paper_url:
        return "not_found", None

    # Check if already in KB (may have been added by another paper in same article)
    if arxiv_id:
        for p in papers:
            if p.get("arxiv_id") == arxiv_id:
                return "already_exists", p
    else:
        # For non-arXiv entries: match by title
        for p in papers:
            if _title_word_overlap(best.get("title", ""), p.get("title", "")) >= cfg("article_ingest", "kb_dedup_threshold", 0.8):
                return "already_exists", p

    ingest_label = f"arXiv:{arxiv_id}" if arxiv_id else f"web:{paper_url[:60]}"
    if dry_run:
        print(f"  [dry-run] would ingest {ingest_label} — {best.get('title','')[:60]}")
        return "ingested", best

    # Add to entries store
    default_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else paper_url
    new_paper = {
        "title": best.get("title", ""),
        "arxiv_id": arxiv_id,
        "authors": best.get("authors", []),
        "year": best.get("year"),
        "abstract": best.get("abstract", ""),
        "citations": best.get("citations", 0),
        "url": best.get("url", default_url),
        "project_urls": best.get("project_urls", []),
        "source_kind": "arxiv" if arxiv_id else "url",
        "entry_type": "paper",
    }
    template_spec = resolve_template("auto", item=new_paper, template_dir=template_dir)
    new_paper["template_id"] = template_spec.template_id
    papers.append(new_paper)

    save_entries(papers, papers_path)

    # Call enrich_wiki to create the source page for this entry
    sources_dir = wiki_dir / "wiki" / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    try:
        ew = _import_enrich_wiki()
        # Build a minimal source page
        slug = _paper_slug_from_paper(new_paper)
        page_path = sources_dir / f"{slug}.md"
        if not page_path.exists():
            _create_source_page_stub(page_path, new_paper, template_dir=template_dir)
        _integrate_new_entry_into_wiki(new_paper, wiki_dir, papers_path)
        # Enrich the page
        ew.enrich_source_page(
            page_path,
            new_paper,
            pdf_dir=None,
            all_papers=papers,
            force=False,
            llm_provider=provider,
            llm_model=model,
            figures_dir=None,
            media_dir=None,
            web_resources=False,
        )
    except Exception as e:
        print(f"  [warn] enrich_source_page failed for {arxiv_id}: {e}", file=sys.stderr)

    return "ingested", new_paper


def _create_source_page_stub(
    page_path: Path,
    paper: dict,
    template_dir: str | Path | None = None,
) -> None:
    """Create a source page stub for a newly ingested entry via the template system."""
    bpw = _import_build_paper_wiki()
    content = bpw.inject_toc(bpw.build_source_page(paper, template_dir=template_dir))
    page_path.write_text(content, encoding="utf-8")


def _integrate_new_entry_into_wiki(entry: dict, wiki_dir: Path, entries_path: Path) -> None:
    ip = _import_ingest_paper()
    entities_dir = wiki_dir / "wiki" / "entities"
    topics_dir = wiki_dir / "wiki" / "topics"
    index_path = wiki_dir / "index.md"
    category = entry.get("category") or cfg("wiki", "fallback_category", DEFAULT_FALLBACK_CATEGORY)
    ip.update_entity_pages(entry, entities_dir)
    ip.update_topic_page(entry, topics_dir, category)
    ip.update_index(entry, index_path, category, load_entries(entries_path))


# ---------------------------------------------------------------------------
# Step 4: Article slug generation
# ---------------------------------------------------------------------------

def _article_slug(title: str, platform: str) -> str:
    """Generate a slug for the article page."""
    slug = re.sub(r"[^\w\s一-鿿-]", "", title.lower())
    # Replace Chinese and spaces with hyphens
    slug = re.sub(r"[\s_一-鿿]+", lambda m: "-" + _pinyin_approx(m.group(0)) + "-", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    # Truncate and add platform prefix
    slug = slug[:55]
    slug = slug.strip("-")
    return f"{platform}-{slug}" if slug else platform


def _pinyin_approx(chinese: str) -> str:
    """
    Very rough approximation: just return empty string for Chinese characters
    so the slug relies on the non-Chinese parts.
    """
    # Filter out Chinese characters, keep ASCII
    ascii_part = re.sub(r"[一-鿿]", "", chinese).strip()
    return re.sub(r"\s+", "-", ascii_part).strip("-")


def _article_slug_v2(title: str, platform: str) -> str:
    """
    Generate an intuitive slug: platform-full_title.
    Keeps Chinese characters and most punctuation for readability in filenames.
    """
    # Remove only the most problematic Windows filename characters
    clean_title = re.sub(r'[<>:"/\\|?*]', '', title).strip()
    return f"{platform}-{clean_title}"


# ---------------------------------------------------------------------------
# Step 4: Write article page
# ---------------------------------------------------------------------------

def _table_esc(text: str) -> str:
    """Escape pipe characters for markdown table compatibility."""
    if not text:
        return ""
    return str(text).replace("|", "\\|")


def _format_article_page(
    info: dict,
    source_url: str,
    platform: str,
    existing_papers: list[dict],  # matched entries in KB (paper_info, kb_paper, slug)
    new_papers: list[dict],       # newly ingested entries (paper_info, kb_paper, slug)
    pending_papers: list[dict],   # not found (paper_info)
    conflict_flags: dict,         # slug → bool (has conflict)
    image_text: str = "",         # OCR-extracted text from inline images
    saved_image_paths: list = (), # PNG files saved to wiki/figures/articles/<slug>/
    article_page_path: "Path | None" = None,  # destination path (for relative img refs)
    template_id: str = "web_article",
    template_dir: str | Path | None = None,
) -> str:
    """Build the full article wiki page Markdown via the selected template."""
    title = info.get("title", "")
    author = info.get("author", "")
    account = info.get("account") or ""
    pub_date = info.get("date", "") or ""
    summary = info.get("summary", "")
    key_insights = info.get("key_insights", [])
    platform_display = PLATFORM_DISPLAY.get(platform, "文章")

    # Build display byline
    if account and author:
        byline = f"{account} · {author}"
    elif account:
        byline = account
    else:
        byline = author

    # Format key insights
    insights_md = "\n".join(
        f"{i+1}. **观点 {i+1}**：{insight}" for i, insight in enumerate(key_insights)
    ) or "（未提取到核心观点）"

    # --- Consolidated Mentioned Entries Table ---
    table_rows = []
    
    # 1. Existing entries
    for item in existing_papers:
        paper_info = item["paper_info"]
        slug = item["slug"]
        kb_paper = item["kb_paper"]
        p_title = kb_paper.get("title", paper_info.get("title", ""))[:50]
        stance = paper_info.get("mention_stance") or paper_info.get("article_stance", "中立")
        note = paper_info.get("key_claim", "").replace('\n', ' ')
        conflict_mark = " ⚡" if conflict_flags.get(slug) else ""
        entry_type = _mentioned_entry_type_label(kb_paper or paper_info)
        table_rows.append(f"| {_table_esc(p_title)} | [[{slug}]]{conflict_mark} | {_table_esc(entry_type)} | {_table_esc(stance)} | {_table_esc(note)} |")

    # 2. New entries
    for item in new_papers:
        paper_info = item["paper_info"]
        kb_paper = item["kb_paper"]
        slug = item["slug"]
        p_title = kb_paper.get("title", paper_info.get("title", ""))[:50]
        status = item.get("status", "✅ 已 ingest")
        stance = paper_info.get("mention_stance") or paper_info.get("article_stance", "未知")
        note = f"新摄入: {status}"
        # Ensure it's a wikilink
        wiki_link = f"[[{slug}]]" if slug else "—"
        entry_type = _mentioned_entry_type_label(kb_paper or paper_info)
        table_rows.append(f"| {_table_esc(p_title)} | {wiki_link} | {_table_esc(entry_type)} | {_table_esc(stance)} | {_table_esc(note)} |")

    # 3. Pending entries
    for item in pending_papers:
        paper_info = item["paper_info"]
        p_title = paper_info.get("title", "")[:50]
        description = paper_info.get("description", "")[:80].replace('\n', ' ')
        stance = paper_info.get("mention_stance") or paper_info.get("article_stance", "未知")
        entry_type = _mentioned_entry_type_label(paper_info)
        table_rows.append(f"| {_table_esc(p_title)} | （未找到匹配） | {_table_esc(entry_type)} | {_table_esc(stance)} | {_table_esc(description)} |")

    papers_table = (
        "| 条目 | Wiki 页面 | 类型 | 文章态度 | 备注 |\n"
        "|------|-----------|------|----------|------|\n"
    ) + "\n".join(table_rows) if table_rows else "| （未提及） | | | | |\n"


    linked_pages = [
        f"[[{item['slug']}]]"
        for item in existing_papers + new_papers
        if item.get("slug")
    ]
    related_pages = "\n".join(f"- {page}" for page in ["[[index]]", *linked_pages[:8]])
    relations_body = markdown_bullets(
        linked_pages[:8],
        fallback="（待补充）",
    )
    facts_body = "\n".join([
        f"- **平台**：{platform_display}",
        f"- **作者/账号**：{byline or '（未知）'}",
        f"- **发布日期**：{pub_date or '（未知）'}",
        f"- **原始链接**：[访问原文]({source_url})" if source_url else "- **原始链接**：（暂无）",
        f"- **提到的已收录条目**：{len(existing_papers) + len(new_papers)}",
        f"- **待确认条目**：{len(pending_papers)}",
    ])

    # Image section
    image_section = ""
    if saved_image_paths:
        image_section = "\n## 演示与图片\n\n"
        page_dir = article_page_path.parent if article_page_path else None
        for i, p in enumerate(saved_image_paths):
            if page_dir is not None:
                import os as _os
                rel = _os.path.relpath(str(p.resolve()), str(page_dir.resolve()))
                # Force forward slashes
                rel = rel.replace("\\", "/")
            else:
                rel = str(p).replace("\\", "/")
            
            import urllib.parse
            rel_encoded = urllib.parse.quote(rel)
            image_section += f"![图 {i+1}]({rel_encoded})\n> 图 {i+1}\n\n"
            
        if image_text and image_text.strip():
            img_body = re.sub(
                r"^---\s*\n\s*## 图片内容（自动提取）\s*\n", "",
                image_text.strip(), flags=re.MULTILINE
            ).strip()
            if img_body:
                image_section += f"\n### 图片内容\n{img_body}\n"
    if image_section and not image_section.endswith("\n\n"):
        image_section = image_section.rstrip() + "\n\n"

    context = {
        "template_id_yaml": yaml_string(template_id),
        "entry_type_yaml": yaml_string("article"),
        "source_kind_yaml": yaml_string("url"),
        "title": title,
        "title_yaml": yaml_string(title),
        "author_yaml": yaml_string(author),
        "account_yaml": yaml_string(account),
        "date_value": pub_date or "null",
        "source_url": source_url,
        "source_url_yaml": yaml_string(source_url),
        "platform_yaml": yaml_string(platform),
        "platform_tag": platform,
        "platform_display": platform_display,
        "byline": byline or "（未知）",
        "created": TODAY,
        "updated": TODAY,
        "summary_body": summary or "（待补充）",
        "highlights_body": insights_md,
        "mentions_body": papers_table,
        "facts_body": facts_body,
        "relations_body": relations_body,
        "notes_body": image_text.strip() or "（待补充）",
        "actions_body": "（待补充）",
        "image_section": image_section,
        "related_pages": related_pages,
    }
    return render_template(template_id, context, template_dir=template_dir)

# ---------------------------------------------------------------------------
# Index articles section updater
# ---------------------------------------------------------------------------

def _update_index_articles(wiki_dir: Path, title: str, slug: str,
                            url: str, date: str, info: dict) -> None:
    """Surgically add/update an article entry in index.md under '## 文章' section."""
    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        return

    content = index_path.read_text(encoding="utf-8")
    author = info.get("author", "")
    account = info.get("account", "")
    platform_map = {"weixin": "微信", "zhihu": "知乎", "xiaohongshu": "小红书"}
    platform = info.get("platform", "")
    platform_label = platform_map.get(platform, platform)
    byline = " · ".join(filter(None, [platform_label, account, author, date]))

    new_line = f"- [[{slug}]] — {title}（{byline}）"

    # Check if already present
    if slug in content:
        return

    articles_header = "\n## 文章\n"
    if articles_header in content:
        # Append after the header line
        insert_after = content.index(articles_header) + len(articles_header)
        # Skip blank line after header if present
        if content[insert_after:insert_after+1] == "\n":
            insert_after += 1
        content = content[:insert_after] + new_line + "\n" + content[insert_after:]
    else:
        # Append a new section before the last line
        content = content.rstrip("\n") + "\n\n## 文章\n\n" + new_line + "\n"

    index_path.write_text(content, encoding="utf-8")
    print(f"[index] Added article link: {slug}")


def _entry_category(entry: dict) -> str:
    return entry.get("category") or cfg("wiki", "fallback_category", DEFAULT_FALLBACK_CATEGORY)


def _guess_pdf_dir(wiki_dir: Path) -> Path | None:
    for candidate in (wiki_dir.parent / "pdfs", wiki_dir / "pdfs"):
        if candidate.exists():
            return candidate
    return None


def _infer_wiki_topic_name(wiki_dir: Path) -> str:
    index_path = wiki_dir / "index.md"
    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
        match = re.search(r"^#\s+(.+?)\s+知识库索引\s*$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return wiki_dir.name


def _post_ingest_enrich(
    wiki_dir: Path,
    entries: list[dict],
    touched_entries: list[tuple[Path, dict]],
    provider: str = "auto",
    model: str = "",
    template_dir: str | Path | None = None,
) -> dict[str, int | bool]:
    """Enrich touched source pages and refresh related topics/survey after article ingest."""
    ew = _import_enrich_wiki()
    pdf_dir = _guess_pdf_dir(wiki_dir)
    topics_dir = wiki_dir / "wiki" / "topics"

    unique_entries: dict[Path, dict] = {}
    for page_path, entry in touched_entries:
        if page_path.exists():
            unique_entries[page_path] = entry

    stats: dict[str, int | bool] = {
        "sources": 0,
        "topics": 0,
        "survey": False,
    }
    if not unique_entries:
        return stats

    for page_path, entry in sorted(unique_entries.items(), key=lambda item: item[0].name):
        try:
            modified = ew.enrich_source_page(
                page_path,
                entry,
                pdf_dir,
                entries,
                False,
                provider,
                model,
                figures_dir=None,
                media_dir=None,
                web_resources=False,
                direct_input=None,
                template_dir=template_dir,
            )
            if modified:
                stats["sources"] += 1
                print(f"  [ok] source enrich: {page_path.name}")
        except Exception as e:
            print(f"  [warn] post-ingest source enrich failed for {page_path.name}: {e}", file=sys.stderr)

    touched_categories = sorted({_entry_category(entry) for entry in unique_entries.values()})
    for category in touched_categories:
        topic_path = topics_dir / f"{category}.md"
        if not topic_path.exists():
            continue
        entries_in_topic = [entry for entry in entries if _entry_category(entry) == category]
        try:
            modified = ew.enrich_topic_page(
                topic_path,
                entries_in_topic,
                False,
                provider,
                model,
                do_compare=True,
            )
            if modified:
                stats["topics"] += 1
                print(f"  [ok] topic enrich: {topic_path.name}")
        except Exception as e:
            print(f"  [warn] post-ingest topic enrich failed for {topic_path.name}: {e}", file=sys.stderr)

    if topics_dir.exists():
        try:
            ew.generate_survey(
                wiki_dir,
                entries,
                _infer_wiki_topic_name(wiki_dir),
                provider,
                model,
            )
            stats["survey"] = True
        except Exception as e:
            print(f"  [warn] post-ingest survey generation failed: {e}", file=sys.stderr)

    return stats


# ---------------------------------------------------------------------------
# Main ingestion pipeline
# ---------------------------------------------------------------------------

def ingest_article(
    url: str,
    wiki_dir: Path,
    papers_path: Path,
    provider: str = "auto",
    model: str = "",
    force: bool = False,
    no_fetch: bool = False,
    input_file: Path | None = None,
    dry_run: bool = False,
    template: str = "auto",
    template_dir: str | Path | None = None,
    post_ingest_enrich: bool = True,
    direct_input: str | None = None,
    skip_images: bool = False,
) -> None:
    """Full ingestion pipeline for a web article."""

    import time as _time
    _t0 = _time.time()

    def _log(msg):
        elapsed = _time.time() - _t0
        print(f"[{elapsed:6.1f}s] {msg}", flush=True)

    # --- Step 1: Fetch article ---
    if no_fetch:
        if input_file is None:
            _log("[error] --no-fetch requires --input <file.md>")
            sys.exit(1)
        article_md = input_file.read_text(encoding="utf-8")
        _log(f"[fetch] Using local file: {input_file}")
    else:
        _log(f"[fetch] Fetching: {url}")
        article_md = fetch_article_markdown(url)
        _log(f"[fetch] Got {len(article_md)} chars of Markdown")

    # Extract title/URL from Markdown
    md_title, md_url = parse_markdown_meta(article_md, fallback_url=url)

    # Detect platform
    platform = _detect_platform(md_url or url)

    _log(f"[info] Platform: {platform}")
    _log(f"[info] Title (from MD): {md_title[:80]}")

    # --- Step 1b: Extract image content via vision ---
    # Compute a provisional article slug from the md_title for the figures save_dir.
    # The final slug (after LLM title extraction) may differ slightly but the dir name
    # is only used for storage, not for wikilink resolution.
    _provisional_slug = _article_slug_v2(md_title or "article", platform)
    _figures_base = wiki_dir / "figures" / "articles" / _provisional_slug
    _save_dir = None if dry_run else _figures_base

    _log("[info] Extracting images + OCR...")
    image_text, saved_image_paths = "", []
    if not skip_images:
        image_text, saved_image_paths = extract_images_from_article(
            article_md, platform=platform, provider=provider, model=model,
            save_dir=_save_dir,
        )
    _log(f"[info] Images done, OCR text: {len(image_text)} chars, saved: {len(saved_image_paths)} images")
    if image_text:
        article_md = article_md + image_text

    # --- Step 2: LLM extraction ---
    _log("[llm] Extracting structured info from article...")
    info = _extract_article_info(article_md, provider=provider, model=model, direct_input=direct_input)
    _log(f"[llm] Extraction done, title: {info.get('title', '?')[:60]}")

    # Prefer LLM-extracted title/platform if available, else use MD-parsed
    if not info.get("title") and md_title:
        info["title"] = md_title
    if not info.get("platform") or info["platform"] == "article":
        info["platform"] = platform
    else:
        platform = info["platform"]

    title = info.get("title", "") or md_title or "未知标题"
    # CLI --url takes precedence; fall back to URL parsed from fetched Markdown
    source_url = url or md_url
    pub_date = info.get("date", "") or TODAY
    selected_template = resolve_template(
        template,
        item={
            "entry_type": "article",
            "source_kind": "url" if source_url else "file",
            "platform": platform,
            "url": source_url,
            "title": title,
            "content_excerpt": article_md[:500],
        },
        template_dir=template_dir,
    )

    print(f"[info] Extracted title: {title[:80]}")
    print(f"[info] Template: {selected_template.template_id}")
    print(f"[info] Entries mentioned: {len(info.get('entries_mentioned', []))}")
    for pm in info.get("entries_mentioned", []):
        type_label = _mentioned_entry_type_label(pm)
        ref_label = pm.get("arxiv_id") or pm.get("url") or "—"
        print(f"  - {pm.get('title','')[:60]} [{type_label} | {ref_label[:60]}]")

    # Determine output directories (support both legacy nested 'wiki/' and flat root)
    paths = get_wiki_paths(wiki_dir)
    sources_dir = paths["sources"]
    entities_dir = paths["entities"]
    topics_dir = paths["topics"]
    articles_dir = paths["articles"]

    # Generate article slug
    article_slug = _article_slug_v2(title, platform)
    articles_dir.mkdir(parents=True, exist_ok=True)
    article_page_path = articles_dir / f"{article_slug}.md"

    if article_page_path.exists() and not force:
        print(f"[warn] Article page already exists: {article_page_path.name}")
        print("  Use --force to overwrite.")
        sys.exit(0)

    # --- Step 3: Load KB entries ---
    all_papers = load_entries(papers_path)

    papers_mentioned = info.get("entries_mentioned", [])

    existing_papers = []   # [{paper_info, kb_paper, slug}]
    new_papers = []        # [{paper_info, kb_paper, slug, status}]
    pending_papers = []    # [{paper_info}]
    conflict_flags = {}    # slug → bool

    stats_enriched = 0
    stats_ingested = 0
    stats_pending = 0
    touched_entries: list[tuple[Path, dict]] = []

    for pm in papers_mentioned:
        pm_title = pm.get("title", "")
        entry_type = _mentioned_entry_type_label(pm)
        print(f"\n[entry] Processing: {pm_title[:60]} [{entry_type}]")

        kb_paper = _find_entry_in_kb(pm, all_papers)

        if kb_paper is not None:
            # Already in KB
            slug = _entry_slug(kb_paper)
            source_page_path = sources_dir / f"{slug}.md"

            # Detect conflict
            key_claim = pm.get("key_claim", "")
            is_conflict = False
            if key_claim and not dry_run:
                is_conflict, _ = _detect_conflict(
                    key_claim, source_page_path, provider=provider, model=model
                )
            elif (pm.get("mention_stance") or pm.get("article_stance")) == "批评":
                is_conflict = True

            conflict_flags[slug] = is_conflict

            existing_papers.append({
                "paper_info": pm,
                "kb_paper": kb_paper,
                "slug": slug,
            })

            print(f"  [match] Found in KB: {slug[:50]}")

            # Enrich existing source page
            ok = _enrich_existing_entry(
                source_page_path,
                pm,
                article_title=title,
                article_url=source_url,
                article_date=pub_date,
                article_platform=PLATFORM_DISPLAY.get(platform, platform),
                provider=provider,
                model=model,
                dry_run=dry_run,
            )
            if ok:
                stats_enriched += 1
            touched_entries.append((source_page_path, kb_paper))

        else:
            is_research = _is_research_reference(pm)
            if is_research:
                print(f"  [search] Not in KB, searching research sources: {pm_title[:50]}")
                status, found_paper = _search_and_ingest_paper(
                    pm,
                    all_papers,
                    papers_path,
                    wiki_dir,
                    provider=provider,
                    model=model,
                    dry_run=dry_run,
                    template_dir=template_dir,
                )
            else:
                print(f"  [ingest] Not in KB, creating direct entry: {pm_title[:50]}")
                status, found_paper = _ingest_direct_entry(
                    pm,
                    all_papers,
                    papers_path,
                    wiki_dir,
                    dry_run=dry_run,
                    template_dir=template_dir,
                )

            if status in ("ingested", "already_exists"):
                slug = _entry_slug(found_paper) if found_paper else ""
                source_page_path = sources_dir / f"{slug}.md"
                is_conflict = (pm.get("mention_stance") or pm.get("article_stance")) == "批评"
                conflict_flags[slug] = is_conflict

                display_status = "✅ 已 ingest" if status == "ingested" else "✅ 已存在"
                new_papers.append({
                    "paper_info": pm,
                    "kb_paper": found_paper,
                    "slug": slug,
                    "status": display_status,
                })

                if status == "ingested":
                    stats_ingested += 1
                    fp_id = found_paper.get("arxiv_id") or found_paper.get("url", "")[:60]
                    print(f"  [ingest] Added to KB: {fp_id}")
                else:
                    print(f"  [skip] Already in KB (found after search): {slug[:50]}")

                # Also enrich the source page with article evaluation
                if not dry_run:
                    _enrich_existing_entry(
                        source_page_path,
                        pm,
                        article_title=title,
                        article_url=source_url,
                        article_date=pub_date,
                        article_platform=PLATFORM_DISPLAY.get(platform, platform),
                        provider=provider,
                        model=model,
                        dry_run=dry_run,
                    )
                if found_paper is not None:
                    touched_entries.append((source_page_path, found_paper))

            else:
                # Not found anywhere
                pending_papers.append({"paper_info": pm})
                stats_pending += 1
                print(f"  [pending] Not found or insufficient metadata: {pm_title[:50]}")

    # --- Step 4: Write article page ---
    article_content = _format_article_page(
        info=info,
        source_url=source_url,
        platform=platform,
        existing_papers=existing_papers,
        new_papers=new_papers,
        pending_papers=pending_papers,
        conflict_flags=conflict_flags,
        image_text=image_text,
        saved_image_paths=saved_image_paths,
        article_page_path=article_page_path,
        template_id=selected_template.template_id,
        template_dir=template_dir,
    )

    if dry_run:
        print(f"\n[dry-run] Would write article page: {article_page_path}")
        print("=" * 60)
        print(article_content[:2000])
        if len(article_content) > 2000:
            print(f"[...{len(article_content) - 2000} more chars...]")
        print("=" * 60)
    else:
        article_page_path.write_text(article_content, encoding="utf-8")
        print(f"\n[write] Article page: {article_page_path}")
        _update_index_articles(wiki_dir, title, article_slug, source_url, pub_date, info)

        post_stats = {"sources": 0, "topics": 0, "survey": False}
        if post_ingest_enrich:
            print("\n[post-enrich] Enriching touched source pages and refreshing topics...")
            post_stats = _post_ingest_enrich(
                wiki_dir,
                all_papers,
                touched_entries,
                provider=provider,
                model=model,
                template_dir=template_dir,
            )
            print(
                f"[post-enrich] 完成：source {post_stats['sources']} 个"
                f" | topic {post_stats['topics']} 个"
                f" | survey {'已更新' if post_stats['survey'] else '未更新'}"
            )

    # --- Summary ---
    print(
        f"\n{'[dry-run] ' if dry_run else ''}统计：文章页{'已写入' if not dry_run else '（模拟）'} {article_page_path.name}"
        f" | 已收录条目更新 {stats_enriched} 个"
        f" | 新增条目 {stats_ingested} 个"
        f" | 待确认 {stats_pending} 个"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ingest web articles (WeChat / Zhihu / Xiaohongshu) into the wiki.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch and ingest a WeChat article
  python ingest_article.py --url "https://mp.weixin.qq.com/s/..." \\
      --wiki-dir /path/to/wiki --entries /path/to/entries.json

  # Use already-fetched markdown (skip dokobot)
  python ingest_article.py --no-fetch --input article.md \\
      --wiki-dir /path/to/wiki --entries /path/to/entries.json

  # Dry run (analyze only, no file writes)
  python ingest_article.py --url "..." --wiki-dir ... --entries ... --dry-run
        """,
    )
    parser.add_argument("--url", default="", help="URL of the article to ingest")
    parser.add_argument("--wiki-dir", required=True, help="Path to wiki root directory")
    add_entries_argument(parser, required=True)
    parser.add_argument(
        "--llm-provider",
        default=cfg("llm", "provider", "auto"),
        choices=["auto", "direct-inference", "anthropic", "openai", "ollama"],
        help="LLM provider (default: scripts/config.toml llm.provider, fallback auto)",
    )
    parser.add_argument(
        "--llm-model",
        default=cfg("llm", "model", ""),
        help="LLM model name",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing article page",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip dokobot fetch, read from --input file instead",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Already-fetched Markdown file (used with --no-fetch)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print analysis results without writing any files",
    )
    parser.add_argument(
        "--template",
        default="auto",
        help="Page template to use for the article page (default: auto; examples: web_article, generic)",
    )
    parser.add_argument(
        "--template-dir",
        default=None,
        help="Directory containing editable page templates (default: repo templates/)",
    )
    parser.add_argument(
        "--direct-input",
        default=None,
        help="File path containing raw LLM output to bypass the direct-inference pause.",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Skip image download and OCR (faster, useful when images are slow)",
    )
    parser.add_argument(
        "--post-ingest-enrich",
        dest="post_ingest_enrich",
        action="store_true",
        help="After article ingest, enrich touched source pages and refresh related topic/survey pages",
    )
    parser.add_argument(
        "--no-post-ingest-enrich",
        dest="post_ingest_enrich",
        action="store_false",
        help="Skip post-ingest enrich after writing the article page",
    )
    parser.set_defaults(post_ingest_enrich=True)
    git_group = parser.add_mutually_exclusive_group()
    git_group.add_argument(
        "--git-commit",
        dest="git_commit",
        action="store_true",
        help="Create a git commit after writing files",
    )
    git_group.add_argument(
        "--no-git-commit",
        dest="git_commit",
        action="store_false",
        help="Disable git commit for this run",
    )
    parser.set_defaults(git_commit=None)

    args = parser.parse_args()

    if not args.no_fetch and not args.url:
        parser.error("--url is required unless --no-fetch is specified")
    text_provider_desc = describe_provider_selection(
        args.llm_provider,
        allowed={"direct-inference", "anthropic", "openai", "ollama", "claude-cli", "codex-cli", "gemini-cli"},
    )
    vision_provider_desc = describe_provider_selection(
        args.llm_provider,
        vision=True,
        allowed={"openai", "anthropic"},
    )
    print(f"[llm] text provider: {text_provider_desc}", file=sys.stderr, flush=True)
    if vision_provider_desc != text_provider_desc:
        print(f"[llm] vision provider: {vision_provider_desc}", file=sys.stderr, flush=True)

    wiki_dir = Path(args.wiki_dir)
    # Auto-organize images
    organize_pasted_images(wiki_dir, fix=True)


    if not wiki_dir.exists():
        print(f"[error] wiki-dir does not exist: {wiki_dir}", file=sys.stderr)
        sys.exit(1)

    papers_path = Path(args.entries_path)
    if not papers_path.exists():
        print(f"[error] entries file not found: {papers_path}", file=sys.stderr)
        sys.exit(1)

    input_file = Path(args.input) if args.input else None
    if args.no_fetch and input_file and not input_file.exists():
        print(f"[error] input file not found: {input_file}", file=sys.stderr)
        sys.exit(1)

    # Ingest the article
    # We don't have a direct list of touched files from ingest_article yet, 
    # but we can track them. For now, we'll use a simplified version.
    ingest_article(
        url=args.url,
        wiki_dir=wiki_dir,
        papers_path=papers_path,
        provider=args.llm_provider,
        model=args.llm_model,
        force=args.force,
        no_fetch=args.no_fetch,
        input_file=input_file,
        dry_run=args.dry_run,
        template=args.template,
        template_dir=args.template_dir,
        post_ingest_enrich=args.post_ingest_enrich,
        direct_input=args.direct_input,
        skip_images=args.skip_images,
    )

    # Optional git commit
    should_git_commit = args.git_commit if args.git_commit is not None else cfg("git", "auto_commit", False)
    if should_git_commit and not args.dry_run:
        from git_utils import git_commit_paths
        # This is a bit coarse, but covers the major touched areas
        # A more precise implementation would collect paths from ingest_article
        msg = f"auto-wiki-archive: [Ingest] {args.url or input_file.name}"
        committed, detail = git_commit_paths(wiki_dir, [wiki_dir, papers_path], msg)
        if committed:
            print(f"[git] committed: {detail}")


if __name__ == "__main__":
    main()
