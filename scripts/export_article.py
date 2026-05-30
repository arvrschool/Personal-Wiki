#!/usr/bin/env python3
import sys
import os
import subprocess
import re
from pathlib import Path

def run_cmd(cmd, description):
    """Run a subprocess command. Accepts either a list or a shell string.

    Passing a list is preferred (cross-platform, no quoting issues).  A plain
    string is still accepted for legacy callers but is executed via the system
    shell — avoid passing strings with embedded spaces in paths.
    """
    print(f"--> {description}...")
    try:
        use_shell = isinstance(cmd, str)
        result = subprocess.run(
            cmd, shell=use_shell, check=True,
            capture_output=True, text=True, encoding="utf-8",
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error during {description}:")
        print(e.stderr)
        sys.exit(1)

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/export_article.py <source_md_path>")
        sys.exit(1)

    source_md = Path(sys.argv[1]).resolve()
    if not source_md.exists():
        print(f"Error: Source file {source_md} does not exist.")
        sys.exit(1)

    # 1. Resolve Paths Dynamically
    # Expecting: .../wiki/wiki/sources/filename.md
    # Export to: .../wiki/exports/filename/
    slug = source_md.stem
    wiki_root = source_md.parent.parent.parent
    export_base = wiki_root / "exports" / slug
    
    wechat_dir = export_base / "wechat"
    xhs_dir = export_base / "xhs-images"
    
    print(f"=== Exporting Article: {slug} ===")
    print(f"Wiki Root: {wiki_root}")
    print(f"Export Dir: {export_base}")
    
    export_base.mkdir(parents=True, exist_ok=True)
    wechat_dir.mkdir(parents=True, exist_ok=True)
    xhs_dir.mkdir(parents=True, exist_ok=True)

    # Determine script directory
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    # 2. Phase 1: Internal Interactive HTML
    internal_html = export_base / f"{slug}.html"
    wiki_to_html_script = script_dir / "wiki_to_html.py"
    run_cmd(
        [sys.executable, str(wiki_to_html_script), str(source_md), "--output", str(internal_html)],
        "Generating Internal Interactive HTML"
    )

    # 3. Phase 2: WeChat Article
    cleaned_md = wechat_dir / "cleaned_source.md"
    wechat_html = wechat_dir / "article.html"
    prepare_public_md_script = script_dir / "prepare_public_md.py"
    
    # Run sanitization to MD
    run_cmd(
        [sys.executable, str(prepare_public_md_script), str(source_md), str(cleaned_md)],
        "Sanitizing Markdown for Public Distribution"
    )
    
    # Convert Sanitized MD to WeChat HTML using vendor tool
    # Note: Using npx -y bun for consistency with earlier successful calls
    vendor_script = project_root / "vendor" / "baoyu-markdown-to-html" / "scripts" / "main.ts"
    run_cmd(
        f'npx -y bun "{vendor_script}" "{cleaned_md}" --theme default --title "{slug}"',
        "Converting Cleaned MD to WeChat HTML"
    )
    
    # The vendor tool saves to cleaned_source.html, we need to move it and then fix it
    temp_html = wechat_dir / "cleaned_source.html"
    if temp_html.exists():
        if wechat_html.exists():
            wechat_html.unlink()
        temp_html.rename(wechat_html)
    
    # Fix image placeholders in WeChat HTML
    run_cmd(
        [sys.executable, str(prepare_public_md_script), str(source_md), str(cleaned_md),
         "--fix-html", str(wechat_html)],
        "Fixing Image Renderers in WeChat HTML"
    )

    print(f"\nSUCCESS: All formats exported to {export_base}")
    print(f"- Internal: {internal_html.name}")
    print(f"- WeChat: wechat/article.html")
    print(f"- XHS Assets Ready in: xhs-images/")

if __name__ == "__main__":
    main()
