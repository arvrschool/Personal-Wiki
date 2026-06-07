# Ingest / Query / Lint

This reference covers adding specific entries to an existing knowledge base (Workflows F-G) and maintaining wiki health.

## Workflow F: Wiki Maintenance — Ingest / Query / Lint

### ① Ingest — add a new paper/article/bookmark to an existing wiki

**IMPORTANT: After every ingest operation, complete the mandatory INGEST CHECKLIST below.**

#### Automated steps

```bash
cd <wiki-parent-dir>

# Step 1: download PDF + metadata
python3 ./scripts/download_paper.py --arxiv-id <id> --output-dir pdfs --with-project
# Then append the _info.json content to entries.json

# Step 2: LLM assigns category + concepts
python3 ./scripts/enrich_wiki.py --wiki-dir wiki --entries entries.json --classify

# Step 3: rebuild skeleton (creates source page + updates entity/topic/index)
python3 ./scripts/build_paper_wiki.py --entries entries.json --wiki-dir wiki --topic "<Topic>"

# Step 4: enrich the new source page
python3 ./scripts/enrich_wiki.py --wiki-dir wiki --entries entries.json --only-sources \
  --page-slug <slug> --llm-provider auto --figures --figures-dir wiki/figures \
  --media --media-dir wiki/media --web-resources

# Step 5: fix backlinks
python3 ./scripts/enrich_wiki.py --wiki-dir wiki --entries entries.json --fix-backlinks

# Step 6: Obsidian format optimization
# ACTIVATE SKILL: activate_skill(name="obsidian-markdown")
# Follow its instructions to format the newly enriched page at wiki/wiki/sources/<slug>.md
```

---
#### ⚠️ MANDATORY INGEST CHECKLIST

The Agent **must** verify every item below. Do not mark the ingest as complete until all items are checked.

```
□ 1. entries.json 是否已更新？
       检查新增条目的 title / arxiv_id / authors / category 是否正确

□ 2. PDF 是否已下载？
       ls pdfs/<arxiv_id>.pdf
       → 如果失败，重试：python3 scripts/download_paper.py --arxiv-id <id> --output-dir pdfs --with-project
       → 非 arXiv 论文：在 entries.json 中设置 pdf_url 后重试

□ 3. build_paper_wiki.py 是否已运行？
       即使源页面已手动创建，也务必运行此脚本 (创建实体页, 更新 index.md)

□ 4. enrich_wiki.py 是否已触发？
       即使 LLM provider 不可用，也务必运行一次
       → 如果 fallback 到 direct-inference 模式，进入下方【Manual enrich checklist】
       → ⚠️  如果 enrich 已完成，必须检查插图是否也已提取并嵌入

□ 5. 格式优化 (obsidian-markdown) 是否已执行？
       → 必须激活并使用 vendor/obsidian-markdown 技能，对生成的 markdown 页面进行 Obsidian 兼容性格式化。

□ 6. index.md 的概览计数是否准确？

□ 7. log.md 是否已追加操作记录？
       格式：## YYYY-MM-DD ingest | <title>

□ 8. git 是否已提交？
       cd <wiki-root> && git add -A && git commit -m "auto-wiki-archive: [Ingest] <title>"
```

#### Manual enrich checklist (when LLM CLI is unavailable)

When `enrich_wiki.py` falls back to `direct-inference` mode (outputs `.prompt_*.md` files), the Agent **must** execute the full enrich manually. See `workflow-figures.md` for full manual enrichment steps.

---

### ② Query — answer a question from accumulated knowledge

**Approach:**
1. Read `wiki/index.md` to identify relevant source and entity pages.
2. Read those pages in full.
3. Synthesize: answer the question, note contradictions, cite pages with `[[wikilink]]`.
4. If the answer is worth keeping, write it as a new page and add to `index.md`.

---

### ③ Lint — health check on wiki consistency

```bash
python3 ./scripts/lint_wiki.py --wiki-dir wiki --semantic
grep -rl "待补充" wiki/wiki/sources/ | wc -l
```

**Lint findings → actions:**
- `stub`: Run `enrich_wiki.py --only-sources` or manual enrich.
- `orphan`: Link from a relevant entity or topic page.
- `broken-link`: Fix wikilink or create missing page.
- `empty-association`: Read the paper and write `与其他论文的关联`.
- `contradiction`: Read both pages; update whichever is outdated.
- `missing-page`: Create entity page and link from `index.md`.

---

## Workflow G: Ingest Web Content into Wiki

Use `ingest_article.py` to extract mentioned papers from a web URL.

### Supported platforms
- `mp.weixin.qq.com`, `zhihu.com`, `xiaohongshu.com` (dokobot)
- `x.com` / `twitter.com` (baoyu → dokobot)
- `youtube.com` (yt-dlp)
- `github.com/...` (GitHub API)
- other URLs (dokobot)

### Usage

```bash
# Full pipeline
python ./scripts/ingest_article.py \
  --url "https://..." \
  --wiki-dir /path/to/wiki \
  --entries /path/to/entries.json \
  --llm-provider auto

# Use local file (skip dokobot)
python ./scripts/ingest_article.py --no-fetch --input /tmp/article.md --wiki-dir wiki --entries entries.json
```