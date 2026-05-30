#!/usr/bin/env python3
"""Test LLM invocation methods for personal_wiki ingest pipeline."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config_loader import cfg

PROMPT = """Extract structured info from this article. Return ONLY valid JSON.

Article:
一天，记者问董宇辉："张雪峰到底靠不靠谱？" 董宇辉笑着说："我不够资历随意评论，但我记得他说过一句特别重要的话。"

Return JSON with keys: title, summary, key_points (list), category, tone
"""


def test_provider_resolution():
    print("=" * 60)
    print("PROVIDER RESOLUTION & CONFIG MERGING")
    print("=" * 60)
    from llm_cli_utils import resolve_provider, has_openai_credentials, has_anthropic_credentials
    from config_loader import cfg
    
    ds_key = cfg('deepseek', 'api_key', '')
    print(f"  deepseek api_key (from config): {ds_key[:5]}...{ds_key[-5:] if len(ds_key) > 10 else ''}")
    print(f"  has_openai_credentials(): {has_openai_credentials()}")
    print(f"  has_anthropic_credentials(): {has_anthropic_credentials()}")
    print(f"  resolve_provider('auto'): {resolve_provider('auto')}")


def test_api_deepseek():
    print("\n" + "=" * 60)
    print("API: deepseek (openai-compatible)")
    print("=" * 60)
    try:
        from openai import OpenAI
    except ImportError:
        print("  SKIP: 'openai' library not installed")
        return False
        
    key = cfg('deepseek', 'api_key', '')
    base = cfg('deepseek', 'base_url', '')
    if not key:
        print("  SKIP: no API key")
        return False
    
    print(f"  Attempting call to {base}...")
    try:
        client = OpenAI(api_key=key, base_url=base)
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[{'role': 'user', 'content': PROMPT}],
            max_tokens=500,
        )
        text = resp.choices[0].message.content
        print(f"  response: {text[:300]}")
        data = json.loads(text)
        print(f"  parsed JSON: {list(data.keys())}")
        return True
    except Exception as e:
        print(f"  FAILED: {e}")
        return False


def test_call_llm_from_utils():
    print("\n" + "=" * 60)
    print("UTILS call_llm (llm_cli_utils.py)")
    print("=" * 60)
    from llm_cli_utils import call_llm
    result = call_llm(PROMPT, provider="auto", model="")
    print(f"  result type: {type(result)}")
    print(f"  result: {result[:300] if result else None}")
    return result is not None


def main():
    print(f"Python: {sys.version}")
    print(f"Working dir: {os.getcwd()}\n")

    test_provider_resolution()
    test_api_deepseek()
    test_call_llm_from_utils()


if __name__ == "__main__":
    main()
