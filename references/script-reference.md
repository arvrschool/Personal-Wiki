# Script Reference & Architecture Rules

This reference covers naming conventions, known design decisions, and script CLI parameters.

## Slug Naming Convention

- **arXiv paper**: `<title-slug>-<arxiv_id>` (e.g. `featurising-pixels-from-dynamic-3d-scenes-with-linear-in-context-learners-2604.26488`)
- **No-arXiv paper**: `<title-slug>` (e.g. `pi07-a-steerable-model-with-emergent-capabilities`)
- **Article**: `<platform>-<full-title>` (e.g. `weixin-机器人的通用大脑应该是怎么样的？MotuBrain给了一份参考答案`)

Generate slug:
```python
import re
slug = re.sub(r'[^a-z0-9\s-]', '', title.lower())
slug = re.sub(r'[\s]+', '-', slug.strip()).strip('-')
```

## Template System

The structure of generated pages is template-driven. Templates are located in `templates/<template_id>/` and consist of a `manifest.toml` and a `page.md`.
- **Default Templates**: `research_paper`, `web_article`, `research_topic`, `generic`.
- **Editing**: You can safely edit these templates directly to change the default structure of generated wiki pages.
- **Ingestion**: When using ingest scripts, the template can be forced via `--template <id>`, though by default (`auto`) the system selects `research_paper`, `web_article`, or `generic` based on the content type.

## Known Issues & Design Decisions

### Wikilink resolution
Obsidian resolves `[[Target]]` by matching the exact filename stem. Ensure consistency:
- Papers: `<title-slug>-<arxiv_id>.md`
- Articles: `<platform>-<full-title>.md`

### Broken links from LLM-generated wikilinks
When `enrich_wiki.py` asks the LLM to write `与其他论文的关联`, it provides a full list of all papers with their exact `[[slug]]`. If broken links appear after enrichment, re-run with `--force`.

### category, concepts, topics belong in data, not in code
- Paper `category` + `concepts` → stored in `entries.json`, assigned by LLM via `--classify`
- Available topics → read at runtime from `wiki/wiki/topics/*.md` filenames
- Add a new topic: create `.md` in `wiki/wiki/topics/`, then run `--classify --force`

### Enriched content protection
Files with enriched content (`（待补充）` not present AND `len(text) > 2000`) are **never** overwritten, even with `--rebuild`. Only stubs can be overwritten.

### index.md Surgical Updates
`build_paper_wiki.py` updates `index.md` surgically, replacing only the automatically managed sections (Overview, Entities, Topics, Sources) while preserving manual sections like `## 文章` or custom notes.

### Directory Awareness (sources/ vs articles/)
The wiki build process distinguishes between research papers (in `wiki/sources/`) and web articles (in `wiki/articles/`). `build_paper_wiki.py` is aware of both and will update existing entries in either folder without creating duplicates.

### PDF text cache
`download_paper.py` saves extracted text as `<arxiv_id>.txt` alongside the PDF. When answering questions, always read the `.txt` cache (not the raw PDF) to save tokens.
```bash
grep -n "keyword" pdfs/<arxiv_id>.txt
sed -n '<start>,<end>p' pdfs/<arxiv_id>.txt
```

### TOC auto-maintenance
Every source page has a `## 目录` section managed by `toc_utils.py`. You never need to write or edit it manually.

### Wiki → HTML Export
```bash
python ./scripts/wiki_to_html.py <wiki_root>/wiki/wiki/sources/<page>.md
```
Produces self-contained dark-theme HTML with all images base64-inlined.

---

## Script Reference

| Script | Purpose | Key args |
|--------|---------|----------|
| `search_papers_web.py` | Search papers via ArXiv + Google Scholar | `query`, `--authors`, `--limit`, `--json-out` |
| `search_web_resources.py` | Find articles/videos/GitHub | `topic`, `--max-articles`, `--max-videos` |
| `download_paper.py` | Download PDF + metadata | `--arxiv-id`, `--output-dir`, `--with-project` |
| `build_paper_wiki.py` | Build Obsidian wiki from entries.json | `--entries`, `--wiki-dir`, `--topic`, `--rebuild` |
| `enrich_wiki.py` | Fill stub sections with LLM + PDFs | `--wiki-dir`, `--entries`, `--pdf-dir`, `--llm-provider`, `--figures`, `--media`, `--force` |
| `lint_wiki.py` | Health-check wiki | `--wiki-dir`, `--semantic` |
| `ingest_article.py` | Ingest web articles into wiki | `--url`, `--wiki-dir`, `--entries`, `--no-fetch` |
| `wiki_to_html.py` | Convert wiki page to HTML | `<source.md>`, `--output` |