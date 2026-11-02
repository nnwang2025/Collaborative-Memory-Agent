"""Resolve one model configuration without logging or forwarding credentials.

The canonical DeepSeek official settings are ``DEEPSEEK_API_KEY``,
``DEEPSEEK_BASE_URL`` and ``DEEPSEEK_MODEL``.  Existing SiliconFlow and
OpenAI-compatible variable names remain supported so older deployments keep
working.  Placeholder values from templates are treated as unconfigured: a
non-empty string such as ``your_api_key_here`` must never be sent as a key.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_SILICONFLOW_MODEL = "Qwen/Qwen2.5-14B-Instruct"

_PLACEHOLDER_VALUES = {
    "your_api_key_here",
    "your-api-key-here",
    "your_key_here",
    "sk-your-key-here",
    "replace_me",
    "replace-me",
    "changeme",
    "change-me",
    "placeholder",
    "dummy",
    "example",
    "none",
    "null",
    "xxx",
    "***",
    "...",
}
_PLACEHOLDER_MARKERS = (
    "placeholder",
    "replace_me",
    "replace-me",
    "changeme",
    "change-me",
    "example-key",
    "example_key",
    "dummy-key",
    "dummy_key",
)


def is_placeholder_secret(value: str | None) -> bool:
    """True for empty values and for obvious template/example credentials.

    Real provider keys are left untouched.  The check is deliberately limited
    to exact template strings and clear markers so a valid key is not rejected
    merely because it contains a common word.
    """
    text = (value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if lowered in _PLACEHOLDER_VALUES:
        return True
    if lowered.startswith(("your_", "your-", "sk-your")):
        return True
    if lowered.startswith("<") and lowered.endswith(">"):
        return True
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return True
    return False


def _host(base_url: str) -> str:
    return (urlsplit(base_url).hostname or "").lower()


def _is_deepseek(base_url: str) -> bool:
    host = _host(base_url)
    return host == "api.deepseek.com" or host.endswith(".deepseek.com")


def api_key_names(base_url: str) -> tuple[str, ...]:
    """Credential names in priority order for the configured provider origin.

    A DeepSeek credential is never forwarded to another origin.
    """
    if _is_deepseek(base_url):
        return ("DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY", "OPENAI_API_KEY")
    return ("SILICONFLOW_API_KEY", "OPENAI_API_KEY")


def _first_configured(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if not is_placeholder_secret(value):
            return value
    return ""


def resolve_api_key(base_url: str) -> str:
    """Return a usable key for this origin, or an empty string."""
    return _first_configured(api_key_names(base_url))


def credential_source(base_url: str) -> str | None:
    """Return the environment variable name that supplies the usable key."""
    for name in api_key_names(base_url):
        if not is_placeholder_secret(os.getenv(name, "").strip()):
            return name
    return None


def resolve_base_url() -> str:
    """Resolve the provider URL while preserving legacy variable support.

    An explicit ``DEEPSEEK_BASE_URL`` is canonical and wins over legacy names.
    If only a DeepSeek key is configured, the official endpoint is selected so
    the key is not paired with the SiliconFlow default by accident.
    """
    for name in ("DEEPSEEK_BASE_URL", "SILICONFLOW_BASE_URL", "OPENAI_BASE_URL"):
        value = os.getenv(name, "").strip()
        if value:
            return value.rstrip("/")
    if not is_placeholder_secret(os.getenv("DEEPSEEK_API_KEY", "").strip()):
        return DEFAULT_DEEPSEEK_BASE_URL
    return DEFAULT_SILICONFLOW_BASE_URL


def resolve_model_name(base_url: str | None = None) -> str:
    """Resolve a model name appropriate to the configured origin."""
    base = base_url or resolve_base_url()
    if _is_deepseek(base):
        names = ("DEEPSEEK_MODEL", "SILICONFLOW_MODEL", "MODEL_NAME")
        default = DEFAULT_DEEPSEEK_MODEL
    else:
        names = ("SILICONFLOW_MODEL", "MODEL_NAME")
        default = DEFAULT_SILICONFLOW_MODEL
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default
