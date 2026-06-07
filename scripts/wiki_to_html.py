#!/usr/bin/env python3
"""
wiki_to_html.py — personal_wiki source page → interactive dark-theme HTML.

Usage:
    python wiki_to_html.py <source.md> [--output <path>] [--title <override>]

Produces a self-contained HTML file (all CSS/JS/images inlined) modelled on
the v2.html dark-theme template.  Section names are detected by heading text
and mapped to interactive components:

    ## 核心观点           → accordion (.acc-item)
    ## 方法摘要           → arch-flow + formula-block + pipeline-grid
    ## 实验与结果         → exp-grid
    ## 与其他论文的关联   → related-grid
    ## 对X项目的启示      → ins-grid (insight cards)

All other ## sections fall back to generic rendered prose.
Images: ![[...]] Obsidian embeds are resolved relative to the source file and
inlined as base64 data URIs so the output is fully portable.
"""

import sys, os, re, base64, argparse, html as html_mod, urllib.parse
from pathlib import Path

# ─── YAML frontmatter ─────────────────────────────────────────────────────────

def parse_frontmatter(text):
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip()
    body = text[end + 4:].lstrip('\n')
    try:
        import yaml
        meta = yaml.safe_load(fm_raw) or {}
    except Exception:
        meta = {}
        for line in fm_raw.splitlines():
            m = re.match(r'^(\w+):\s*(.*)', line)
            if m:
                meta[m.group(1)] = m.group(2).strip('"\'[]')
    return meta, body

# ─── Image helpers ─────────────────────────────────────────────────────────────

def encode_image(path):
    ext = Path(path).suffix.lower().lstrip('.')
    mime = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png',
            'gif': 'gif', 'webp': 'webp', 'svg': 'svg+xml'}.get(ext, 'png')
    with open(path, 'rb') as f:
        data = base64.b64encode(f.read()).decode()
    return f'data:image/{mime};base64,{data}'

def resolve_image(ref, source_dir, extra_dirs=None):
    """Resolve an Obsidian [[ref]] or plain path to a base64 data URI."""
    ref = urllib.parse.unquote(ref.strip())
    m = re.match(r'^\[\[(.+?)\]\]$', ref)
    if m:
        ref = m.group(1)

    search_roots = [source_dir] + (extra_dirs or [])

    for root in search_roots:
        candidate = os.path.normpath(os.path.join(root, ref))
        if os.path.isfile(candidate):
            return encode_image(candidate)
        basename = os.path.basename(ref)
        candidate2 = os.path.join(root, basename)
        if os.path.isfile(candidate2):
            return encode_image(candidate2)

    basename = os.path.basename(ref)
    for root in search_roots:
        for dirpath, _, files in os.walk(root):
            if basename in files:
                return encode_image(os.path.join(dirpath, basename))

    return None

# ─── Markdown inline renderer ─────────────────────────────────────────────────

def esc(text):
    return html_mod.escape(str(text), quote=False)

def md_inline(text, source_dir=None, extra_dirs=None):
    text = esc(text)
    
    # Inline images: ![alt](url)
    def _img_sub(m):
        alt, path = m.group(1), m.group(2)
        uri = resolve_image(path, source_dir, extra_dirs)
        src = uri or path
        return f'<img src="{src}" alt="{alt}" class="inline-img">'
    
    text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', _img_sub, text)
    
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r'\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]',
                  lambda m2: f'<span class="wikilink">{esc(m2.group(1))}</span>', text)
    return text

# ─── LaTeX math → HTML ────────────────────────────────────────────────────────

def render_math(expr):
    expr = re.sub(r'\^{([^}]+)}', lambda m: f'<sup>{m.group(1)}</sup>', expr)
    expr = re.sub(r'\^(\w)',       lambda m: f'<sup>{m.group(1)}</sup>', expr)
    expr = re.sub(r'_{([^}]+)}',   lambda m: f'<sub>{m.group(1)}</sub>', expr)
    expr = re.sub(r'_(\w)',        lambda m: f'<sub>{m.group(1)}</sub>', expr)
    replacements = [
        (r'\\cdot', '·'), (r'\\times', '×'), (r'\\in', '∈'),
        (r'\\sum', '∑'), (r'\\otimes', '⊗'), (r'\\oplus', '⊕'),
        (r'\\rightarrow', '→'), (r'\\leftarrow', '←'),
        (r'\\approx', '≈'), (r'\\neq', '≠'), (r'\\leq', '≤'),
        (r'\\geq', '≥'), (r'\\nabla', '∇'), (r'\\partial', '∂'),
        (r'\\mathbb{R}', 'ℝ'), (r'\\mathcal{L}', 'ℒ'),
        (r'\\alpha', 'α'), (r'\\beta', 'β'), (r'\\gamma', 'γ'),
        (r'\\delta', 'δ'), (r'\\epsilon', 'ε'), (r'\\lambda', 'λ'),
        (r'\\mu', 'μ'), (r'\\sigma', 'σ'), (r'\\theta', 'θ'),
        (r'\\phi', 'φ'), (r'\\pi', 'π'), (r'\\rho', 'ρ'),
        (r'\\text\{([^}]+)\}', r'\1'),
        (r'\\mathrm\{([^}]+)\}', r'\1'),
        (r'\\mathbf\{([^}]+)\}', r'<strong>\1</strong>'),
    ]
    for pat, rep in replacements:
        expr = re.sub(pat, rep, expr)
    return expr

# ─── Markdown block renderer ───────────────────────────────────────────────────

def _render_list(lines, start, inline_fn, ordered=False):
    """Render a bullet or numbered list starting at lines[start], supporting one
    level of indented sub-lists (2+ leading spaces)."""
    tag = 'ol' if ordered else 'ul'
    item_pat = re.compile(r'^\d+\.\s+(.*)') if ordered else re.compile(r'^[-*]\s+(.*)')
    sub_pat = re.compile(r'^\s{2,}[-*\d]')

    items_html = ''
    i = start
    while i < len(lines):
        line = lines[i]
        m = item_pat.match(line)
        if m:
            text_content = m.group(1)
            # Look ahead for indented continuation / sub-list
            sub_lines = []
            j = i + 1
            while j < len(lines) and lines[j].startswith('  ') and lines[j].strip():
                sub_lines.append(lines[j])
                j += 1
            if sub_lines:
                # Check if sub-lines are a nested list or just continuation text
                if any(sub_pat.match(sl) for sl in sub_lines):
                    dedented = [sl[2:] for sl in sub_lines]
                    sub_is_ordered = bool(re.match(r'^\d+\.', dedented[0]))
                    nested_html = _render_list(dedented, 0, inline_fn, ordered=sub_is_ordered)
                    items_html += f'<li>{inline_fn(text_content)}{nested_html}</li>'
                else:
                    # Continuation lines: join into the paragraph text
                    full_text = text_content + ' ' + ' '.join(sl.strip() for sl in sub_lines)
                    items_html += f'<li>{inline_fn(full_text)}</li>'
                i = j
            else:
                items_html += f'<li>{inline_fn(text_content)}</li>'
                i += 1
        elif line.startswith('  ') and line.strip():
            # Orphaned indented line — consume silently (already handled in lookahead)
            i += 1
        else:
            break
    return f'<{tag}>{items_html}</{tag}>'


def render_blocks(text, source_dir=None, extra_dirs=None):
    lines = text.splitlines()
    out = []
    i = 0

    def _inline(t):
        return md_inline(t, source_dir, extra_dirs)

    while i < len(lines):
        line = lines[i]

        img_obs = re.match(r'^\s*!\[\[(.+?)\]\]', line)
        if img_obs:
            uri = resolve_image(f'[[{img_obs.group(1)}]]', source_dir or '.', extra_dirs)
            if uri:
                out.append(f'<figure class="wiki-fig"><img src="{uri}" alt="" loading="lazy"></figure>')
            i += 1
            continue

        img_md = re.match(r'^\s*!\[([^\]]*)\]\(([^)]+)\)', line)
        if img_md:
            uri = resolve_image(img_md.group(2), source_dir or '.', extra_dirs)
            src = uri or esc(img_md.group(2))
            alt = esc(img_md.group(1))
            out.append(f'<figure class="wiki-fig"><img src="{src}" alt="{alt}" loading="lazy"></figure>')
            i += 1
            continue

        fence_m = re.match(r'^```(\w*)', line)
        if fence_m:
            lang = fence_m.group(1)
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1
            code = esc('\n'.join(code_lines))
            out.append(f'<pre><code class="lang-{lang}">{code}</code></pre>')
            continue

        if line.startswith('> '):
            bq = []
            while i < len(lines) and lines[i].startswith('> '):
                bq.append(lines[i][2:])
                i += 1
            out.append(f'<blockquote><p>{_inline(" ".join(bq))}</p></blockquote>')
            continue

        if re.match(r'^---+$', line.strip()):
            out.append('<hr>')
            i += 1
            continue

        if '|' in line:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and re.match(r'^[\|\s\-:]+$', lines[j]):
                tbl_lines = []
                while i < len(lines) and ('|' in lines[i] or re.match(r'^[\s\|\-:]+$', lines[i])):
                    tbl_lines.append(lines[i])
                    i += 1
                out.append(render_md_table('\n'.join(tbl_lines), _inline))
                continue

        h_m = re.match(r'^(#{3,6})\s+(.+)', line)
        if h_m:
            lvl = len(h_m.group(1))
            text_h = _inline(h_m.group(2).strip())
            out.append(f'<h{lvl} class="sec-sub">{text_h}</h{lvl}>')
            i += 1
            continue

        if re.match(r'^[-*]\s', line):
            out.append(_render_list(lines, i, _inline, ordered=False))
            # advance i past the consumed list
            while i < len(lines) and (re.match(r'^[-*]\s', lines[i]) or
                                       re.match(r'^\s{2,}', lines[i])):
                i += 1
            continue

        if re.match(r'^\d+\.\s', line):
            out.append(_render_list(lines, i, _inline, ordered=True))
            while i < len(lines) and (re.match(r'^\d+\.\s', lines[i]) or
                                       re.match(r'^\s{2,}', lines[i])):
                i += 1
            continue

        math_m = re.match(r'^\$\$(.+?)\$\$\s*$', line.strip())
        if math_m:
            out.append(f'<div class="disp-math">{render_math(math_m.group(1))}</div>')
            i += 1
            continue

        math_m2 = re.match(r'^\$(.+?)\$\s*$', line.strip())
        if math_m2:
            out.append(f'<div class="disp-math">{render_math(math_m2.group(1))}</div>')
            i += 1
            continue

        if not line.strip():
            i += 1
            continue

        para = []
        while i < len(lines) and lines[i].strip() and \
              not re.match(r'^(#{1,6}|\d+\.|>|```|\|)', lines[i]) and \
              not re.match(r'^[-*]\s', lines[i]) and \
              not re.match(r'^\s*!\[', lines[i]):  # image lines must not be absorbed into paragraphs
            raw_line = lines[i]
            # Soft line break: line ends with two or more spaces → <br>
            if raw_line.endswith('  '):
                para.append(_inline(raw_line.rstrip()) + '<br>')
            else:
                para.append(_inline(raw_line))
            i += 1
        if para:
            out.append(f'<p>{"".join(para)}</p>')
        else:
            i += 1

    return '\n'.join(out)

def render_md_table(text, inline_fn=None):
    if inline_fn is None:
        inline_fn = md_inline
    lines = [l for l in text.splitlines() if l.strip()]
    data_lines = [l for l in lines if not re.match(r'^[\|\s\-:]+$', l)]
    if not data_lines:
        return ''

    def parse_row(line):
        return [c.strip() for c in line.strip('| ').split('|')]

    headers = parse_row(data_lines[0])
    rows = [parse_row(l) for l in data_lines[1:]]

    th = ''.join(f'<th>{inline_fn(h)}</th>' for h in headers)
    body_rows = ''.join(
        '<tr>' + ''.join(f'<td>{inline_fn(c)}</td>' for c in row) + '</tr>'
        for row in rows
    )
    return f'<div class="tbl-wrap"><table class="wiki-tbl"><thead><tr>{th}</tr></thead><tbody>{body_rows}</tbody></table></div>'

# ─── Section splitter ──────────────────────────────────────────────────────────

def split_h2_sections(body):
    pattern = re.compile(r'^## +(.+)$', re.MULTILINE)
    parts = pattern.split(body)
    intro = parts[0]
    sections = []
    for k in range(1, len(parts), 2):
        heading = parts[k].strip()
        content = parts[k + 1] if k + 1 < len(parts) else ''
        sections.append((heading, content))
    return intro, sections

def split_h3_sections(content):
    pattern = re.compile(r'^### +(.+)$', re.MULTILINE)
    parts = pattern.split(content)
    intro = parts[0]
    subs = []
    for k in range(1, len(parts), 2):
        heading = parts[k].strip()
        sub_content = parts[k + 1] if k + 1 < len(parts) else ''
        subs.append((heading, sub_content))
    return intro, subs

# ─── Specialized section renderers ────────────────────────────────────────────

def render_hero(meta, title, lead_quote, source_dir=None):
    arxiv_id = meta.get('arxiv_id', '')
    year = meta.get('year', '')
    authors_raw = meta.get('authors', [])
    if isinstance(authors_raw, list):
        authors_str = ', '.join(str(a) for a in authors_raw[:4])
        if len(authors_raw) > 4:
            authors_str += ' et al.'
    else:
        authors_str = str(authors_raw)

    category = meta.get('category', '')
    if not category:
        tags = meta.get('tags', [])
        if isinstance(tags, list):
            category = next((t for t in tags if t not in ('素材摘要', '论文')), '')
        elif isinstance(tags, str):
            category = tags

    is_seed = meta.get('is_seed', False)
    sources = meta.get('sources', [])
    if isinstance(sources, list) and sources:
        paper_url = sources[0]
    else:
        paper_url = f'https://arxiv.org/abs/{arxiv_id}' if arxiv_id else '#'

    citations = meta.get('citations', 0)

    # Detect "plain" markdown: no wiki-specific metadata present
    is_wiki_page = any([arxiv_id, year, authors_str, category, is_seed, citations,
                        (paper_url and paper_url != '#')])

    lead_html = ''
    if lead_quote:
        lead_html = f'<p class="hero-lead">{md_inline(lead_quote)}</p>'

    if not is_wiki_page:
        # Minimal hero: just title + optional lead quote, no badges/chips
        return f'''<section class="hero" id="top">
  <div class="hero-inner">
    <h1 class="hero-title">{esc(title)}</h1>
    {lead_html}
  </div>
</section>'''

    badges = []
    if is_seed:
        badges.append('<span class="badge seed">种子论文</span>')
    if category:
        badges.append(f'<span class="badge cat">{esc(category)}</span>')
    if arxiv_id:
        badges.append(f'<span class="badge arxiv">arXiv:{esc(str(arxiv_id))}</span>')
    if year:
        badges.append(f'<span class="badge yr">{esc(str(year))}</span>')
    badge_html = ''.join(badges)

    chips = []
    if authors_str:
        chips.append(f'<span class="chip"><span class="chip-label">作者</span>{esc(authors_str)}</span>')
    if citations:
        chips.append(f'<span class="chip"><span class="chip-label">引用</span>{esc(str(citations))}</span>')
    if paper_url and paper_url != '#':
        chips.append(f'<a class="chip chip-link" href="{esc(paper_url)}" target="_blank">→ 论文链接</a>')
    chip_html = ''.join(chips)

    return f'''<section class="hero" id="top">
  <div class="hero-inner">
    <div class="badge-row">{badge_html}</div>
    <h1 class="hero-title">{esc(title)}</h1>
    {lead_html}
    <div class="chip-row">{chip_html}</div>
  </div>
</section>'''

def render_accordion(content, source_dir=None, extra_dirs=None):
    items = []
    current = None
    current_body = []

    for line in content.splitlines():
        m = re.match(r'^(\d+)\.\s+\*\*(.+?)\*\*[：:。]?\s*(.*)', line)
        if m:
            if current is not None:
                items.append({'num': current['num'], 'title': current['title'],
                              'body': '\n'.join(current_body)})
            current = {'num': m.group(1), 'title': m.group(2), 'first': m.group(3)}
            current_body = [m.group(3)] if m.group(3).strip() else []
        elif current is not None:
            current_body.append(line)

    if current is not None:
        items.append({'num': current['num'], 'title': current['title'],
                      'body': '\n'.join(current_body)})

    if not items:
        return render_blocks(content, source_dir, extra_dirs)

    html = '<section class="section" id="core"><div class="sec-inner">\n'
    html += '<h2 class="sec-title">核心观点</h2>\n'
    html += '<div class="acc-list">\n'

    for item in items:
        body_html = render_blocks(item['body'], source_dir, extra_dirs)
        html += f'''<div class="acc-item">
  <div class="acc-hd" onclick="tog(this)">
    <span class="acc-num">{esc(item["num"])}</span>
    <span class="acc-text">{md_inline(item["title"])}</span>
    <span class="acc-arrow">›</span>
  </div>
  <div class="acc-body">{body_html}</div>
</div>\n'''

    html += '</div>\n</div></section>\n'
    return html

def render_method(content, source_dir=None, extra_dirs=None):
    intro, subs = split_h3_sections(content)

    html = '<section class="section" id="method"><div class="sec-inner">\n'
    html += '<h2 class="sec-title">方法摘要</h2>\n'

    if intro.strip():
        html += _render_method_intro(intro, source_dir, extra_dirs)

    for sub_title, sub_content in subs:
        html += _render_method_sub(sub_title, sub_content, source_dir, extra_dirs)

    html += '</div></section>\n'
    return html

def _render_method_intro(text, source_dir, extra_dirs):
    fence_m = re.search(r'```[^\n]*\n(.*?)```', text, re.DOTALL)
    if fence_m:
        code_text = fence_m.group(1)
        before = text[:fence_m.start()]
        after = text[fence_m.end():]
        out = ''
        if before.strip():
            out += render_blocks(before, source_dir, extra_dirs)
        arch_html = _parse_arch_layers(code_text)
        out += arch_html if arch_html else f'<pre><code>{esc(code_text)}</code></pre>'
        if after.strip():
            out += render_blocks(after, source_dir, extra_dirs)
        return out
    return render_blocks(text, source_dir, extra_dirs)

def _parse_arch_layers(code_text):
    lines = [l for l in code_text.splitlines() if l.strip()]
    if not lines:
        return ''

    layers = []
    current_layer = None
    current_items = []

    for line in lines:
        layer_m = re.match(r'^层\s*(\d+)[：:]\s*(.+)', line)
        if layer_m:
            if current_layer:
                layers.append({'num': current_layer['num'], 'name': current_layer['name'],
                               'items': current_items})
            current_layer = {'num': layer_m.group(1), 'name': layer_m.group(2).strip()}
            current_items = []
        elif current_layer is not None:
            item_text = re.sub(r'^[\s├└│─]+', '', line).strip()
            if item_text:
                current_items.append(item_text)

    if current_layer:
        layers.append({'num': current_layer['num'], 'name': current_layer['name'],
                       'items': current_items})

    if not layers:
        return ''

    colors = ['layer1', 'layer2', 'layer3', 'layer4']
    html = '<div class="arch-flow">\n'
    for idx, layer in enumerate(layers):
        color_cls = colors[idx % len(colors)]
        items_html = ''.join(f'<li>{md_inline(it)}</li>' for it in layer['items'])
        html += f'''<div class="arch-card {color_cls}">
  <div class="arch-num">层 {esc(layer["num"])}</div>
  <div class="arch-name">{md_inline(layer["name"])}</div>
  <ul class="arch-items">{items_html}</ul>
</div>\n'''
    html += '</div>\n'
    return html

def _render_method_sub(title, content, source_dir, extra_dirs):
    has_math = bool(re.search(r'\$\$.*?\$\$|\$[^$\n]+\$', content))
    has_pipeline = bool(re.search(r'\*\*阶段\s*\d+', content))

    html = f'<div class="method-sub" id="sub-{_slugify(title)}">\n'
    html += f'<h3 class="sub-title">{md_inline(title)}</h3>\n'

    if has_math:
        html += _render_formula_section(content, source_dir, extra_dirs)
    elif has_pipeline:
        html += _render_pipeline_section(content, source_dir, extra_dirs)
    else:
        html += render_blocks(content, source_dir, extra_dirs)

    html += '</div>\n'
    return html

def _render_formula_section(content, source_dir, extra_dirs):
    lines = content.splitlines()
    out = []
    i = 0
    formula_buffer = []

    def flush_formulas():
        nonlocal formula_buffer
        if formula_buffer:
            fb = ''.join(f'<div class="formula-line">{f}</div>' for f in formula_buffer)
            out.append(f'<div class="formula-block">{fb}</div>')
            formula_buffer = []

    while i < len(lines):
        line = lines[i]

        math_m = re.match(r'^\s*\$\$(.+?)\$\$\s*$', line)
        math_m2 = re.match(r'^\s*\$(.+?)\$\s*$', line) if not math_m else None
        img_obs = re.match(r'^\s*!\[\[(.+?)\]\]\s*$', line)

        if math_m or math_m2:
            expr = (math_m or math_m2).group(1)
            formula_buffer.append(render_math(expr))
            i += 1
            continue

        if img_obs:
            flush_formulas()
            uri = resolve_image(f'[[{img_obs.group(1)}]]', source_dir or '.', extra_dirs)
            if uri:
                out.append(f'<figure class="wiki-fig"><img src="{uri}" alt="" loading="lazy"></figure>')
            i += 1
            continue

        if '|' in line:
            flush_formulas()
            tbl_lines = []
            while i < len(lines) and ('|' in lines[i] or re.match(r'^[\s\|\-:]+$', lines[i])):
                tbl_lines.append(lines[i])
                i += 1
            out.append(render_md_table('\n'.join(tbl_lines), md_inline))
            continue

        if line.startswith('> '):
            flush_formulas()
            bq = []
            while i < len(lines) and lines[i].startswith('> '):
                bq.append(lines[i][2:])
                i += 1
            out.append(f'<blockquote><p>{md_inline(" ".join(bq))}</p></blockquote>')
            continue

        h_m = re.match(r'^(#{4,6})\s+(.+)', line)
        if h_m:
            flush_formulas()
            lvl = len(h_m.group(1))
            out.append(f'<h{lvl}>{md_inline(h_m.group(2))}</h{lvl}>')
            i += 1
            continue

        flush_formulas()
        if line.strip():
            out.append(f'<p>{md_inline(line)}</p>')
        i += 1

    flush_formulas()
    return '\n'.join(out)

def _pipeline_card_end(body_text):
    """Find where a pipeline stage's own content ends and independent modules begin.

    A stage card's "own" content is its bullets, numbered steps, and one-liner prose.
    An independent module starts when we see, after a blank line:
      - A standalone bold heading:  **Heading**  or  **Heading：**
      - A bold-prefixed paragraph:  **Label：** body text...  (label 4+ chars, ends with ：/**)
      - A markdown table (| col |)
      - An image embed  ![[...]]
    Tables and images that appear *before* the first blank line are considered part of
    the card (e.g. a small timing table directly under the bullets).
    """
    lines = body_text.splitlines()
    passed_first_blank = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if not stripped:
            passed_first_blank = True
            continue

        if not passed_first_blank:
            continue

        prev_blank = not lines[i - 1].strip() if i > 0 else True

        if not prev_blank:
            continue

        # Table or image after blank line → end of card
        if stripped.startswith('|') or stripped.startswith('![['):
            return len('\n'.join(lines[:i]))

        # Pure bold heading: **text** or **text：**
        if re.match(r'^\*\*[^*]+\*\*[：:。]?\s*$', stripped):
            return len('\n'.join(lines[:i]))

        # Bold-prefixed paragraph: **Label（any）：** prose  (label ≥4 chars, colon inside bold)
        # Matches:  **输出资产格式（...）：** content
        # Matches:  **与 DISCOVERSE...**  (standalone heading variant)
        # Does NOT match:  **短标题**：内容  (short inline bold, part of stage body)
        if re.match(r'^\*\*[^*]{6,}[：:][^*]*\*\*', stripped) and \
                not re.match(r'^[-*\d]', stripped):
            return len('\n'.join(lines[:i]))

    return len(body_text)

def _render_pipeline_section(content, source_dir, extra_dirs):
    stage_pattern = re.compile(r'\*\*阶段\s*(\d+)[：:]\s*(.+?)\*\*', re.MULTILINE)
    stages = list(stage_pattern.finditer(content))

    if not stages:
        return render_blocks(content, source_dir, extra_dirs)

    # Everything before the first stage and after the last stage's card content
    # is rendered as prose below the grid.
    prefix = content[:stages[0].start()].strip()
    last_stage_end_in_content = stages[-1].end()

    html = ''
    if prefix:
        html += render_blocks(prefix, source_dir, extra_dirs)

    html += '<div class="pipe-grid">\n'
    tail_blocks = []  # content after each stage's card portion, to render after grid

    for idx, stg in enumerate(stages):
        stage_num = stg.group(1)
        stage_name = stg.group(2).strip()
        raw_start = stg.end()
        raw_end = stages[idx + 1].start() if idx + 1 < len(stages) else len(content)
        stage_body = content[raw_start:raw_end]

        # Find where the stage's tight content ends
        card_cut = _pipeline_card_end(stage_body)
        card_text = stage_body[:card_cut].strip()
        overflow = stage_body[card_cut:].strip()

        # Remove images and tables from the card itself — they render in overflow
        card_clean = re.sub(r'!\[\[.+?\]\]', '', card_text)
        card_clean = re.sub(r'(\|[^\n]+\n?)+', '', card_clean).strip()
        body_html = render_blocks(card_clean, source_dir, extra_dirs) if card_clean else ''

        color_cls = f'pipe-c{(idx % 5) + 1}'
        html += f'''<div class="pipe-card {color_cls}">
  <div class="pipe-num">阶段 {esc(stage_num)}</div>
  <div class="pipe-name">{md_inline(stage_name)}</div>
  {f'<div class="pipe-body">{body_html}</div>' if body_html else ""}
</div>\n'''

        # Collect images/tables from card_text + overflow for rendering after grid
        if card_text:
            tail_blocks.append(('images_tables', card_text))
        if overflow:
            tail_blocks.append(('full', overflow))

    html += '</div>\n'

    # Render all post-grid content in document order
    seen_imgs = set()
    seen_tbls = set()
    for kind, block in tail_blocks:
        if kind == 'full':
            html += render_blocks(block, source_dir, extra_dirs)
        else:
            # Only images and tables extracted from card text
            for img_m in re.finditer(r'!\[\[(.+?)\]\]', block):
                key = img_m.group(1)
                if key not in seen_imgs:
                    seen_imgs.add(key)
                    uri = resolve_image(f'[[{key}]]', source_dir or '.', extra_dirs)
                    if uri:
                        html += f'<figure class="wiki-fig"><img src="{uri}" alt="" loading="lazy"></figure>'
            for tbl_m in re.finditer(r'((?:\|[^\n]+\n)+)', block):
                tbl_text = tbl_m.group(1)
                key = tbl_text[:40]
                if key not in seen_tbls and re.search(r'\|\s*[-:]+\s*\|', tbl_text):
                    seen_tbls.add(key)
                    html += render_md_table(tbl_text, md_inline)

    return html

def render_experiments(content, source_dir=None, extra_dirs=None):
    intro, subs = split_h3_sections(content)

    html = '<section class="section" id="experiments"><div class="sec-inner">\n'
    html += '<h2 class="sec-title">实验与结果</h2>\n'

    if intro.strip():
        html += f'<div class="exp-intro">{render_blocks(intro, source_dir, extra_dirs)}</div>\n'

    if subs:
        exp_colors = ['exp-c1', 'exp-c2', 'exp-c3', 'exp-c4', 'exp-c5']
        for idx, (sub_title, sub_content) in enumerate(subs):
            color_cls = exp_colors[idx % len(exp_colors)]
            body_html = render_blocks(sub_content, source_dir, extra_dirs)
            clean_title = re.sub(r'^[A-Z]\.\s+', '', sub_title)
            # Use accordion-style full-width panels — images and tables render naturally
            html += f'''<div class="exp-panel {color_cls}">
  <div class="exp-panel-hd" onclick="tog(this)">
    <span class="exp-label">{chr(65 + idx)}</span>
    <span class="exp-panel-title">{md_inline(clean_title)}</span>
    <span class="acc-arrow">›</span>
  </div>
  <div class="acc-body exp-panel-body">{body_html}</div>
</div>\n'''

    html += '</div></section>\n'
    return html

def render_related(content, source_dir=None, extra_dirs=None):
    html = '<section class="section" id="related"><div class="sec-inner">\n'
    html += '<h2 class="sec-title">关联论文</h2>\n'
    html += '<div class="related-grid">\n'

    for line in content.splitlines():
        m = re.match(r'^[-*]\s+\[\[(.+?)\]\][：:]?\s*(.*)', line)
        if m:
            paper_ref = m.group(1)
            description = m.group(2).strip()
            arxiv_m = re.search(r'(\d{4}\.\d{4,5})', paper_ref)
            arxiv_id = arxiv_m.group(1) if arxiv_m else ''
            link = f'https://arxiv.org/abs/{arxiv_id}' if arxiv_id else '#'
            title = re.sub(r'-\d{4}\.\d{4,5}$', '', paper_ref).replace('-', ' ').strip()
            html += f'''<div class="rel-card">
  <div class="rel-title"><a href="{link}" target="_blank">{md_inline(title)}</a></div>
  <div class="rel-desc">{md_inline(description)}</div>
</div>\n'''

    html += '</div>\n</div></section>\n'
    return html

def render_insights(content, heading_text, source_dir=None, extra_dirs=None):
    proj_m = re.search(r'对(.+?)项目', heading_text)
    proj_name = proj_m.group(1) if proj_m else '项目'

    html = '<section class="section" id="insights"><div class="sec-inner">\n'
    html += f'<h2 class="sec-title">对 {esc(proj_name)} 的启示</h2>\n'
    html += '<div class="ins-grid">\n'

    ins_count = 0
    for line in content.splitlines():
        m = re.match(r'^>\s*(\d+)\.\s+\*\*(.+?)\*\*[：:]?\s*(.*)', line)
        m2 = re.match(r'^(\d+)\.\s+\*\*(.+?)\*\*[：:]?\s*(.*)', line) if not m else None
        item = m or m2
        if item:
            ins_count += 1
            num, ttl, desc = item.group(1), item.group(2), item.group(3)
            html += f'''<div class="ins-card">
  <div class="ins-num">{esc(num)}</div>
  <div class="ins-title">{md_inline(ttl)}</div>
  <div class="ins-desc">{md_inline(desc)}</div>
</div>\n'''

    if ins_count == 0:
        html += f'<div class="ins-wide">{render_blocks(content, source_dir, extra_dirs)}</div>\n'

    html += '</div>\n</div></section>\n'
    return html

def render_abstract(content, source_dir=None, extra_dirs=None):
    html = '<section class="section" id="abstract"><div class="sec-inner">\n'
    html += '<h2 class="sec-title">摘要</h2>\n'
    html += f'<div class="abstract-block">{render_blocks(content, source_dir, extra_dirs)}</div>\n'
    html += '</div></section>\n'
    return html

def render_generic_section(heading, content, section_id, source_dir=None, extra_dirs=None):
    html = f'<section class="section" id="{section_id}"><div class="sec-inner">\n'
    html += f'<h2 class="sec-title">{md_inline(heading)}</h2>\n'
    html += render_blocks(content, source_dir, extra_dirs)
    html += '</div></section>\n'
    return html

def _slugify(text):
    return re.sub(r'[^\w-]', '-', text.lower())[:40].strip('-')

# ─── Nav builder ──────────────────────────────────────────────────────────────

def build_nav(sections):
    SECTION_LABELS = {
        '核心观点': ('核心观点', 'core'),
        '方法摘要': ('方法', 'method'),
        '实验与结果': ('实验', 'experiments'),
        '与其他论文的关联': ('关联', 'related'),
        '摘要': ('摘要', 'abstract'),
    }
    nav_items = ['<li><a href="#top">概览</a></li>']
    _SKIP = {'基本信息', '项目资源', '相关页面', '引用关系', '目录'}
    for heading, _ in sections:
        if heading in _SKIP:
            continue
        if heading in SECTION_LABELS:
            label, sec_id = SECTION_LABELS[heading]
        elif re.search(r'项目的启示|对.*的启示', heading):
            label, sec_id = '启示', 'insights'
        else:
            label = heading[:14] + ('…' if len(heading) > 14 else '')
            sec_id = _slugify(heading)
        nav_items.append(f'<li><a href="#{sec_id}">{esc(label)}</a></li>')
    nav_html = '\n'.join(nav_items)
    return f'<nav class="top-nav"><ul>{nav_html}</ul></nav>'

# ─── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:  #0a0e1a; --bg2: #111827; --bg3: #1f2937; --bg4: #374151;
  --border: #2d3748; --border2: #4a5568;
  --accent:  #60a5fa;
  --accent2: #34d399;
  --accent3: #f87171;
  --accent4: #c084fc;
  --accent5: #fbbf24;
  --text:  #f1f5f9;
  --text2: #94a3b8;
  --text3: #cbd5e1;
  --radius: 10px;
  --font-sans: ui-sans-serif, system-ui, -apple-system, sans-serif;
  --font-serif: "Source Serif 4", "Newsreader", Georgia, serif;
  --font-mono: ui-monospace, "Cascadia Code", "Fira Code", monospace;
}
html { scroll-behavior: smooth; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 15px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}

/* Nav */
.top-nav {
  position: fixed; top: 0; left: 0; right: 0; z-index: 100;
  background: rgba(10,14,26,.92); backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  padding: 0 2rem; height: 48px;
  display: flex; align-items: center;
}
.top-nav ul { list-style: none; display: flex; gap: 1.5rem; }
.top-nav a { color: var(--text2); text-decoration: none; font-size: .875rem; transition: color .15s; }
.top-nav a:hover { color: var(--accent); }

/* Hero */
.hero {
  padding: 7rem 2rem 3.5rem;
  background: linear-gradient(160deg, #0d1427 0%, #0a0e1a 60%);
  border-bottom: 1px solid var(--border);
}
.hero-inner { max-width: 860px; margin: 0 auto; }
.hero-title {
  font-family: var(--font-serif);
  font-size: clamp(1.6rem, 3.5vw, 2.4rem);
  font-weight: 600;
  line-height: 1.25;
  text-wrap: pretty;
  color: var(--text);
  margin: .75rem 0 1rem;
  letter-spacing: -.01em;
}
.hero-lead {
  font-size: 1rem;
  color: var(--text3);
  line-height: 1.7;
  max-width: 720px;
  border-left: 3px solid var(--accent2);
  padding-left: 1rem;
  margin-bottom: 1.25rem;
  text-wrap: pretty;
}

/* Badges */
.badge-row { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: .75rem; }
.badge {
  display: inline-block; padding: .2rem .6rem;
  border-radius: 99px; font-size: .75rem; font-weight: 500;
  letter-spacing: .02em;
}
.badge.seed  { background: #fbbf2422; color: var(--accent5); border: 1px solid #fbbf2440; }
.badge.cat   { background: #34d39922; color: var(--accent2); border: 1px solid #34d39940; }
.badge.arxiv { background: #60a5fa22; color: var(--accent);  border: 1px solid #60a5fa40; }
.badge.yr    { background: #c084fc22; color: var(--accent4); border: 1px solid #c084fc40; }

/* Chips */
.chip-row { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .75rem; }
.chip {
  display: inline-flex; align-items: center; gap: .35rem;
  padding: .3rem .7rem; border-radius: var(--radius);
  background: var(--bg3); border: 1px solid var(--border);
  font-size: .8rem; color: var(--text2);
}
.chip-label { color: var(--text2); font-size: .7rem; opacity: .7; }
.chip-link { color: var(--accent); text-decoration: none; }
.chip-link:hover { color: var(--text); background: var(--bg4); }

/* Sections */
.section { padding: 3rem 2rem; border-bottom: 1px solid var(--border); }
.sec-inner { max-width: 860px; margin: 0 auto; }
.sec-title {
  font-family: var(--font-serif);
  font-size: 1.5rem; font-weight: 600;
  color: var(--text); margin-bottom: 1.5rem;
  padding-bottom: .5rem;
  border-bottom: 1px solid var(--border);
}
.sec-sub { font-size: 1.1rem; font-weight: 600; color: var(--text3); margin: 1.25rem 0 .6rem; }

/* Accordion */
.acc-list { display: flex; flex-direction: column; gap: .5rem; }
.acc-item { border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; transition: border-color .2s; }
.acc-item.open { border-color: var(--accent2); }
.acc-hd {
  display: flex; align-items: center; gap: .75rem;
  padding: .85rem 1rem; cursor: pointer;
  background: var(--bg2); user-select: none; transition: background .15s;
}
.acc-hd:hover { background: var(--bg3); }
.acc-num {
  min-width: 2rem; height: 2rem;
  display: flex; align-items: center; justify-content: center;
  border-radius: 50%;
  background: var(--accent2); color: var(--bg);
  font-size: .8rem; font-weight: 700; flex-shrink: 0;
}
.acc-text { flex: 1; font-size: .95rem; font-weight: 500; color: var(--text); }
.acc-arrow { font-size: 1.2rem; color: var(--text2); transition: transform .2s; }
.acc-item.open .acc-arrow,
.exp-panel.open .acc-arrow { transform: rotate(90deg); }
.acc-body {
  display: none; padding: 1rem 1rem 1rem 3.5rem;
  background: var(--bg); border-top: 1px solid var(--border);
  font-size: .9rem; color: var(--text3);
}
.acc-item.open .acc-body,
.exp-panel.open .acc-body { display: block; }

/* Arch flow */
.arch-flow {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem; margin: 1.25rem 0;
}
.arch-card { border-radius: var(--radius); padding: 1rem; border: 1px solid var(--border); background: var(--bg2); }
.arch-card.layer1 { border-top: 3px solid var(--accent2); }
.arch-card.layer2 { border-top: 3px solid var(--accent); }
.arch-card.layer3 { border-top: 3px solid var(--accent5); }
.arch-card.layer4 { border-top: 3px solid var(--accent3); }
.arch-num { font-size: .7rem; color: var(--text2); margin-bottom: .25rem; }
.arch-name { font-size: .95rem; font-weight: 600; color: var(--text); margin-bottom: .6rem; }
.arch-items { padding-left: 1.2rem; font-size: .85rem; color: var(--text3); }
.arch-items li { margin-bottom: .2rem; }

/* Formula block */
.formula-block {
  margin: 1.25rem 0; padding: 1.25rem 1.5rem;
  background: var(--bg2); border: 1px solid var(--border);
  border-left: 3px solid var(--accent4);
  border-radius: var(--radius);
  font-family: var(--font-serif); font-size: 1.05rem;
}
.formula-line { padding: .35rem 0; color: var(--text3); line-height: 1.8; }
.disp-math { text-align: center; font-family: var(--font-serif); font-size: 1.1rem; color: var(--text3); padding: .75rem 0; }

/* Pipeline grid */
.pipe-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: .75rem; margin: 1.25rem 0;
}
.pipe-card { border-radius: var(--radius); padding: 1rem; background: var(--bg2); border: 1px solid var(--border); }
.pipe-c1 { border-top: 3px solid var(--accent); }
.pipe-c2 { border-top: 3px solid var(--accent2); }
.pipe-c3 { border-top: 3px solid var(--accent5); }
.pipe-c4 { border-top: 3px solid var(--accent3); }
.pipe-c5 { border-top: 3px solid var(--accent4); }
.pipe-num { font-size: .7rem; color: var(--text2); margin-bottom: .2rem; }
.pipe-name { font-size: .9rem; font-weight: 600; color: var(--text); margin-bottom: .5rem; }
.pipe-items { padding-left: 1rem; font-size: .82rem; color: var(--text3); }
.pipe-items li { margin-bottom: .15rem; }
.pipe-body { font-size: .82rem; color: var(--text3); }
.pipe-body p { margin-bottom: .3rem; }
.pipe-body ul, .pipe-body ol { padding-left: 1rem; }
.pipe-body li { margin-bottom: .15rem; }

/* Method subsections */
.method-sub { margin: 1.75rem 0; }
.sub-title { font-size: 1.05rem; font-weight: 600; color: var(--accent2); margin-bottom: .75rem; }

/* Experiments — full-width accordion panels */
.exp-intro { margin-bottom: 1.5rem; }
.exp-panel {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  margin-bottom: .6rem;
  transition: border-color .2s;
}
.exp-panel.open { border-color: var(--border2); }
.exp-panel-hd {
  display: flex; align-items: center; gap: .75rem;
  padding: .85rem 1rem; cursor: pointer;
  background: var(--bg2); user-select: none; transition: background .15s;
}
.exp-panel-hd:hover { background: var(--bg3); }
.exp-label {
  min-width: 1.8rem; height: 1.8rem;
  display: flex; align-items: center; justify-content: center;
  border-radius: 4px; font-size: .75rem; font-weight: 700;
  flex-shrink: 0; color: var(--bg);
}
.exp-panel-title { flex: 1; font-size: .95rem; font-weight: 600; color: var(--text); }
.exp-panel-body { padding: 1.25rem 1.5rem; font-size: .9rem; }
.exp-c1 .exp-label { background: var(--accent); }
.exp-c2 .exp-label { background: var(--accent2); }
.exp-c3 .exp-label { background: var(--accent5); }
.exp-c4 .exp-label { background: var(--accent3); }
.exp-c5 .exp-label { background: var(--accent4); }
.exp-c1.open { border-color: var(--accent); }
.exp-c2.open { border-color: var(--accent2); }
.exp-c3.open { border-color: var(--accent5); }
.exp-c4.open { border-color: var(--accent3); }
.exp-c5.open { border-color: var(--accent4); }
/* keep legacy grid classes in case other pages use them */
.exp-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 1rem; }
.exp-card { border-radius: var(--radius); padding: 1.25rem; background: var(--bg2); border: 1px solid var(--border); }
.exp-title { font-size: .95rem; font-weight: 600; color: var(--text3); margin-bottom: .75rem; }
.exp-body { font-size: .88rem; color: var(--text3); }

/* Related grid */
.related-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: .75rem; }
.rel-card { padding: 1rem; background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); }
.rel-title { font-weight: 600; font-size: .9rem; margin-bottom: .4rem; }
.rel-title a { color: var(--accent); text-decoration: none; }
.rel-title a:hover { text-decoration: underline; }
.rel-desc { font-size: .85rem; color: var(--text3); }

/* Insights grid */
.ins-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: .75rem; }
.ins-card {
  padding: 1.1rem; background: var(--bg2);
  border: 1px solid var(--border2); border-radius: var(--radius);
  display: grid; grid-template-rows: auto auto 1fr; gap: .35rem;
}
.ins-num {
  width: 1.6rem; height: 1.6rem; border-radius: 50%;
  background: var(--accent5); color: var(--bg);
  display: flex; align-items: center; justify-content: center;
  font-size: .75rem; font-weight: 700;
}
.ins-title { font-weight: 600; font-size: .9rem; color: var(--text); }
.ins-desc { font-size: .85rem; color: var(--text3); }
.ins-wide { padding: 1rem; background: var(--bg2); border-radius: var(--radius); }

/* Abstract */
.abstract-block {
  padding: 1.25rem 1.5rem; background: var(--bg2);
  border-radius: var(--radius); border-left: 3px solid var(--accent);
  font-size: .95rem; color: var(--text3);
}

/* Tables */
.tbl-wrap { overflow-x: auto; margin: 1rem 0; }
.wiki-tbl { width: 100%; border-collapse: collapse; font-size: .85rem; }
.wiki-tbl th { background: var(--bg3); color: var(--text); padding: .5rem .75rem; text-align: left; border-bottom: 2px solid var(--border2); white-space: nowrap; }
.wiki-tbl td { padding: .45rem .75rem; border-bottom: 1px solid var(--border); color: var(--text3); vertical-align: top; }
.wiki-tbl tr:hover td { background: var(--bg3); }

/* Figures */
.wiki-fig { margin: 1.25rem 0; text-align: center; }
.wiki-fig img { max-width: 100%; border-radius: 8px; border: 1px solid var(--border); }
.inline-img { max-height: 1.5em; vertical-align: middle; border-radius: 2px; }

/* Prose */
p { margin-bottom: .75rem; text-wrap: pretty; color: var(--text3); }
p:last-child { margin-bottom: 0; }
ul, ol { padding-left: 1.4rem; margin-bottom: .75rem; color: var(--text3); }
li { margin-bottom: .2rem; }
blockquote { border-left: 3px solid var(--border2); padding: .6rem 1rem; color: var(--text2); margin: .75rem 0; background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; }
pre { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 1rem; overflow-x: auto; margin: .75rem 0; }
code { font-family: var(--font-mono); font-size: .85em; color: var(--accent2); }
pre code { color: var(--text3); font-size: .82rem; }
strong { color: var(--text); }
em { font-style: italic; }
a { color: var(--accent); }
.wikilink { color: var(--accent4); cursor: pointer; }
hr { border: none; border-top: 1px solid var(--border); margin: 1.5rem 0; }
h4 { font-size: .95rem; font-weight: 600; color: var(--text); margin: 1rem 0 .5rem; }
h5, h6 { font-size: .9rem; font-weight: 600; color: var(--text2); margin: .75rem 0 .4rem; }

/* Scroll-top */
#scroll-top {
  position: fixed; bottom: 1.5rem; right: 1.5rem;
  width: 40px; height: 40px; border-radius: 50%;
  background: var(--bg3); border: 1px solid var(--border2);
  color: var(--text2);
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; font-size: 1.1rem;
  opacity: 0; transition: opacity .2s;
  text-decoration: none;
}
#scroll-top.visible { opacity: 1; }
#scroll-top:hover { background: var(--bg4); color: var(--text); }

/* Responsive */
@media (max-width: 720px) {
  .hero { padding: 5rem 1rem 2.5rem; }
  .section { padding: 2rem 1rem; }
  .exp-grid { grid-template-columns: 1fr; }
  .arch-flow { grid-template-columns: 1fr; }
}
"""

JS = """
function tog(hd) { hd.parentElement.classList.toggle('open'); }
const scrollBtn = document.getElementById('scroll-top');
window.addEventListener('scroll', () => {
  scrollBtn.classList.toggle('visible', window.scrollY > 400);
}, { passive: true });
"""

# ─── Main HTML assembly ────────────────────────────────────────────────────────

def build_html(title, nav_html, body_sections_html):
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
{nav_html}
{body_sections_html}
<a id="scroll-top" href="#top" aria-label="Back to top">↑</a>
<script>{JS}</script>
</body>
</html>"""

# ─── Section type map ─────────────────────────────────────────────────────────

KNOWN_SECTIONS = {
    '核心观点': 'accordion',
    '方法摘要': 'method',
    '实验与结果': 'experiments',
    '与其他论文的关联': 'related',
    '摘要': 'abstract',
    '基本信息': 'skip',
    '相关页面': 'skip',
    '引用关系': 'skip',
    '启示与关联': 'skip',
    '对X项目的启示': 'skip',
    '目录': 'skip',        # wiki auto-generated TOC — skip in HTML output
}

def section_type(heading):
    if heading in KNOWN_SECTIONS:
        return KNOWN_SECTIONS[heading]
    if re.search(r'项目的启示|对.*的启示', heading):
        return 'skip'
    return 'generic'


# ─── Main converter ────────────────────────────────────────────────────────────

def convert(md_path, output_path=None, title_override=None):
    md_path = Path(md_path).resolve()
    source_dir = str(md_path.parent)

    # Build image search path: walk up 4 levels to catch sibling dirs (figures/, etc.)
    extra_dirs = []
    p = md_path.parent
    for _ in range(4):
        extra_dirs.append(str(p))
        p = p.parent

    text = md_path.read_text(encoding='utf-8')
    meta, body = parse_frontmatter(text)

    h1_m = re.match(r'^#\s+(.+)$', body, re.MULTILINE)
    raw_title = h1_m.group(1).strip() if h1_m else md_path.stem
    title = title_override or re.sub(r'\s*🌱\s*$', '', raw_title).strip()
    if h1_m:
        body = body[h1_m.end():].lstrip('\n')

    # Extract first lead blockquote
    lead_quote = ''
    bq_m = re.match(r'^>\s*(.+)$', body, re.MULTILINE)
    if bq_m and bq_m.start() < 400:
        lead_quote = bq_m.group(1).strip()
        body = body[:bq_m.start()] + body[bq_m.end():].lstrip('\n')

    intro, sections = split_h2_sections(body)

    nav_html = build_nav(sections)
    hero_html = render_hero(meta, title, lead_quote, source_dir)

    body_parts = [hero_html]

    if intro.strip():
        intro_html = render_blocks(intro.strip(), source_dir, extra_dirs)
        if intro_html.strip():
            body_parts.append(f'<section class="section"><div class="sec-inner">{intro_html}</div></section>')

    for heading, content in sections:
        stype = section_type(heading)
        sid = _slugify(heading)

        if stype == 'skip':
            continue
        elif stype == 'accordion':
            body_parts.append(render_accordion(content, source_dir, extra_dirs))
        elif stype == 'method':
            body_parts.append(render_method(content, source_dir, extra_dirs))
        elif stype == 'experiments':
            body_parts.append(render_experiments(content, source_dir, extra_dirs))
        elif stype == 'related':
            body_parts.append(render_related(content, source_dir, extra_dirs))
        elif stype == 'insights':
            body_parts.append(render_insights(content, heading, source_dir, extra_dirs))
        elif stype == 'abstract':
            body_parts.append(render_abstract(content, source_dir, extra_dirs))
        else:
            body_parts.append(render_generic_section(heading, content, sid, source_dir, extra_dirs))

    full_html = build_html(title, nav_html, '\n'.join(body_parts))

    if output_path is None:
        # Resolve wiki root to place exports folder
        curr = md_path.parent
        wiki_root = None
        for _ in range(5):
            if (curr / "entries.json").exists() or (curr / "index.md").exists():
                wiki_root = curr
                break
            curr = curr.parent
        if wiki_root is None:
            wiki_root = md_path.parent.parent.parent

        slug = md_path.stem
        article_export_dir = wiki_root / 'exports' / slug
        article_export_dir.mkdir(parents=True, exist_ok=True)
        output_path = article_export_dir / f'{slug}.html'
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(full_html, encoding='utf-8')
    return output_path

# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Convert wiki markdown source page to HTML')
    parser.add_argument('source', help='Path to .md source file')
    parser.add_argument('--output', '-o', help='Output HTML path')
    parser.add_argument('--title', help='Override page title')
    args = parser.parse_args()
    out = convert(args.source, args.output, title_override=args.title)
    size_kb = out.stat().st_size / 1024
    print(f'✓ {out}  ({size_kb:.0f} KB)')

if __name__ == '__main__':
    main()
