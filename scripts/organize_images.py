#!/usr/bin/env python3
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

def organize_pasted_images(wiki_dir: Path, fix: bool = False):
    """
    Find 'Pasted image' references in wiki markdown files and move them to the 
    appropriate figures subfolder.
    """
    wiki_dir = Path(wiki_dir).resolve()
    # Root directory is parent of wiki_dir
    root_dir = wiki_dir.parent
    
    # Internal wiki content dir
    content_dir = wiki_dir / "wiki"
    figures_dir = wiki_dir / "figures"
    
    if not content_dir.exists():
        # Fallback if structure is different
        content_dir = wiki_dir
        
    md_files = list(content_dir.rglob("*.md"))
    
    results = []
    
    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue
        
        # Matches ![[Pasted image ...]] or ![...](Pasted image ...)
        obsidian_pattern = r'!\[\[(Pasted image [^\]]+)\]\]'
        standard_pattern = r'!\[(.*?)\]\((.*?Pasted image [^)]+)\)'
        
        pasted_images = []
        
        # Extract from Obsidian style
        for match in re.finditer(obsidian_pattern, content):
            pasted_images.append({
                "full_match": match.group(0),
                "filename": match.group(1),
                "is_obsidian": True
            })
            
        # Extract from Standard style
        for match in re.finditer(standard_pattern, content):
            path_part = match.group(2)
            filename = Path(urllib.parse.unquote(path_part)).name
            pasted_images.append({
                "full_match": match.group(0),
                "label": match.group(1),
                "path": path_part,
                "filename": filename,
                "is_obsidian": False
            })
            
        if not pasted_images:
            continue
            
        doc_slug = md_file.stem
        target_subfolder = figures_dir / doc_slug
        
        if "sources" in str(md_file.parent) and figures_dir.exists():
            existing_folders = [d for d in figures_dir.iterdir() if d.is_dir()]
            for d in existing_folders:
                if d.name in doc_slug or (len(d.name) > 5 and d.name[:10] in doc_slug):
                    target_subfolder = d
                    break
        
        modified_content = content
        
        for img in pasted_images:
            img_filename = img["filename"]
            # Look for the image file
            found_path = None
            search_dirs = [
                root_dir,
                wiki_dir,
                md_file.parent,
                root_dir / "attachments",
                target_subfolder,
            ]
            
            for sd in search_dirs:
                if (sd / img_filename).exists():
                    found_path = sd / img_filename
                    break
            
            if not found_path:
                results.append({
                    "type": "missing-pasted-image",
                    "page": str(md_file.relative_to(wiki_dir)),
                    "detail": f"Missing image file: {img_filename}",
                    "severity": "warning"
                })
                continue
            
            if fix:
                target_subfolder.mkdir(parents=True, exist_ok=True)
                dest_path = target_subfolder / img_filename
                
                if found_path.resolve() != dest_path.resolve():
                    print(f"Moving {img_filename} to {target_subfolder.name}/")
                    try:
                        shutil.move(str(found_path), str(dest_path))
                    except Exception as e:
                        print(f"Error moving {img_filename}: {e}")
                
                # Update link in markdown
                try:
                    rel_to_wiki = md_file.relative_to(wiki_dir)
                    depth = len(rel_to_wiki.parents) - 1
                    prefix = "../" * depth
                    rel_link = f"{prefix}figures/{target_subfolder.name}/{img_filename}"
                except Exception:
                    rel_link = f"../../figures/{target_subfolder.name}/{img_filename}"
                
                # URL encode the link (especially for spaces)
                encoded_rel_link = urllib.parse.quote(rel_link, safe="/:")
                
                if img["is_obsidian"]:
                    new_link = f"![{img_filename}]({encoded_rel_link})"
                    modified_content = modified_content.replace(img["full_match"], new_link)
                else:
                    new_link = f"![{img['label']}]({encoded_rel_link})"
                    if img["full_match"] != new_link or " " in img["path"]:
                         modified_content = modified_content.replace(img["full_match"], new_link)
            else:
                results.append({
                    "type": "unorganized-image",
                    "page": str(md_file.relative_to(wiki_dir)),
                    "detail": f"Unorganized image found: {img_filename}",
                    "severity": "info"
                })
                
        if fix and modified_content != content:
            md_file.write_text(modified_content, encoding="utf-8")
            
    return results

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--wiki-dir", required=True)
    parser.add_argument("--fix", action="store_true")
    args = parser.parse_args()
    
    res = organize_pasted_images(Path(args.wiki_dir), args.fix)
    for r in res:
        print(f"[{r['severity']}] {r['page']}: {r['detail']}")
