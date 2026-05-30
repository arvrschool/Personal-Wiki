#!/usr/bin/env python3
"""
Fetch paper figures from ar5iv (HTML) or PDF (raster image extraction).
Falls back to PDF extraction if ar5iv is unavailable.
"""

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

AR5IV_BASE = "https://ar5iv.labs.arxiv.org"
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_MIN_PDF_IMAGE_AREA = 100 * 100  # pixels
_DEFAULT_MIN_SIZE_KB = 20
_BOILERPLATE_SIZE = (800, 600)  # arXiv badge / icon uniform size to filter
_BOILERPLATE_TOLERANCE = 2
_TITLE_PAGE_COUNT = 1  # skip title/metadata page for image extraction


def _is_boilerplate_image(w: int, h: int) -> bool:
    """Return True if image dimensions match arXiv badge / icon boilerplate (800x600 uniform icons)."""
    bw, bh = _BOILERPLATE_SIZE
    return (abs(w - bw) <= _BOILERPLATE_TOLERANCE and abs(h - bh) <= _BOILERPLATE_TOLERANCE) \
        or (abs(w - bh) <= _BOILERPLATE_TOLERANCE and abs(h - bw) <= _BOILERPLATE_TOLERANCE)


# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------

def fetch_figures(
    arxiv_id: str,
    pdf_path: Path | str | None,
    out_dir: Path,
    max_figures: int = 15,
    min_size_kb: int = _DEFAULT_MIN_SIZE_KB,
) -> list[dict]:
    """
    Fetch figures for a paper.
    1. Try ar5iv HTML extraction (best: has captions, high quality).
    2. If fails, try PDF extraction (fallback: raster images, no captions).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id.strip())

    print(f"[figures] Fetching for {arxiv_id} ...")

    # 1. ar5iv (HTML)
    figures = _fetch_ar5iv_figures(arxiv_id, out_dir, max_figures, min_size_kb)
    if figures:
        print(f"  [ok] Found {len(figures)} figures via ar5iv HTML")
        _save_figures_json(out_dir, figures)
        return figures

    # 2. PDF fallback
    if pdf_path:
        pdf_path = Path(pdf_path)
        if pdf_path.exists():
            print(f"  [info] ar5iv failed. Attempting PDF extraction from {pdf_path}...")
            figures = _extract_pdf_figures(pdf_path, out_dir, max_figures, min_size_kb)
            if figures:
                print(f"  [ok] Extracted {len(figures)} figures from PDF")
                _save_figures_json(out_dir, figures)
                return figures

    print("  [warn] No figures found.")
    return []


def _fetch_ar5iv_figures(
    arxiv_id: str,
    out_dir: Path,
    max_figures: int,
    min_size_kb: int,
) -> list[dict]:
    """Fetch figures from ar5iv HTML rendering."""
    url = f"{AR5IV_BASE}/html/{arxiv_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as response:
            html = response.read().decode("utf-8")
    except Exception as e:
        # print(f"  [debug] ar5iv fetch failed: {e}", file=sys.stderr)
        return []

    # Extremely crude regex-based extraction of <figure> tags and <img> srcs
    # Real implementations should use BeautifulSoup.
    figure_blocks = re.findall(r'<figure.*?</figure>', html, re.DOTALL)
    figures = []
    index = 1

    for block in figure_blocks:
        if index > max_figures:
            break

        # Find image src
        img_m = re.search(r'<img\s+src="([^"]+)"', block)
        if not img_m:
            continue
        img_src = img_m.group(1)
        if not img_src.startswith("http"):
            if img_src.startswith("/"):
                img_url = f"{AR5IV_BASE}{img_src}"
            else:
                img_url = f"{AR5IV_BASE}/html/{arxiv_id}/{img_src}"
        else:
            img_url = img_src

        # Find caption
        cap_m = re.search(r'<figcaption>(.*?)</figcaption>', block, re.DOTALL)
        caption = ""
        if cap_m:
            caption = re.sub(r'<.*?>', '', cap_m.group(1)).strip()
            # Clean up LaTeX or spacing in captions
            caption = re.sub(r'\s+', ' ', caption)

        # Download
        try:
            ext = Path(img_url).suffix or ".png"
            if "?" in ext: ext = ext.split("?")[0]
            filename = f"fig-{index}{ext}"
            dest = out_dir / filename
            
            req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as img_resp:
                img_data = img_resp.read()
                
            if len(img_data) < min_size_kb * 1024:
                continue
                
            dest.write_bytes(img_data)
            w, h = _image_dimensions(dest)

            figures.append({
                "index": index,
                "filename": filename,
                "caption": caption,
                "source_url": img_url,
                "fig_id": f"fig-{index}",
                "width": w,
                "height": h,
            })
            index += 1
        except Exception as e:
            print(f"  [warn] Failed to download {img_url}: {e}", file=sys.stderr)

    return figures


def _extract_pdf_figures(
    pdf_path: Path,
    out_dir: Path,
    max_figures: int,
    min_size_kb: int,
) -> list[dict]:
    """
    Extract embedded raster images from PDF using pdfimages (poppler) or PyMuPDF.
    Filters by minimum area and file size; returns figures WITHOUT captions.
    """
    figures = []
    
    if _which("pdfimages"):
        prefix = str(out_dir / "raw")
        try:
            result = subprocess.run(
                ["pdfimages", "-png", "-f", "2", str(pdf_path), prefix],
                capture_output=True, timeout=60,
            )
            if result.returncode == 0:
                raw_files = sorted(out_dir.glob("raw-*.png"))
                index = 1
                for raw in raw_files:
                    if index > max_figures:
                        break
                    size_kb = raw.stat().st_size // 1024
                    if size_kb < min_size_kb:
                        raw.unlink(missing_ok=True)
                        continue
                    w, h = _image_dimensions(raw)
                    if PILImage is not None and (w * h < _MIN_PDF_IMAGE_AREA or _is_boilerplate_image(w, h)):
                        raw.unlink(missing_ok=True)
                        continue
                    filename = f"fig-{index}.png"
                    raw.rename(out_dir / filename)
                    figures.append({"index": index, "filename": filename, "caption": "", "width": w, "height": h})
                    index += 1
                return figures
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Fallback to PyMuPDF
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        index = 1
        for i in range(len(doc)):
            if index > max_figures:
                break
            for img in doc.get_page_images(i):
                if index > max_figures:
                    break
                xref = img[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]

                size_kb = len(image_bytes) // 1024
                if size_kb < min_size_kb:
                    continue

                ext = base_image["ext"]
                temp_name = f"temp-{index}.{ext}"
                temp_path = out_dir / temp_name
                temp_path.write_bytes(image_bytes)

                w, h = _image_dimensions(temp_path)
                if PILImage is not None and (w * h < _MIN_PDF_IMAGE_AREA or _is_boilerplate_image(w, h)):
                    temp_path.unlink(missing_ok=True)
                    continue
                
                filename = f"fig-{index}.{ext}"
                temp_path.rename(out_dir / filename)
                figures.append({"index": index, "filename": filename, "caption": f"Extracted from PDF page {i+1}", "width": w, "height": h})
                index += 1
        doc.close()
    except ImportError:
        print("  [WARN] pdfimages not found and PyMuPDF (fitz) not installed. Skipping PDF extraction.", file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] PDF extraction failed: {e}", file=sys.stderr)

    return figures


def _save_figures_json(out_dir: Path, figures: list[dict]):
    with open(out_dir / "figures.json", "w", encoding="utf-8") as f:
        json.dump(figures, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def figures_to_markdown(figures: list[dict], figures_dir: Path, page_dir: Path = None, wiki_root: Path = None) -> str:
    """
    Generate Markdown content for the 论文图表 section.
    Attempts to use relative paths.
    """
    if not figures:
        return ""

    lines = ["## 论文图表\n"]
    for fig in figures:
        img_path = (figures_dir / fig["filename"]).resolve()
        if page_dir:
            img_ref = os.path.relpath(img_path, Path(page_dir).resolve())
        elif wiki_root:
            try:
                img_ref = str(img_path.relative_to(Path(wiki_root).resolve()))
            except ValueError:
                img_ref = str(img_path)
        else:
            img_ref = str(img_path)

        # Force forward slashes for Markdown compatibility on Windows
        img_ref = img_ref.replace("\\", "/")

        caption = fig.get("caption", "").strip()
        index = fig.get("index", "?")
        label = f"Figure {index}" + (f": {caption[:120]}" if caption else "")

        lines.append(f"![{label}]({img_ref})\n")
        if caption:
            lines.append(f"> {caption}\n")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def _image_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) using PIL."""
    if PILImage is None:
        return 0, 0
    try:
        with PILImage.open(path) as img:
            return img.size  # (width, height)
    except Exception:
        return 0, 0


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch/Extract paper figures")
    parser.add_argument("--arxiv-id", required=True, help="ArXiv ID")
    parser.add_argument("--pdf-path", help="Local PDF path for fallback extraction")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument("--max-figures", type=int, default=15)
    parser.add_argument("--min-size-kb", type=int, default=_DEFAULT_MIN_SIZE_KB)
    parser.add_argument("--markdown", action="store_true", help="Print Markdown output")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    figures = fetch_figures(args.arxiv_id, args.pdf_path, out_dir, args.max_figures, args.min_size_kb)

    if args.markdown and figures:
        # For CLI output, we use wiki-root-relative or absolute paths
        print("\n--- MARKDOWN START ---")
        print(figures_to_markdown(figures, out_dir))
        print("--- MARKDOWN END ---\n")

    print(f"Downloaded {len(figures)} figures -> {out_dir}/figures.json")


if __name__ == "__main__":
    main()
