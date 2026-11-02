"""Model-aware context budgeting and deterministic token estimation.

Phase 3 extends the existing byte-based request budget instead of replacing it.
``max_context_bytes`` remains an auxiliary hard limit; this module adds a
model context window, a reserved output allowance, a safety margin and a
per-category view of the request.

The estimator is deliberately conservative and is **not** a billed-token
counter.  A real tokenizer is used only when explicitly requested and actually
importable; otherwise the estimate mode is recorded in every trace payload.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

TOKENIZER_ESTIMATE_VERSION = "heuristic_v1"
BUDGET_POLICY_VERSION = "phase3.budget.v1"
CONTEXT_SCHEMA_VERSION = "phase3.investigation.v1"

# Conservative defaults.  They are only used when the caller does not supply a
# window; the resolved value is always written into the context trace.
MODEL_CONTEXT_WINDOWS = {
    "deepseek-chat": 64000,
    "deepseek-reasoner": 64000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4.1": 1047576,
    "gpt-4.1-mini": 1047576,
    "claude-3-5-sonnet": 200000,
    "claude-sonnet-4": 200000,
    "claude-sonnet-4-5": 200000,
    "claude-opus-4": 200000,
    "claude-opus-4-5": 200000,
}
DEFAULT_MODEL_CONTEXT_WINDOW = 64000


def resolve_model_context_window(model: str | None, override: int | None = None) -> int:
    if override is not None:
        return int(override)
    name = str(model or "").strip().lower()
    if name in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[name]
    # Longest matching family prefix, so provider-prefixed identifiers resolve.
    matches = [window for family, window in MODEL_CONTEXT_WINDOWS.items()
               if name.startswith(family)]
    return max(matches) if matches else DEFAULT_MODEL_CONTEXT_WINDOW


def _is_cjk(character: str) -> bool:
    value = ord(character)
    return (0x3400 <= value <= 0x4DBF or 0x4E00 <= value <= 0x9FFF
            or 0xF900 <= value <= 0xFAFF or 0x3040 <= value <= 0x30FF
            or 0xAC00 <= value <= 0xD7AF)


def estimate_text_tokens(text: str) -> int:
    """Conservative deterministic estimate, not a provider token count.

    ASCII is assumed to pack at most 3.2 characters per token, CJK at most one
    character per token and other scripts at most two.  Rounding up keeps the
    estimate on the safe side of a real tokenizer for the material this system
    handles.
    """
    ascii_chars = cjk_chars = other_chars = 0
    for character in str(text or ""):
        if ord(character) < 128:
            ascii_chars += 1
        elif _is_cjk(character):
            cjk_chars += 1
        else:
            other_chars += 1
    return (math.ceil(ascii_chars / 3.2) + cjk_chars
            + math.ceil(other_chars / 2.0))


def estimate_message_tokens(messages) -> int:
    total = 0
    for message in messages or []:
        total += 4 + estimate_text_tokens(message.get("content") or "")
        total += estimate_text_tokens(str(message.get("role") or ""))
    return total


class TokenCounter:
    """Tokenizer adapter with an explicit, auditable mode."""

    def __init__(self, model: str | None = None, mode: str = "estimate",
                 encoding_name: str | None = None):
        self.model = str(model or "")
        self.requested_mode = mode
        self.mode = "estimate"
        self.name = TOKENIZER_ESTIMATE_VERSION
        self.fallback_reason = None
        self._encoder = None
        if mode in {"tiktoken", "exact"}:
            try:  # optional dependency; never required for offline operation
                import tiktoken  # type: ignore
            except ModuleNotFoundError:
                self.fallback_reason = "tiktoken_unavailable"
            else:
                try:
                    self._encoder = (tiktoken.encoding_for_model(self.model)
                                     if self.model else None)
                except Exception:
                    self._encoder = None
                if self._encoder is None:
                    try:
                        self._encoder = tiktoken.get_encoding(
                            encoding_name or "cl100k_base")
                    except Exception:
                        self.fallback_reason = "encoding_unavailable"
                if self._encoder is not None:
                    self.mode = "exact"
                    self.name = "tiktoken"
        if self.mode == "estimate":
            self.fallback_reason = self.fallback_reason or "estimate_requested"

    @property
    def exact(self) -> bool:
        return self.mode == "exact"

    def count_text(self, text: str) -> int:
        if self._encoder is not None:
            return len(self._encoder.encode(str(text or "")))
        return estimate_text_tokens(text)

    def count_messages(self, messages) -> int:
        if self._encoder is not None:
            total = 0
            for message in messages or []:
                total += 4 + len(self._encoder.encode(
                    str(message.get("content") or "")))
                total += len(self._encoder.encode(
                    str(message.get("role") or "")))
            return total
        return estimate_message_tokens(messages)

    def trace(self) -> dict:
        return {
            "tokenizer_mode": "exact" if self.exact else "estimated",
            "tokenizer_name": self.name,
            "tokenizer_requested_mode": self.requested_mode,
            "tokenizer_fallback_reason": self.fallback_reason,
            "estimate_kind": ("provider_tokenizer" if self.exact
                              else "estimated_not_billed_tokens"),
        }


@dataclass(frozen=True)
class ContextBudgetPolicy:
    model: str = ""
    model_context_window: int = DEFAULT_MODEL_CONTEXT_WINDOW
    reserved_output_tokens: int = 2048
    safety_margin_tokens: int = 512
    tokenizer_mode: str = "estimate"
    max_context_bytes: int = 48000
    max_excerpts: int = 8
    max_excerpts_per_evidence: int = 1
    evidence_share: float = 0.62
    session_share: float = 0.12
    tool_history_share: float = 0.16
    decision_share: float = 0.10

    def trace(self) -> dict:
        return {
            "budget_policy_version": BUDGET_POLICY_VERSION,
            "model_context_window": self.model_context_window,
            "reserved_output_tokens": self.reserved_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "max_context_bytes": self.max_context_bytes,
            "max_excerpts": self.max_excerpts,
            "max_excerpts_per_evidence": self.max_excerpts_per_evidence,
        }


def build_policy(*, model: str = "", max_tokens: int = 64000,
                 max_output_tokens: int = 2048, context_window_tokens=None,
                 safety_margin_tokens: int = 512, tokenizer_mode: str = "estimate",
                 max_context_bytes: int = 48000, max_excerpts: int = 8,
                 max_excerpts_per_evidence: int = 1) -> ContextBudgetPolicy:
    window = resolve_model_context_window(model, context_window_tokens)
    reserved = max(1, min(int(max_output_tokens), max(1, window // 2)))
    return ContextBudgetPolicy(
        model=str(model or ""), model_context_window=window,
        reserved_output_tokens=reserved,
        safety_margin_tokens=max(0, min(int(safety_margin_tokens), max(0, window // 4))),
        tokenizer_mode=tokenizer_mode, max_context_bytes=max(1, int(max_context_bytes)),
        max_excerpts=max(1, int(max_excerpts)),
        max_excerpts_per_evidence=max(1, int(max_excerpts_per_evidence)))


@dataclass
class ContextBudget:
    """Resolved ceiling for one model request.

    ``input_ceiling_tokens`` is the smaller of the model window after reserving
    output/safety and the remaining total token budget.  The byte limit stays a
    separate auxiliary ceiling.
    """
    policy: ContextBudgetPolicy
    counter: TokenCounter
    remaining_total_tokens: int
    categories: dict = field(default_factory=dict)

    @property
    def window_ceiling(self) -> int:
        return max(0, self.policy.model_context_window
                   - self.policy.reserved_output_tokens
                   - self.policy.safety_margin_tokens)

    @property
    def input_ceiling_tokens(self) -> int:
        return max(0, min(self.window_ceiling,
                          int(self.remaining_total_tokens)))

    def tokens(self, text: str) -> int:
        return self.counter.count_text(text)

    def messages_tokens(self, messages) -> int:
        return self.counter.count_messages(messages)

    @property
    def max_context_bytes(self) -> int:
        return self.policy.max_context_bytes

    def overflow(self, *, category: str, required: int, available: int,
                 pruned: int = 0, reasons=None) -> "ContextOverflowError":
        detail = {
            **self.policy.trace(), **self.counter.trace(),
            "context_limit": {
                "code": "context_limit",
                "reason": "mandatory_state_exceeds_budget",
                "category": category,
                "required_input_tokens": int(required),
                "available_input_tokens": int(available),
            },
            "estimated_input_tokens": int(required),
            "input_token_ceiling": int(available),
            "pruned_count": int(pruned),
            "pruning_reasons": dict(reasons or {}),
            "category_tokens": dict(self.categories),
        }
        return ContextOverflowError(
            "Mandatory context exceeds the model input budget", detail)


class ContextOverflowError(ValueError):
    """Structured ``context_limit`` error; never silently drops mandatory rules."""

    code = "context_limit"

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = dict(detail or {})
        self.detail.setdefault("context_limit", {"code": self.code,
                                                 "reason": "mandatory_state_exceeds_budget"})
        self.detail.setdefault("code", self.code)


def summarize_category_tokens(categories: dict) -> dict:
    """Stable, compact category report for traces and Run queries."""
    return {name: {"tokens": int(value), "estimated": True}
            for name, value in sorted((categories or {}).items())}
