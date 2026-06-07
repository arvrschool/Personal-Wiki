"""
config_loader.py — Lightweight TOML config loader (no external dependencies).

Usage:
    from config_loader import cfg
    max_figs = cfg("figures", "max_figures", 10)
"""

from __future__ import annotations
from pathlib import Path

_config: dict | None = None


def get_config(config_path: str | Path | None = None) -> dict:
    """Load and return the config dict.

    Search order:
      (a) config_path argument
      (b) config.toml in the same directory as this file
      (c) config.local.toml in the same directory as this file (overrides config.toml)
      (c) empty dict (callers use their own defaults)
    """
    global _config
    if _config is not None:
        return _config

    if config_path:
        path = Path(config_path)
        _config = _load_toml(path) if path.exists() else {}
        return _config

    config_dir = Path(__file__).parent
    base_path = config_dir / "config.toml"
    local_path = config_dir / "config.local.toml"
    base = _load_toml(base_path) if base_path.exists() else {}
    local = _load_toml(local_path) if local_path.exists() else {}
    _config = _merge_dicts(base, local)
    return _config


def cfg(section: str, key: str, default):
    """Return config[section][key], or default if missing."""
    return get_config().get(section, {}).get(key, default)


def get_wiki_paths(wiki_root: str | Path) -> dict[str, Path]:
    """Determine output directories (support both legacy nested 'wiki/' and flat root)."""
    root = Path(wiki_root)
    
    # Try flat root first (sources/ directly in wiki_root)
    if (root / "sources").exists() or (root / "entities").exists():
        return {
            "sources": root / "sources",
            "entities": root / "entities",
            "topics": root / "topics",
            "articles": root / "articles"
        }
        
    # Check for nested 'wiki/' folder
    if (root / "wiki" / "sources").exists():
        return {
            "sources": root / "wiki" / "sources",
            "entities": root / "wiki" / "entities",
            "topics": root / "wiki" / "topics",
            "articles": root / "wiki" / "articles"
        }

    # Default fallback to nested (standard convention)
    return {
        "sources": root / "wiki" / "sources",
        "entities": root / "wiki" / "entities",
        "topics": root / "wiki" / "topics",
        "articles": root / "wiki" / "articles"
    }


def _merge_dicts(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Minimal TOML parser (handles simple key = value and [section] tables only)
# ---------------------------------------------------------------------------

def _load_toml(path: Path) -> dict:
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass
    # Fallback: hand-rolled parser for flat key=value + [section] tables
    result: dict = {}
    current: dict = result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            current = result.setdefault(section, {})
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.split("#")[0].strip()  # strip inline comments
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                current[key] = val[1:-1]
            elif val.lower() == "true":
                current[key] = True
            elif val.lower() == "false":
                current[key] = False
            else:
                try:
                    current[key] = int(val)
                except ValueError:
                    try:
                        current[key] = float(val)
                    except ValueError:
                        current[key] = val
    return result
