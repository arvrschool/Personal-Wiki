---
name: llm-wiki
description: Universal LLM-powered knowledge-base workflow. Use to ingest papers/articles, enrich stubs, extract figures, query the KB, or export to HTML. Trigger phrases: "add this URL to the wiki", "ingest this article", "search papers on X", "extract figures".
---

# LLM-Wiki Maintenance

A universal wiki ingestion & maintenance skill. It can ingest arXiv/OpenReview papers, local PDFs, web articles, bookmarks, GitHub repos, or plain URLs — and organize them into an Obsidian-compatible knowledge base with structured source pages, entity pages, and topic pages.

## Core philosophy: scripts handle automatable tasks; the Agent (you) handles everything that requires reasoning.

**Standard Workflow: Host Agent Integration**
This skill is optimized for intelligent agent environments (like Gemini CLI, Claude Code, or Codex). It follows a simplified **two-mode philosophy** for LLM reasoning:

1. **API Key Mode**: If API keys (OpenAI/Anthropic) are provided in environment variables or `config.local.toml`, scripts call the APIs directly for maximum speed.
2. **Agent-Integrated Mode (Default)**: If no keys are found, scripts use **Direct-Inference**. They automatically detect the host agent and delegate complex extraction/synthesis tasks to YOU. You will be prompted to process `.llm_prompt.txt` files and resume the script with the results.

## Vendor Skill Priority
... [existing vendor content] ...

## Core Protocol: Data Integrity & Git Archiving
All modifications to wiki documents MUST follow the **Git-on-Change** protocol:
1. **Atomic Commits**: Every file modification or batch operation must be followed by a Git commit (`auto-wiki-archive: [Ingest/Enrich] <summary>`).
2. **Index & Log Sync**: Always update `wiki/index.md` (counts/links) and `wiki/log.md` (audit trail) after ingestion.
3. **No Unstaged Changes**: Commit early, commit often.


## Core Workflows (Progressive Disclosure)

Based on the user's request, read the appropriate reference file BEFORE taking action:

- **Research Search & Knowledge Base Build** (Literature landscape, seed-paper survey, fetching web resources, author/citation trace):
  See [references/workflow-search.md](references/workflow-search.md)

- **Ingest / Query / Lint** (Adding specific papers/articles to an existing wiki, maintaining health, querying knowledge):
  See [references/workflow-ingest.md](references/workflow-ingest.md)
  *NOTE: This includes the MANDATORY INGEST CHECKLIST which must be strictly followed.*

- **Figure Extraction & Insertion** (Mandatory during text enrichment when automated LLM providers are unavailable):
  See [references/workflow-figures.md](references/workflow-figures.md)

- **Script CLI Parameters, Naming, & Architecture Rules**:
  See [references/script-reference.md](references/script-reference.md)