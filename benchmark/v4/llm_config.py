"""Provider environment mapping for the opt-in online runner.

Only environment *names* and the exact HTTPS origin are recorded.  Key values
are read at execution time and never returned, logged or written to a report.
"""
from __future__ import annotations

import os
from typing import Any

PROVIDERS = {
    "deepseek": {"key": "DEEPSEEK_API_KEY", "base": "DEEPSEEK_BASE_URL",
                 "model": "DEEPSEEK_MODEL"},
    "siliconflow": {"key": "SILICONFLOW_API_KEY", "base": "SILICONFLOW_BASE_URL",
                    "model": "SILICONFLOW_MODEL"},
    "openai": {"key": "OPENAI_API_KEY", "base": "OPENAI_BASE_URL", "model": "MODEL_NAME"},
}

_ALL_NAMES = {name for provider in PROVIDERS.values() for name in provider.values()}
_ALL_NAMES.add("MODEL_NAME")


def provider_environment(provider: str, model: str, base_url: str,
                         api_key_env: str | None = None) -> dict[str, str]:
    """Build a complete environment for exactly one provider.

    Other providers' key/base/model variables are blanked so a different
    credential cannot be selected by the shared resolver.  The key is read
    from ``api_key_env`` (or the provider default) and returned only inside
    this mapping; callers must never serialize it.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    selected = PROVIDERS[provider]
    key_name = api_key_env or selected["key"]
    key = os.environ.get(key_name, "").strip()
    env = {name: "" for name in _ALL_NAMES}
    env[selected["key"]] = key
    env[selected["base"]] = base_url
    env[selected["model"]] = model
    if provider == "openai":
        env["MODEL_NAME"] = model
    env["LIVE_RETRIEVAL_MODE"] = os.environ.get("LIVE_RETRIEVAL_MODE", "bm25")
    return env


def redact_environment(env: dict[str, Any]) -> dict[str, Any]:
    """Return a report-safe view: names only, never key values."""
    return {name: ("<redacted>" if "KEY" in name and value else "")
            for name, value in env.items()}
