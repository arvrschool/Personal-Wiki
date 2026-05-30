# CLI Environment Auto-Routing Rule

When Python scripts are executed inside CLI environments (Claude Code, Codex, Gemini, OpenCode), they cannot spawn subprocesses of the same CLI type due to platform limitations. This rule provides automatic detection and routing to use the current host CLI instance for LLM calls.

## Problem Statement

```python
# ❌ This fails in Claude Code CLI:
result = subprocess.run(["claude", "-p", prompt])  # RecursionError or timeout

# ✅ Solution: Use the CLI that's already running:
provider = detect_host_cli()  # Returns "claude-cli"
# Then call_llm() reuses the same claude-cli instance
```

## Solution: Auto-Detection & Routing

### 1. Detection Phase

The `detect_host_cli()` function checks for CLI environment markers in this priority order:

| Priority | Check | Marker | Returns |
|----------|-------|--------|---------|
| 0 | Special | `OPENCODE=1` | `None` (use direct-inference) |
| 1 | Env vars | `CLAUDE_CODE_*` set | `"claude-cli"` |
| 2 | Env vars | `CODEX_*` set | `"codex-cli"` |
| 3 | Process tree | Parent process contains `"claude"` | `"claude-cli"` |
| 3 | Process tree | Parent process contains `"codex"` | `"codex-cli"` |
| 3 | Process tree | Parent process contains `"gemini"` | `"gemini-cli"` |
| None | None | No markers found | `None` |

### 2. Routing Phase

In `enrich_wiki.py:call_llm()`:

```python
def call_llm(prompt: str, provider: str = "auto", ...) -> str:
    # Detect host CLI
    host_cli = detect_host_cli()
    
    # If provider is "auto" and we're in a CLI, route to host CLI
    if provider == "auto" and host_cli:
        provider = host_cli  # Use claude-cli, codex-cli, or gemini-cli
    
    # Then resolve_provider() uses standard subprocess calls
    # (which is safe because the CLI is already running)
```

## Supported CLI Environments

### Claude Code CLI

```bash
# Environment markers
CLAUDE_CODE_SESSION_ID=57c5a069-...
CLAUDE_CODE_ENTRYPOINT=cli
CLAUDE_CODE_EXECPATH=/path/to/claude.exe
...

# Auto-detection
detect_host_cli() → "claude-cli"

# Routing
provider="auto" → provider="claude-cli"

# Script behavior
python ingest_article.py --llm-provider auto
  # Auto-routes to claude-cli ✓
```

### Codex CLI

```bash
# Environment markers
CODEX_THREAD_ID=thread-123
CODEX_MANAGED_PACKAGE_ROOT=/path
CODEX_CI=true

# Auto-detection
detect_host_cli() → "codex-cli"

# Routing
provider="auto" → provider="codex-cli"

# Script behavior
codex exec -- python ingest_article.py --llm-provider auto
  # Auto-routes to codex-cli ✓
```

### Gemini CLI

```bash
# Environment markers
GEMINI_* (various environment variables)
# OR process ancestry contains "gemini"

# Auto-detection
detect_host_cli() → "gemini-cli"

# Routing
provider="auto" → provider="gemini-cli"

# Script behavior
gemini -- python ingest_article.py --llm-provider auto
  # Auto-routes to gemini-cli ✓
```

### OpenCode Agent

```bash
# Environment markers
OPENCODE=1

# Auto-detection
detect_host_cli() → None

# Routing
provider="auto" → falls through to standard resolution
  # Uses direct-inference provider ✓
```

## Usage Examples

### Example 1: Article Ingestion in Claude Code CLI

```bash
# Running in Claude Code CLI (automatic detection)
python /home/user/personal_wiki-main/scripts/ingest_article.py \
  --url "https://baijiahao.baidu.com/s?id=..." \
  --wiki-dir /home/user/wiki \
  --llm-provider auto  # Auto-detects Claude Code CLI
  # → Routing: provider="auto" → host_cli="claude-cli" → uses claude-cli ✓
```

### Example 2: Wiki Enrichment in Codex CLI

```bash
# Running in Codex CLI (automatic detection)
codex exec --sandbox workspace-write -- \
  python /path/to/enrich_wiki.py \
    --wiki-dir /path/to/wiki \
    --entries /path/to/entries.json \
    --llm-provider auto  # Auto-detects Codex CLI
  # → Routing: provider="auto" → host_cli="codex-cli" → uses codex-cli ✓
```

### Example 3: Fallback to Configured Provider in Shell

```bash
# Running in standard shell (no CLI detection)
python ingest_article.py \
  --url "..." \
  --wiki-dir /path/to/wiki \
  --llm-provider auto  # No CLI detected, uses config default
  # → Routing: host_cli=None → uses configured provider (e.g., anthropic) ✓
```

### Example 4: Explicit Provider Override

```bash
# Force use of specific provider (even if CLI detected)
python ingest_article.py \
  --url "..." \
  --wiki-dir /path/to/wiki \
  --llm-provider anthropic  # Explicit, not "auto"
  # → Routing: provider="anthropic" (not "auto") → uses anthropic provider ✓
```

## Implementation Details

### Code Location

- **Detection:** `/scripts/llm_cli_utils.py:detect_host_cli()`
- **Routing:** `/scripts/enrich_wiki.py:call_llm()`

### Environment Variable Precedence

| Scenario | CLAUDE_CODE | CODEX | Result |
|----------|------------|-------|--------|
| Only Claude Code set | ✓ | ✗ | `claude-cli` |
| Only Codex set | ✗ | ✓ | `codex-cli` |
| Both set | ✓ | ✓ | `claude-cli` (priority) |
| Neither set | ✗ | ✗ | Process tree check |

**Rationale:** Claude Code has highest priority because if both are set, Claude Code is the "outer" environment that should be preferred.

### Process Ancestry Fallback

If environment variables don't match any known CLI:

```bash
# Check parent process command line
ps -o command= -p $PPID
# Look for: "claude", "codex", "gemini"
# Return corresponding CLI type
```

This provides a safety net for CLI environments where environment variables might not be set.

## Testing

```bash
# Test 1: Verify Claude Code detection
python3 -c "
import os
os.environ['CLAUDE_CODE_SESSION_ID'] = 'test-id'
import sys; sys.path.insert(0, 'scripts')
from llm_cli_utils import detect_host_cli
assert detect_host_cli() == 'claude-cli'
print('✓ Claude Code detection works')
"

# Test 2: Verify provider routing
python3 -c "
import os
os.environ['CLAUDE_CODE_SESSION_ID'] = 'test-id'
import sys; sys.path.insert(0, 'scripts')
from llm_cli_utils import detect_host_cli
from enrich_wiki import call_llm

host_cli = detect_host_cli()
provider = 'auto'
if provider == 'auto' and host_cli:
    provider = host_cli
assert provider == 'claude-cli'
print('✓ Provider routing works')
"
```

## Compatibility

✅ Backward compatible
- Scripts without `--llm-provider` flag still work
- Falls back to configured provider if CLI not detected
- Explicit provider specification always works

✅ Forward compatible
- New CLI environments can be added by updating `detect_host_cli()`
- Process ancestry check provides automatic detection for new CLIs

## Troubleshooting

### Q: My script still tries to spawn subprocess despite CLI detection

**A:** Check environment variables:
```bash
env | grep CLAUDE_CODE  # Should see multiple CLAUDE_CODE_* variables
```

If empty, detection won't work. Verify you're running inside the CLI.

### Q: Gemini CLI detection not working

**A:** Gemini doesn't set as many env vars as Claude Code. Fallback to process ancestry:
```bash
ps -o command= -p $$  # Should contain "gemini"
```

If not found, may need to update `_iter_process_commands()` depth.

### Q: Both Claude and Codex detected, which takes priority?

**A:** Claude Code CLI has priority (seen as the "outer" environment). To force Codex:
```bash
python script.py --llm-provider codex-cli  # Explicit override
```

## Migration Guide

### Before (Manual Provider Selection)

```bash
# User had to know which CLI to use
python ingest_article.py \
  --url "..." \
  --llm-provider claude-cli  # Manual!
```

### After (Auto-Detection)

```bash
# User just uses "auto" or omits (defaults to auto)
python ingest_article.py \
  --url "..." \
  --llm-provider auto  # Auto-routes ✓
```

## Future Improvements

1. **Caching:** Cache detection result per session to reduce overhead
2. **Config file:** Allow defining preferred CLI in config.toml
3. **Logging:** Add `--debug-cli-detection` flag to trace detection path
4. **Multi-CLI pipelines:** Support chaining multiple CLI environments
