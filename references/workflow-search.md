# Research Search & Knowledge Base Build

This reference covers the initial creation and exploration of a knowledge base (Workflows A-E), including output directory structures.

## Output Directory Convention

Every search creates a dedicated folder named `<topic>-<YYYYMMDD>` under the current working directory, or a user-specified output directory. Structure:

```
<outdir>/<topic>-<YYYYMMDD>/
├── entries.json       ← structured source entries (machine-readable)
├── papers.md          ← research search summary table (optional)
├── web_resources.md   ← blog posts, YouTube videos, GitHub repos
├── restore_articles.py ← utility to restore index articles after rebuild
├── templates/         ← editable structural definitions for pages
├── pdfs/
│   ├── <id>_info.json ← paper metadata
│   └── <id>.pdf       ← PDF (if download succeeded)
└── wiki/              ← llm-wiki knowledge base (Obsidian-compatible)
    ├── index.md           ← global navigation
    ├── log.md             ← operation history
    └── wiki/
        ├── sources/       ← one page per source entry
        ├── articles/      ← one page per fetched article
        ├── entities/      ← concept pages
        └── topics/        ← topic/category pages
```

Create this folder at the start and write all outputs into it directly — never use `/tmp` as the final destination.

---

## Workflow A: Research Search + Wiki Build

Use this when the user wants a literature landscape or seed-paper survey. For plain URLs, fetched articles, bookmarks, repos, or mixed non-paper sources, skip this workflow and use Workflow F/G in `workflow-ingest.md`.

### Step 0 — Domain expansion (do this before running any script)

The user gives a short keyword. Before calling the search script, use your own training knowledge to expand it into three dimensions:
1. **Key authors** — who are the primary researchers in this area?
2. **Related concepts** — what adjacent topics would a survey paper also cover?
3. **Specific paper names** — are there important papers whose titles don't contain the main keyword?

Then pass these to the script via `--authors`, `--related`, `--extra`. The script will run all dimensions in parallel. Do NOT ask the user to supply these.

4. **Non-searchable foundational papers** — some landmark papers live outside arXiv/OpenReview. Manually inject them into `entries.json` with `"is_seed": true` and the correct `"url"`. Before assuming a paper has no arXiv ID, verify via arXiv title search. For OpenReview papers: set `pdf_url: "https://openreview.net/pdf?id=<forum_id>"`.

### Step 1 — Find papers via arXiv XML API + Google Scholar

```bash
python ./scripts/search_papers_web.py \
  "<topic keyword>" \
  --authors "Author1" "Author2" \
  --related "concept1 concept2" \
  --extra "PaperName1" "PaperName2" \
  --limit 40 --json-out <outdir>/papers.json
```

### Step 2 — Fetch web resources (articles, videos, GitHub)

```bash
python ./scripts/search_web_resources.py \
  "<topic> <key authors>" \
  --max-articles 8 --max-videos 8 --max-github 5
```
Save the output to `<outdir>/web_resources.md`.

### Step 3 — Download PDFs for seed papers

```bash
python ./scripts/download_paper.py \
  --arxiv-id <id> --output-dir <outdir>/pdfs --with-project
```

### Step 4 — Build llm-wiki knowledge base

```bash
python ./scripts/build_paper_wiki.py \
  --entries <outdir>/entries.json \
  --wiki-dir <outdir>/wiki \
  --topic "<Topic>"
```
*Note: `--rebuild` overwrites all pages. `--only-seeds` writes source pages only for seed papers.*

### Step 5 — Enrich wiki stubs with LLM

```bash
# RECOMMENDED: full enrichment with figures + media + web resources
python ./scripts/enrich_wiki.py \
  --wiki-dir <outdir>/wiki --entries <outdir>/entries.json --pdf-dir <outdir>/pdfs \
  --llm-provider auto --only-sources --figures --media --web-resources
```
**When `--figures` cannot run (e.g., no LLM CLI) → Agent MUST manually extract and insert figures per `workflow-figures.md`. This is NOT skippable.**

**Post-Enrichment Formatting (Mandatory):**
After enrichment, you MUST activate the `obsidian-markdown` vendor skill (`activate_skill(name="obsidian-markdown")`) and use it to format all newly enriched source pages to ensure Obsidian compatibility.

### Step 6 — Lint wiki health check

```bash
python ./scripts/lint_wiki.py --wiki-dir <outdir>/wiki --semantic
```

---

## Workflow B: Find Blog Posts / Videos / GitHub Projects

```bash
python ./scripts/search_web_resources.py \
  "<topic keyword>" \
  --max-articles 5 --max-videos 5 --max-github 3
```

---

## Workflow C: Download Paper PDF + Project Docs

```bash
python ./scripts/download_paper.py --arxiv-id <id> --output-dir ./papers [--with-project]
```

---

## Workflow D: Citation Trace

```bash
# Online (if Semantic Scholar API is reachable):
python ./vendor/paper-navigator/scripts/citation_traverse.py \
  --paper-id ArXiv:<id> --direction forward --limit 20
# Offline: use training knowledge + manual search
```

---

## Workflow E: Author / Institution Search

```bash
python ./vendor/paper-navigator/scripts/author_search.py \
  --name "Author Name" --papers --limit 20 --sort-by citations
```