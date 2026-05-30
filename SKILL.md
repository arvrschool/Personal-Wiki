---
name: llm-wiki
description: >
  Universal LLM-powered personal knowledge-base workflow for papers, articles, bookmarks, repos.
  Supports ingest, enrich, query, lint, and wiki-to-HTML export.
  Designed to work across different LLM models (Claude, Gemini, local models) and multiple OS (macOS, Linux).
  Use this skill whenever the user wants a structured, maintainable knowledge base.
  Trigger phrases: "add this URL to the wiki", "ingest this article/paper/bookmark",
  "search papers on X", "export wiki page to HTML", "convert markdown to HTML".
---

# LLM-Wiki Maintenance

A universal wiki ingestion & maintenance skill. It can ingest arXiv/OpenReview papers, local PDFs,
web articles, bookmarks, GitHub repos, or plain URLs — and organize them into an Obsidian-compatible
knowledge base with structured source pages, entity pages, and topic pages.

**Core philosophy:** scripts handle automatable tasks (fetching, classification, figure download);
the Agent (you) handles everything that requires reasoning — especially filling in template stubs
when no LLM CLI is available.

**Skill scripts:** `./scripts/`
**Structured store:** `entries.json` (preferred, backward-compatible with legacy `papers.json`)

---

## Vendor Skill Priority

The `vendor/` directory contains specialized, expert-level skills (e.g., from `baoyu-skills`).
**Mandatory Rule:** When a task can be performed by a skill in `vendor/`, ALWAYS prioritize
activating and using the vendor skill over generic scripts or manual workflows.

**Activation Procedure:**
1. Check `./vendor/<skill-name>/SKILL.md`.
2. Activate the skill using `activate_skill(name="<skill-name>")`.
3. Follow the expert guidance provided by the activated skill.

---

## Environment Setup

Before performing any ingestion or enrichment tasks for the first time, ensure all Python
dependencies are installed.

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
```

*Note: Some dependencies like `dokobot` may require custom installation. If a fetching tool
is missing, prioritize using a vendor skill (Workflow G) for data retrieval.*

---

## Output Directory Convention

Every search creates a dedicated folder named `<topic>-<YYYYMMDD>` under the current working directory,
or a user-specified output directory. Structure:

```
<outdir>/<topic>-<YYYYMMDD>/
├── entries.json       ← structured source entries (machine-readable)
├── papers.md          ← research search summary table (optional)
├── web_resources.md   ← blog posts, YouTube videos, GitHub repos
├── restore_articles.py ← utility to restore index articles after rebuild
├── templates/         ← editable structural definitions for pages
│   ├── research_paper/
│   ├── web_article/
│   └── generic/
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

Create this folder at the start and write all outputs into it directly — never use `/tmp` as
the final destination.

---

## Workflow A: Research Search + Wiki Build

Use this when the user wants a literature landscape or seed-paper survey.
For plain URLs, fetched articles, bookmarks, repos, or mixed non-paper sources, skip this workflow
and use the direct ingest flows later in this file.

### Step 0 — Domain expansion (do this before running any script)

The user gives a short keyword. Before calling the search script, use your own training knowledge
to expand it into three dimensions:

1. **Key authors** — who are the primary researchers in this area?
2. **Related concepts** — what adjacent topics would a survey paper also cover?
3. **Specific paper names** — are there important papers whose titles don't contain the main keyword?

Then pass these to the script via `--authors`, `--related`, `--extra`. The script will run all
dimensions in parallel and rank by cross-source overlap + citations + recency.

Do NOT ask the user to supply these — they gave you a keyword, you fill in the rest.

4. **Non-searchable foundational papers** — some landmark papers live outside arXiv and OpenReview.
   Use your training knowledge to identify these and manually inject them into `entries.json`
   with `"is_seed": true` and the correct `"url"`.

   **Before assuming a paper has no arXiv ID**, always verify via arXiv title search:
   `https://arxiv.org/search/?searchtype=all&query=<title+keywords>&start=0`
   Only set `arxiv_id: ""` if arXiv search confirms it's not there.

   For OpenReview papers: set `pdf_url: "https://openreview.net/pdf?id=<forum_id>"`.

### Step 1 — Find papers via arXiv XML API + Google Scholar

```bash
python ./scripts/search_papers_web.py \
  "<topic keyword>" \
  --authors "Author1" "Author2" \
  --related "concept1 concept2" \
  --extra "PaperName1" "PaperName2" \
  --limit 40 --json-out <outdir>/papers.json
```

- **Primary source: arXiv XML API** — reliable, no captcha, returns 20–50+ papers per query.
- `--authors` — searches `au:` field.
- `--related` — concept expansion queries.
- `--extra` — specific paper names/acronyms.
- **Scholar enrichment** — merges citation counts from Google Scholar into arXiv results.

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

This produces:
```
<outdir>/wiki/
├── index.md              ← global navigation
├── log.md                ← operation history
└── wiki/
    ├── sources/          ← one .md page per source entry
    ├── entities/         ← concept pages
    └── topics/           ← topic/category pages
```

**Key design principles:**
- **Source pages** = the "compiled" form of each source entry: metadata + summary + concept links
- **Entity pages** = living pages for concepts that accumulate observations from multiple papers
- **Topic pages** = cross-entry synthesis tables
- **`[[WikiLink]]` syntax** = Obsidian double-bracket links connect everything
- **`--rebuild` flag** = overwrite all pages (use when entries.json changes significantly)
- **`--only-seeds` flag** = write source pages only for seed papers

### Step 5 — Enrich wiki stubs with LLM

```bash
python ./scripts/enrich_wiki.py \
  --wiki-dir <outdir>/wiki --entries <outdir>/entries.json --pdf-dir <outdir>/pdfs \
  --llm-provider <provider> --only-sources

# With figures + media + web resources
python ./scripts/enrich_wiki.py \
  --wiki-dir <outdir>/wiki --entries <outdir>/entries.json --pdf-dir <outdir>/pdfs \
  --llm-provider <provider> --only-sources --figures --media --web-resources
```

**Key behaviours:**
- Pages with already-filled sections are skipped automatically.
- `--figures` and `--media` run independently of text enrichment.
- Source pages are matched to papers by arXiv ID or title slug.
- `--force`: rewrites all sections including figures/media/web-resources.
- If a paper has no PDF and no abstract, the LLM generates from its training knowledge.

#### LLM provider resolution

`--llm-provider auto` (default): checks for API keys in order — openai (DeepSeek/Qwen/Kimi etc.) → anthropic → ollama.
If no API key is configured, falls back to `direct-inference` mode, which writes `.prompt_<page>.md` files
for the host Agent (Claude Code, Gemini CLI, Codex, etc.) to process directly.

**When no API key is available, the Agent must follow the manual enrich checklist in Workflow F.**

### Step 6 — Lint wiki health check

```bash
# Structural lint only (fast, no LLM)
python ./scripts/lint_wiki.py --wiki-dir <outdir>/wiki

# With semantic analysis (LLM, slower)
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

---

## Core Protocol: Data Integrity & Git Archiving

All modifications to wiki documents must follow the **Git-on-Change** protocol:

1. **Atomic Commits**: Every file modification must be followed by a Git commit.
2. **Commit Message Format**:
   - Single updates: `auto-wiki-archive: [Update] <filename> - <summary>`
   - Batch operations: `auto-wiki-archive: [Batch] <operation> - <count> pages`
3. **Rollback Strategy**: Use `git restore` or `git revert` to revert bad changes.
4. **No Unstaged Changes**: Commit early, commit often.

### Repository setup

Check if git is initialized after any significant batch operation. If not, set it up:

```bash
cd <wiki-root>
git status 2>/dev/null || {
  git init && git add -A && git commit -m "init: initial wiki state"
}
```

**If git is missing:** try `apt-get install git` (Linux), `conda install git`, or `brew install git` (macOS).
Do not skip git setup silently.

---

## Workflow F: Wiki Maintenance — Ingest / Query / Lint

### ① Ingest — add a new paper/article/bookmark to an existing wiki

**IMPORTANT: After every ingest operation, complete the mandatory INGEST CHECKLIST below.**
**Do not consider the ingest done until all items are checked.**

#### Automated steps

```bash
cd <wiki-parent-dir>

# Step 1: download PDF + metadata
python3 ./scripts/download_paper.py \
  --arxiv-id <id> --output-dir pdfs --with-project
# Then append the _info.json content to entries.json

# Step 2: LLM assigns category + concepts
python3 ./scripts/enrich_wiki.py \
  --wiki-dir wiki --entries entries.json --classify

# Step 3: rebuild skeleton (creates source page + updates entity/topic/index)
python3 ./scripts/build_paper_wiki.py \
  --entries entries.json --wiki-dir wiki --topic "<Topic>"

# Step 4: enrich the new source page
python3 ./scripts/enrich_wiki.py \
  --wiki-dir wiki --entries entries.json --only-sources \
  --page-slug <slug> \
  --llm-provider <provider> \
  --figures --figures-dir wiki/figures \
  --media --media-dir wiki/media \
  --web-resources

# Step 5: fix backlinks
python3 ./scripts/enrich_wiki.py \
  --wiki-dir wiki --entries entries.json --fix-backlinks
```

---
#### ⚠️ MANDATORY INGEST CHECKLIST

After running all the commands above, the Agent **must** verify every item below.
Do not mark the ingest as complete until all items are checked.

```
□ 1. entries.json 是否已更新？
       检查新增条目的 title / arxiv_id / authors / category 是否正确

□ 2. PDF 是否已下载？
       ls pdfs/<arxiv_id>.pdf
       → 如果失败，重试：python3 scripts/download_paper.py --arxiv-id <id> --output-dir pdfs --with-project
       → 非 arXiv 论文：在 entries.json 中设置 pdf_url 后重试

□ 3. build_paper_wiki.py 是否已运行？
       即使源页面已手动创建，也务必运行此脚本
       → 它会自动为新概念创建实体页
       → 它会更新 index.md 的实体/主题列表
       → 不运行会导致实体页缺失

□ 4. enrich_wiki.py 是否已触发？
       即使 LLM provider 不可用，也务必运行一次
       → 脚本会尝试 LLM enrich
       → 如果 fallback 到 direct-inference 模式，会产生 .prompt_*.md 文件
       → 进入下方【Manual enrich checklist】

□ 5. index.md 的概览计数是否准确？
       条目总数、研究论文数、种子条目数等应与 entries.json 一致

□ 6. log.md 是否已追加操作记录？
       格式：## YYYY-MM-DD ingest | <title>

□ 7. git 是否已提交？
       cd <wiki-root> && git add -A && git commit -m "auto-wiki-archive: [Ingest] <title>"
```

#### Manual enrich checklist (when LLM CLI is unavailable)

When `enrich_wiki.py` falls back to `direct-inference` mode (outputs `.prompt_*.md` files),
the Agent **must** execute the full enrich manually. Do NOT skip this step.

```
□ 1. 读取 prompt 文件
       cat wiki/wiki/sources/.prompt_<slug>.md
       了解需要填充哪些字段（核心观点、方法摘要、具体方法、实验与结果等）

□ 2. 从 PDF 中提取信息
       Method A — pdftotext（如已安装）：
         pdftotext pdfs/<id>.pdf pdfs/<id>.txt
         grep -n "key phrase" pdfs/<id>.txt

       Method B — pypdf（通用 Python 回退，无系统依赖）：
         pip install --user --break-system-packages pypdf
         然后编写 Python 脚本来提取各页文本

       Method C — 直接读取 PDF 元数据 + 项目主页：
         如果 PDF 解析都不可用，从项目主页、arXiv 页面提取信息

□ 3. 将提取的信息写回源页面
       根据 prompt 的结构要求，逐项填充：
       - 核心观点（4-5条：核心贡献、关键设计、实验结果、与已知工作关系、局限）
       - 方法摘要（3-5句：架构组成、训练目标/损失函数）
       - 具体方法（整体架构 → 核心模块[伪代码] → 关键公式[LaTeX] → 训练推理流程）
       - 实验与结果（具体数据集名称 + 指标数字 + 对比方法 + 差距）
       - 关键概念（列出 [[WikiLink]]）
       - 与其他论文的关联（如已知可判断的关系）
       - 引用关系（本文引用 + 被引）

□ 4. 删除 prompt 文件
       rm wiki/wiki/sources/.prompt_<slug>.md

□ 5. 验证源页面不再包含"（待补充）"或"（待消化）"占位符
       grep "待补充\|待消化" wiki/wiki/sources/<slug>.md && echo "STILL HAS STUBS"

□ 6. 更新 index.md 计数（如手动操作改变了条目数量）
□ 7. git add + git commit
```

### ② Query — answer a question from accumulated knowledge

**Approach:**
1. Read `wiki/index.md` to identify relevant source and entity pages
2. Read those pages in full
3. Synthesize: answer the question, note contradictions, cite pages with `[[wikilink]]`
4. If the answer is worth keeping, write it as a new page and add to `index.md`

### ③ Lint — health check on wiki consistency

```bash
python3 ./scripts/lint_wiki.py --wiki-dir wiki
python3 ./scripts/lint_wiki.py --wiki-dir wiki --semantic
grep -rl "待补充" wiki/wiki/sources/ | wc -l
```

**Lint findings → actions:**

| Finding | Action |
|---------|--------|
| `stub` | Run `enrich_wiki.py --only-sources` or manual enrich |
| `orphan` | Link from a relevant entity or topic page |
| `broken-link` | Fix wikilink or create missing page |
| `empty-association` | Read the paper and write `与其他论文的关联` |
| `contradiction` | Read both pages; update whichever is outdated |
| `missing-page` | Create entity page and link from `index.md` |

---

## Workflow G: Ingest Web Content into Wiki

Use `ingest_article.py` to extract mentioned papers from a web URL.

### Supported platforms

| Platform | URL pattern | Fetch method | Dependency |
|----------|-------------|--------------|------------|
| 微信公众号 | `mp.weixin.qq.com` | dokobot | dokobot |
| 知乎 | `zhihu.com` | dokobot | dokobot |
| 小红书 | `xiaohongshu.com` | dokobot + carousel images | dokobot |
| X/Twitter | `x.com` / `twitter.com` | baoyu → dokobot fallback | bun (optional) |
| YouTube | `youtube.com` | yt-dlp (metadata + subtitles) | yt-dlp |
| GitHub Repo | `github.com/<owner>/<repo>` | GitHub API + raw README | none |
| GitHub Issue/PR | `.../issues/N` `.../pull/N` | GitHub Issues API | none |
| 普通网页 | any other URL | dokobot | dokobot |

### Dependency installation

```bash
# dokobot
pip install dokobot  # or: npm install -g dokobot

# yt-dlp
pip install yt-dlp   # or: brew install yt-dlp
```

**If script fails due to missing Python dependency:**
```bash
pip install --user --break-system-packages <pkg>   # macOS/Linux with PEP 668
pip install --user <pkg>                            # standard user install
pip install <pkg>                                   # conda env
```

### Usage

```bash
# Full pipeline
python ./scripts/ingest_article.py \
  --url "https://..." \
  --wiki-dir /path/to/wiki \
  --entries /path/to/entries.json \
  --llm-provider <provider>

# Dry-run preview
python ... --dry-run

# Use local file (skip dokobot)
python ... --no-fetch --input /tmp/article.md
```

---

## Workflow H: Agent-as-Engine Enrichment (Complete Fallback Procedure)

When automated LLM providers are unavailable, the Agent directly performs enrichment.
This is the **complete step-by-step procedure**, not a description.

### Step-by-step

1. **Identify stubs**: Run `enrich_wiki.py --only-sources`. If LLM unavailable, it writes
   `.prompt_<slug>.md` files. Note which pages need enrichment.

2. **Read the prompt**: `cat wiki/wiki/sources/.prompt_<slug>.md`

3. **Extract PDF text** (in order of preference):
   ```bash
   # Option A: pdftotext
   pdftotext pdfs/<id>.pdf pdfs/<id>.txt && grep -n "method\|experiment\|loss" pdfs/<id>.txt

   # Option B: pypdf (universal Python fallback)
   pip install --user --break-system-packages pypdf

   # Option C: project page + arXiv abstract (if PDF parsing is impossible)
   # Use web_fetch or urllib to retrieve HTML, extract text from project page
   ```

4. **Write enriched content to source page**: Fill in all stub sections following the
   prompt's format requirements (核心观点、方法摘要、具体方法 with LaTeX formulas、
   实验与结果 with concrete metrics).

5. **Clean up**: `rm wiki/wiki/sources/.prompt_<slug>.md`

6. **Verify no stubs remain**: `grep "待补充\|待消化" wiki/wiki/sources/<slug>.md`

7. **Commit**: `git add -A && git commit -m "auto-wiki-archive: [Enrich] <slug>"`

---

## Slug Naming Convention

- arXiv paper: `<title-slug>-<arxiv_id>` (e.g. `featurising-pixels-from-dynamic-3d-scenes-with-linear-in-context-learners-2604.26488`)
- No-arXiv paper: `<title-slug>` (e.g. `pi07-a-steerable-model-with-emergent-capabilities`)
- Article: `<platform>-<full-title>` (e.g. `weixin-机器人的通用大脑应该是怎么样的？MotuBrain给了一份参考答案`)

Generate slug:
```python
import re
slug = re.sub(r'[^a-z0-9\s-]', '', title.lower())
slug = re.sub(r'[\s]+', '-', slug.strip()).strip('-')
```

---

## Known Issues & Design Decisions

### Wikilink resolution

Obsidian resolves `[[Target]]` by matching the exact filename stem. Ensure consistency:
- Papers: `<title-slug>-<arxiv_id>.md`
- Articles: `<platform>-<full-title>.md`

### Broken links from LLM-generated wikilinks

When `enrich_wiki.py` asks the LLM to write `与其他论文的关联`, it provides a full list
of all papers with their exact `[[slug]]`. The LLM is instructed to copy slugs verbatim.
If broken links appear after enrichment, re-run with `--force`.

### category, concepts, topics belong in data, not in code

- Paper `category` + `concepts` → stored in `entries.json`, assigned by LLM via `--classify`
- Available topics → read at runtime from `wiki/wiki/topics/*.md` filenames
- Add a new topic: create `.md` in `wiki/wiki/topics/`, then run `--classify --force`

### Enriched content protection

Files with enriched content (`（待补充）` not present AND `len(text) > 2000`) are **never**
overwritten, even with `--rebuild`. Only stubs can be overwritten.

### PDF text cache

`download_paper.py` saves extracted text as `<arxiv_id>.txt` alongside the PDF.
When answering questions, always read the `.txt` cache (not the raw PDF) to save tokens.

```bash
grep -n "keyword" pdfs/<arxiv_id>.txt
sed -n '<start>,<end>p' pdfs/<arxiv_id>.txt
```

### TOC auto-maintenance

Every source page has a `## 目录` section managed by `toc_utils.py`. You never need to
write or edit it manually.

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
