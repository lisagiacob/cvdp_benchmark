"""
Shared parsing of LLM_MODEL, the single model setting every CVDP agent reads.

LLM_MODEL uses the LiteLLM "<provider>/<model>" form, e.g.:
  bedrock/global.anthropic.claude-sonnet-4-6
  vertex_ai/claude-sonnet-4-6@default
  anthropic/claude-sonnet-4-6
  openrouter/anthropic/claude-3-7-sonnet
  openai/gpt-4o

Each agent maps the provider onto its own CLI's naming scheme. A bare name
with no provider prefix (e.g. claude-sonnet-4-6) is returned with provider ""
so agents can keep their legacy shorthand rules.
"""

from __future__ import annotations

import os

DEFAULT_LLM_MODEL = "claude-sonnet-4-6"

KNOWN_PROVIDERS = {"bedrock", "vertex_ai", "anthropic", "openrouter", "openai"}


def split_llm_model(value: str | None = None) -> tuple[str, str]:
    """Return (provider, model) for LLM_MODEL (or `value`, if given)."""
    if value is None:
        value = os.environ.get("LLM_MODEL", "")
    value = value.strip() or DEFAULT_LLM_MODEL
    provider, sep, model = value.partition("/")
    if sep and provider in KNOWN_PROVIDERS:
        return provider, model
    return "", value
