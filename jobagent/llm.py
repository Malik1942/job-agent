"""Small provider adapter for grounded LLM assistance.

The rest of the agent only asks for "one completion". This module decides
whether the local environment is configured for Anthropic/Claude or OpenAI
and fails soft when no key is available.
"""

from __future__ import annotations

import os
from typing import Any

import requests


ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"
OPENAI_KEY_ENVS = ("OPENAI_API_KEY", "JOBAGENT_OPENAI_API_KEY")


def _getattr(settings: Any, name: str, default: Any) -> Any:
    return getattr(settings, name, default) if settings is not None else default


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


def selected_provider(settings: Any = None) -> str:
    """Return anthropic/openai/off based on config and available keys."""
    provider = str(_getattr(settings, "provider", "auto") or "auto").lower()
    if provider in {"off", "none", "false", "disabled"}:
        return "off"
    if provider in {"anthropic", "claude"}:
        return "anthropic" if os.getenv(ANTHROPIC_KEY_ENV) else "off"
    if provider in {"openai", "codex"}:
        return "openai" if _env_first(*OPENAI_KEY_ENVS) else "off"
    if os.getenv(ANTHROPIC_KEY_ENV):
        return "anthropic"
    if _env_first(*OPENAI_KEY_ENVS):
        return "openai"
    return "off"


def complete(prompt: str, settings: Any = None, max_tokens: int = 300) -> str | None:
    """Generate text with the configured provider.

    Returns None on missing credentials, missing optional packages, HTTP/API
    failures, or empty output so callers can fall back safely.
    """
    provider = selected_provider(settings)
    if provider == "anthropic":
        model = (
            os.getenv("ANTHROPIC_MODEL")
            or _getattr(settings, "anthropic_model", "claude-sonnet-4-6")
        )
        return _complete_anthropic(prompt, model, max_tokens)
    if provider == "openai":
        model = (
            os.getenv("OPENAI_MODEL")
            or os.getenv("JOBAGENT_OPENAI_MODEL")
            or _getattr(settings, "openai_model", "gpt-5.5")
        )
        return _complete_openai(prompt, model, max_tokens)
    return None


def _complete_anthropic(prompt: str, model: str, max_tokens: int) -> str | None:
    key = os.getenv(ANTHROPIC_KEY_ENV)
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None

    chunks = []
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", "")
        if text:
            chunks.append(text)
    out = "".join(chunks).strip()
    return out or None


def _complete_openai(prompt: str, model: str, max_tokens: int) -> str | None:
    key = _env_first(*OPENAI_KEY_ENVS)
    if not key:
        return None

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": max_tokens,
    }

    try:
        resp = requests.post(
            f"{base_url}/responses",
            headers=headers,
            json=payload,
            timeout=60,
        )
        if resp.status_code in {400, 404, 405}:
            return _complete_openai_chat(base_url, headers, prompt, model, max_tokens)
        resp.raise_for_status()
        return _extract_openai_responses_text(resp.json())
    except Exception:
        return None


def _complete_openai_chat(
    base_url: str, headers: dict[str, str], prompt: str, model: str, max_tokens: int
) -> str | None:
    """Fallback for OpenAI-compatible environments that expose chat only."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        out = (msg.get("content") or "").strip()
        return out or None
    except Exception:
        return None


def _extract_openai_responses_text(data: dict[str, Any]) -> str | None:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    chunks: list[str] = []
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    out = "".join(chunks).strip()
    return out or None
