#!/usr/bin/env python3
import sys, os, re, urllib.parse
from pathlib import Path

def clean_wikilinks(text):
    # Convert [[Link|Text]] -> Text
    text = re.sub(r'\[\[[^\]|]+\|([^\]]+)\]\]', r'\1', text)
    # Convert [[Link]] -> Link
    text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
    # Convert internal anchor links [[#anchor]] -> anchor text (if any) or just remove
    text = re.sub(r'\[\[#([^\]]+)\]\]', r'\1', text)
    return text

def prepare_public_md(source_path):
    source_path = Path(source_path).resolve()
    text = source_path.read_text(encoding='utf-8')

    # Remove frontmatter
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            text = text[end + 4:].lstrip('\n')

    # Skip list for H2 sections
    SKIP_H2 = {'基本信息', '项目资源', '相关页面', '引用关系', '启示与关联', '目录'}

    lines = text.splitlines()
    output_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Detect H2 section to skip (more flexible regex)
        h2_m = re.match(r'^##\s*(.+)$', line)
        if h2_m:
            heading = h2_m.group(1).strip()
            # Check if heading starts with any of the skip words or is exactly a skip word
            if any(heading.startswith(skip) for skip in SKIP_H2):
                i += 1
                # Skip until next heading
                while i < len(lines) and not re.match(r'^##?\s*', lines[i]):
                    i += 1
                continue
        
        # Fix image paths (unquote %20 and resolve)
        img_obs = re.match(r'^(\s*!\[\[)(.+?)(\]\]\s*)$', line)
        if img_obs:
            prefix, ref, suffix = img_obs.groups()
            unquoted_ref = urllib.parse.unquote(ref)
            resolved_path = None
            search_roots = [source_path.parent]
            p = source_path.parent
            for _ in range(4):
                search_roots.append(p)
                p = p.parent
            
            for root in search_roots:
                cand = os.path.normpath(os.path.join(root, unquoted_ref))
                if os.path.isfile(cand):
                    resolved_path = cand
                    break
                base = os.path.basename(unquoted_ref)
                cand2 = os.path.join(root, base)
                if os.path.isfile(cand2):
                    resolved_path = cand2
                    break
            
            if resolved_path:
                line = f"![image]({resolved_path})"
            else:
                line = clean_wikilinks(line)
        else:
            img_md = re.match(r'^(.*!\[[^\]]*\]\()([^)]+)(\).*)$', line)
            if img_md:
                prefix, path, suffix = img_md.groups()
                unquoted_path = urllib.parse.unquote(path)
                
                resolved_path = None
                search_roots = [source_path.parent]
                p = source_path.parent
                for _ in range(4):
                    search_roots.append(p)
                    p = p.parent
                
                for root in search_roots:
                    cand = os.path.normpath(os.path.join(root, unquoted_path))
                    if os.path.isfile(cand):
                        resolved_path = cand
                        break
                    base = os.path.basename(unquoted_path)
                    cand2 = os.path.join(root, base)
                    if os.path.isfile(cand2):
                        resolved_path = cand2
                        break
                
                if resolved_path:
                    line = f"{prefix}{resolved_path}{suffix}"
                else:
                    line = clean_wikilinks(line)
            else:
                line = clean_wikilinks(line)
        
        output_lines.append(line)
        i += 1

    return '\n'.join(output_lines)

def fix_html_placeholders(html_path):
    html_path = Path(html_path)
    if not html_path.exists():
        print(f"Error: {html_path} not found.")
        return
    
    content = html_path.read_text(encoding='utf-8')
    
    # Replace <img src="MDTOHTMLIMGPH_N" data-local-path="PATH" ...>
    # with <img src="PATH" ...>
    def _fix_img(m):
        full_tag = m.group(0)
        local_path_m = re.search(r'data-local-path="([^"]+)"', full_tag)
        if local_path_m:
            local_path = local_path_m.group(1)
            # Use string replace instead of re.sub to avoid backslash escaping issues
            placeholder_m = re.search(r'src="(MDTOHTMLIMGPH_\d+)"', full_tag)
            if placeholder_m:
                placeholder = placeholder_m.group(1)
                fixed_tag = full_tag.replace(f'src="{placeholder}"', f'src="{local_path}"')
                return fixed_tag
        return full_tag

    new_content = re.sub(r'<img [^>]+>', _fix_img, content)
    html_path.write_text(new_content, encoding='utf-8')
    print(f"✓ Fixed image placeholders in: {html_path}")

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: prepare_public_md.py <source.md> <output.md> [--fix-html <html_path>]")
        sys.exit(1)
    
    source = sys.argv[1]
    output = sys.argv[2]
    
    cleaned_content = prepare_public_md(source)
    Path(output).write_text(cleaned_content, encoding='utf-8')
    print(f"✓ Cleaned markdown saved to: {output}")

    if "--fix-html" in sys.argv:
        idx = sys.argv.index("--fix-html")
        if idx + 1 < len(sys.argv):
            fix_html_placeholders(sys.argv[idx + 1])
