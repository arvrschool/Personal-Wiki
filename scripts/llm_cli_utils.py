from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from config_loader import cfg


_OPENAI_COMPAT_MODEL_HINTS = {
    "deepseek": ("deepseek",),
    "qwen": ("qwen", "qwq"),
    "kimi": ("kimi", "moonshot"),
    "openrouter": ("openrouter",),
    "siliconflow": ("siliconflow",),
}

_OPENAI_COMPAT_SECTIONS = ("openai", "deepseek", "qwen", "kimi", "openrouter", "siliconflow")

# Provider resolution order: API key providers first, then direct-inference fallback
_TEXT_AUTO_ORDER = (
    "openai",
    "anthropic",
    "ollama",
    "direct-inference",
)

_VISION_AUTO_ORDER = (
    "openai",
    "anthropic",
)


def _cfg_str(section: str, key: str) -> str:
    value = cfg(section, key, "")
    return "" if value is None else str(value).strip()


def _first_nonempty(*values: str | None) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def provider_available(provider: str, *, vision: bool = False) -> bool:
    if provider == "anthropic":
        return has_anthropic_credentials()
    if provider == "openai":
        return has_openai_credentials()
    if provider == "ollama":
        return (not vision) and bool(shutil.which("ollama") or os.environ.get("OLLAMA_HOST"))
    if provider == "direct-inference":
        return not vision
    return False


def resolve_provider(
    provider: str,
    *,
    vision: bool = False,
    allowed: set[str] | None = None,
) -> str:
    if provider != "auto":
        return provider

    allowed_set = set(allowed or (_VISION_AUTO_ORDER if vision else _TEXT_AUTO_ORDER))

    ordered = _VISION_AUTO_ORDER if vision else _TEXT_AUTO_ORDER
    for candidate in ordered:
        if candidate in allowed_set and provider_available(candidate, vision=vision):
            return candidate

    return "direct-inference"


def describe_provider_selection(
    provider: str,
    *,
    vision: bool = False,
    allowed: set[str] | None = None,
) -> str:
    allowed_set = set(allowed or (_VISION_AUTO_ORDER if vision else _TEXT_AUTO_ORDER))
    if provider != "auto":
        if provider not in allowed_set:
            mode = "vision" if vision else "text"
            return f"{provider} (unsupported for {mode})"
        return provider
    resolved = resolve_provider(provider, vision=vision, allowed=allowed_set)
    return f"auto -> {resolved}"


def _model_family(model: str) -> str:
    lower = model.lower()
    if "claude" in lower:
        return "claude"
    if "gemini" in lower:
        return "gemini"
    if any(token in lower for token in (
        "deepseek", "qwen", "qwq", "kimi", "moonshot", "openrouter", "siliconflow",
    )):
        return "openai"
    if "codex" in lower or re.search(r"\bgpt[-\w]*\b", lower) or re.match(r"o\d", lower):
        return "openai"
    return "generic"


def resolve_model_arg(provider: str, model: str) -> str:
    selected = (model or "").strip()
    if not selected:
        return ""

    default_provider = cfg("llm", "provider", "auto")
    default_model = (cfg("llm", "model", "") or "").strip()
    if provider != default_provider and selected == default_model:
        family = _model_family(selected)
        if provider == "anthropic" and family == "claude":
            return selected
        if provider == "openai" and family == "openai":
            return selected
        return ""

    return selected


def anthropic_client_kwargs() -> dict[str, str]:
    kwargs: dict[str, str] = {}
    api_key = _first_nonempty(
        os.environ.get("ANTHROPIC_API_KEY"),
        _cfg_str("anthropic", "api_key"),
    )
    auth_token = _first_nonempty(
        os.environ.get("ANTHROPIC_AUTH_TOKEN"),
        _cfg_str("anthropic", "auth_token"),
    )
    base_url = _first_nonempty(
        os.environ.get("ANTHROPIC_BASE_URL"),
        os.environ.get("ANTHROPIC_BEDROCK_BASE_URL"),
        _cfg_str("anthropic", "base_url"),
    )
    if api_key:
        kwargs["api_key"] = api_key
    elif auth_token:
        kwargs["auth_token"] = auth_token
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


def has_anthropic_credentials() -> bool:
    return bool(
        _first_nonempty(
            os.environ.get("ANTHROPIC_API_KEY"),
            os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            _cfg_str("anthropic", "api_key"),
            _cfg_str("anthropic", "auth_token"),
        )
    )


def anthropic_http_api_key() -> str:
    return _first_nonempty(
        os.environ.get("ANTHROPIC_API_KEY"),
        _cfg_str("anthropic", "api_key"),
    )


def resolve_openai_profile(model: str = "", requested_profile: str | None = None) -> str:
    explicit = _first_nonempty(requested_profile, _cfg_str("llm", "api_profile"))
    if explicit:
        return explicit

    lower = _first_nonempty(model, _cfg_str("llm", "model")).lower()
    for profile, hints in _OPENAI_COMPAT_MODEL_HINTS.items():
        if any(hint in lower for hint in hints):
            return profile
    return "openai"


def _openai_config_value(key: str, model: str = "", profile: str | None = None) -> str:
    section = resolve_openai_profile(model=model, requested_profile=profile)
    value = _cfg_str(section, key)
    if value:
        return value
    if section != "openai":
        return _cfg_str("openai", key)
    return ""


def openai_client_kwargs(model: str = "", profile: str | None = None) -> dict[str, str]:
    kwargs: dict[str, str] = {}
    api_key = _first_nonempty(
        os.environ.get("OPENAI_API_KEY"),
        _openai_config_value("api_key", model=model, profile=profile),
    )
    base_url = _first_nonempty(
        os.environ.get("OPENAI_BASE_URL"),
        _openai_config_value("base_url", model=model, profile=profile),
    )
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


def has_openai_credentials(model: str = "", profile: str | None = None) -> bool:
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return True
    explicit_profile = _first_nonempty(profile, _cfg_str("llm", "api_profile"))
    if explicit_profile:
        return bool(_openai_config_value("api_key", model=model, profile=explicit_profile))
    if _first_nonempty(model, _cfg_str("llm", "model")):
        return bool(_openai_config_value("api_key", model=model, profile=profile))
    return any(_cfg_str(section, "api_key") for section in _OPENAI_COMPAT_SECTIONS)


def call_llm(
    prompt: str,
    provider: str = "auto",
    model: str = "",
    direct_input: str | None = None,
    allowed: set[str] | None = None,
) -> str:
    """Call an LLM and return the response text.

    Two paths:
      1. API key available → call API directly (openai/anthropic/ollama)
      2. No API key       → direct-inference (save prompt, host Agent enriches)
    """

    provider = resolve_provider(
        provider,
        allowed=allowed or {"direct-inference", "anthropic", "openai", "ollama"},
    )
    effective_model = resolve_model_arg(provider, model)

    # Clean up any null bytes that might crash some SDKs/parsers
    prompt = prompt.replace("\x00", "")

    if provider == "direct-inference":
        if direct_input and Path(direct_input).exists():
            return Path(direct_input).read_text(encoding="utf-8")
        prompt_file = Path.cwd() / ".llm_prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        raise RuntimeError(
            f"direct-inference: prompt saved to {prompt_file}.\n"
            f"Have your Agent process it, save the response to a file, "
            f"then re-run with --direct-input <response_file> to resume."
        )

    if provider == "anthropic":
        import json as _json, urllib.request as _urlreq
        client_kwargs = anthropic_client_kwargs()
        api_key = anthropic_http_api_key()
        auth_token = client_kwargs.get("auth_token")

        if not client_kwargs and not api_key:
            raise RuntimeError(
                "Anthropic credentials not found.\n"
                "Set ANTHROPIC_API_KEY or configure [anthropic].api_key in scripts/config.local.toml."
            )
        try:
            from anthropic import Anthropic
            client = Anthropic(**client_kwargs)
            msg = client.messages.create(
                model=effective_model or "claude-3-5-sonnet-latest",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text
        except ImportError:
            # Fallback to direct HTTP call if SDK is missing
            base_url = client_kwargs.get("base_url", "https://api.anthropic.com").rstrip("/")
            if auth_token and "bedrock" in base_url.lower():
                # Specialized Bedrock-via-HTTP header handling (simplified)
                headers = {
                    "Authorization": f"Bearer {auth_token}",
                    "Content-Type": "application/json",
                }
            else:
                if not api_key:
                    raise RuntimeError(
                        "anthropic SDK not installed, and no API key available for HTTP fallback."
                    )
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }

            body = _json.dumps({
                "model": effective_model or "claude-3-5-sonnet-latest",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()

            req = _urlreq.Request(f"{base_url}/v1/messages", data=body, headers=headers)
            with _urlreq.urlopen(req, timeout=cfg("llm", "timeout", 300)) as response:
                resp_data = _json.loads(response.read())
                return resp_data["content"][0]["text"]

    if provider == "openai":
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai SDK not installed. Run: pip install openai")

        client_kwargs = openai_client_kwargs(model=effective_model or model)
        if not client_kwargs.get("api_key"):
            raise RuntimeError(
                "OpenAI-compatible credentials not found. Set OPENAI_API_KEY or configure "
                "[openai]/[deepseek]/[qwen]/[kimi] api_key in scripts/config.local.toml."
            )
        client = OpenAI(**client_kwargs)
        resp = client.chat.completions.create(
            model=effective_model or "gpt-4o",
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content

    if provider == "ollama":
        try:
            import requests as _requests
        except ImportError:
            raise RuntimeError("requests not installed for Ollama. Run: pip install requests")
        resp = _requests.post(
            "http://localhost:11434/api/generate",
            json={"model": effective_model or "llama3", "prompt": prompt, "stream": False},
            timeout=cfg("llm", "timeout", 300),
        )
        resp.raise_for_status()
        return resp.json()["response"]

    raise ValueError(f"Unknown LLM provider: {provider}")
