"""Final evidence-acceptance policy for live retrieval.

Candidate recall and final acceptance are deliberately separate:

* Recall: BM25/dense/RRF may return nearest neighbours for any query.
* Acceptance: a parent Evidence record is returned only when an independent
  relevance signal clears a calibrated threshold, or the query contains an
  exact identifier that occurs in the record.

The policy is loaded from ``relevance_policy.json`` next to this module (or the
path in ``LIVE_RELEVANCE_POLICY``).  Thresholds are calibrated on an independent
development corpus; the benchmark test corpus is never used to tune them.

Cosine similarity and lexical coverage are different scales and are never
compared with each other numerically.  RRF scores are rank-fusion scores and are
not used by this gate.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.retrieval import _tokenize

POLICY_ENV = "LIVE_RELEVANCE_POLICY"
DEFAULT_POLICY_PATH = Path(__file__).with_name("relevance_policy.json")
POLICY_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Query/document text signals
# ---------------------------------------------------------------------------

# Function words do not demonstrate that a document answers a query.  Removing
# them keeps coverage from being inflated by long, generic questions.
_EN_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "while",
    "of", "to", "in", "on", "at", "by", "for", "from", "with", "without",
    "about", "into", "over", "under", "between", "through", "during", "before",
    "after", "above", "below", "up", "down", "out", "off", "again", "further",
    "once", "here", "there", "all", "any", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "nor", "not", "only", "own", "same", "so",
    "than", "too", "very", "can", "will", "just", "should", "now", "what",
    "which", "who", "whom", "whose", "why", "how", "does", "do", "did", "is",
    "are", "was", "were", "be", "been", "being", "have", "has", "had", "having",
    "i", "me", "my", "we", "us", "our", "you", "your", "he", "him", "his", "she",
    "her", "it", "its", "they", "them", "their", "this", "that", "these",
    "those", "say", "says", "said", "tell", "please", "find", "search", "show",
    "get", "give", "want", "need", "know", "also", "may", "might", "must",
    "shall", "would", "could", "cannot", "can't", "don't", "doesn't", "isn't",
    "aren't", "wasn't", "weren't", "won't", "wouldn't", "shouldn't", "couldn't",
    # Task-intent and generic format words.  They say what the user wants to do
    # or which container to search, not which fact the document must contain.
    "decision", "reason", "commit", "review", "document", "doc", "evidence",
    "record", "history", "historical", "investigate", "investigation", "explain",
    "describe", "description", "detail", "information", "info", "mention",
    "related", "regarding", "include", "including", "context", "overview",
    "summary", "status", "current", "latest", "change", "issue", "problem",
    "log", "finding", "result", "source", "system", "help", "question", "answer",
    "task", "user", "project", "data", "file", "content", "text", "section",
    "topic", "subject", "thing", "kind", "type", "example", "support", "prove",
    "according", "via", "work", "works",
}

# Only high-frequency grammatical CJK characters are removed; content
# characters are kept individually because no word segmenter is assumed.
_CJK_STOPCHARS = set(
    "的了是在和与及等对中请问么怎何为我你他她它们这那哪个些可以不会要就都而或"
    "但被把从到上下里外时后前也还很太更最并且因所于之其该此已未能需应将者着过"
    "来去吗呢吧啊呀哦嗯您谁"
)

_IDENTIFIER_RE = re.compile(
    r"(?i)(?<![a-z0-9])("
    r"[a-z]{1,12}[-_/]\d{1,8}"                 # ISSUE-101, ARCH-42, v2/release
    r"|[0-9a-f]{7,40}"                          # commit SHA
    r"|[a-z0-9_.-]+\.(?:md|py|json|jsonl|csv|tsv|txt|yaml|yml|toml|ini|log)"
    r"|[a-z]{1,12}\d{1,8}"                      # V2, CVE2026
    r")(?![a-z0-9])"
)

# Chinese option labels ("为什么选择B方案") are identifiers too, but a bare
# Latin letter is only meaningful when the query is otherwise Chinese.  The
# match in the document is case-sensitive and token-bounded so a single "b"
# inside an English word cannot satisfy it.
_CJK_RE = re.compile(r"[一-鿿]")
_OPTION_LABEL_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z])(?![A-Za-z0-9])")


def _normalize_token(token: str) -> str:
    """Light, deterministic suffix normalisation for coverage matching only."""
    # The shared tokenizer keeps ``.``/``/`` so file names and identifiers stay
    # intact; strip only sentence punctuation at token edges.
    value = token.lower().strip(".,;:!?()[]{}\"'")
    if not value:
        return ""
    if re.fullmatch(r"[0-9a-f]{7,40}", value):
        return value
    if value.endswith("'s") and len(value) > 3:
        value = value[:-2]
    if value.endswith("ically") and len(value) > 7:
        value = value[:-4]  # atomically -> atomic, historically -> historic
    elif value.endswith("ies") and len(value) > 4:
        value = value[:-3] + "y"
    elif value.endswith("sses"):
        value = value[:-2]
    elif value.endswith("s") and not value.endswith("ss") and len(value) > 3:
        value = value[:-1]
    if value.endswith("ing") and len(value) > 5:
        value = value[:-3]
    elif value.endswith("ed") and len(value) > 4:
        value = value[:-2]
    # Derivational forms that appear in ordinary task queries.
    if value.endswith("ation") and len(value) > 6:
        value = value[:-3] + "e"   # rotation -> rotate
    elif value.endswith("tion") and len(value) > 5:
        value = value[:-3]            # action -> act, collection -> collect
    return value


def is_stopword(token: str) -> bool:
    lowered = token.lower().strip(".,;:!?()[]{}\"'")
    if lowered in _EN_STOPWORDS:
        return True
    # Check the singular form too, so "decisions" / "documents" are removed.
    if _normalize_token(lowered) in _EN_STOPWORDS:
        return True
    return len(lowered) == 1 and lowered in _CJK_STOPCHARS


def meaningful_tokens(text: str) -> list[str]:
    """Distinct, normalised content tokens in first-occurrence order."""
    seen = set()
    result = []
    for token in _tokenize(text):
        if is_stopword(token):
            continue
        normalized = _normalize_token(token)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def lexical_coverage(query: str, text: str) -> float | None:
    """Fraction of distinct meaningful query tokens present in *text*.

    ``None`` means the query has no content tokens (for example, it consists
    only of stopwords); the gate must not interpret that as zero coverage.
    """
    query_tokens = meaningful_tokens(query)
    if not query_tokens:
        return None
    document_tokens = set(meaningful_tokens(text))
    matched = sum(1 for token in query_tokens if token in document_tokens)
    return matched / len(query_tokens)


def query_identifiers(query: str) -> list[str]:
    query = query or ""
    identifiers = [match.group(1).lower()
                   for match in _IDENTIFIER_RE.finditer(query)]
    if _CJK_RE.search(query):
        identifiers.extend(
            label for label in (match.group(1)
                                for match in _OPTION_LABEL_RE.finditer(query))
            if label != "I")
    return identifiers


def identifier_match(query: str, text: str) -> bool:
    """True when an exact query identifier occurs in the record text."""
    if not (text or ""):
        return False
    query = query or ""
    for match in _IDENTIFIER_RE.finditer(query):
        # Token-bounded, so a truncated SHA never matches a different record's
        # longer SHA and "v2" never matches inside an arbitrary word.
        pattern = (rf"(?<![A-Za-z0-9]){re.escape(match.group(1))}"
                   rf"(?![A-Za-z0-9])")
        if re.search(pattern, text, re.IGNORECASE):
            return True
    if _CJK_RE.search(query):
        for label in _OPTION_LABEL_RE.finditer(query):
            if label.group(1) == "I":
                continue
            # Case-sensitive, token-bounded: option label B must be its own
            # token in the record, not an arbitrary letter inside a word.
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(label.group(1))}"
                         rf"(?![A-Za-z0-9])", text):
                return True
    return False


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RelevancePolicy:
    policy_version: str = "v2-relevance-uncalibrated"
    enabled: bool = True
    dense_min_score: float = 0.782
    lexical_min_coverage: float = 0.55
    identifier_bypass: bool = True
    calibration: dict = field(default_factory=dict)
    source: str = "builtin"

    def __post_init__(self):
        if not str(self.policy_version or "").strip():
            raise ValueError("policy_version is required")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.identifier_bypass, bool):
            raise ValueError("identifier_bypass must be a boolean")
        if not (isinstance(self.dense_min_score, (int, float))
                and math.isfinite(float(self.dense_min_score))
                and -1.0 <= float(self.dense_min_score) <= 1.0):
            raise ValueError("dense_min_score must be finite and within [-1, 1]")
        if not (isinstance(self.lexical_min_coverage, (int, float))
                and math.isfinite(float(self.lexical_min_coverage))
                and 0.0 <= float(self.lexical_min_coverage) <= 1.0):
            raise ValueError("lexical_min_coverage must be finite and within [0, 1]")

    @classmethod
    def disabled(cls, policy_version="ablation-policy-disabled") -> "RelevancePolicy":
        return cls(policy_version=policy_version, enabled=False, source="disabled")

    def decision(self, *, dense_score=None, lexical_coverage=None,
                 identifier_match=False) -> dict:
        """Accept or reject one parent Evidence candidate.

        The two thresholds are evaluated independently (logical OR).  A dense
        cosine score and a lexical coverage ratio are never averaged or ranked
        against one another.
        """
        dense_score = None if dense_score is None else float(dense_score)
        coverage = None if lexical_coverage is None else float(lexical_coverage)
        if not self.enabled:
            return {"accepted": True, "reason": "policy_disabled",
                    "filter_reason": None, "dense_score": dense_score,
                    "lexical_coverage": coverage,
                    "identifier_match": bool(identifier_match)}
        if identifier_match and self.identifier_bypass:
            return {"accepted": True, "reason": "exact_identifier",
                    "filter_reason": None, "dense_score": dense_score,
                    "lexical_coverage": coverage, "identifier_match": True}
        if dense_score is not None and dense_score >= self.dense_min_score:
            return {"accepted": True, "reason": "dense_threshold",
                    "filter_reason": None, "dense_score": dense_score,
                    "lexical_coverage": coverage,
                    "identifier_match": bool(identifier_match)}
        if coverage is not None and coverage >= self.lexical_min_coverage:
            return {"accepted": True, "reason": "lexical_coverage_threshold",
                    "filter_reason": None, "dense_score": dense_score,
                    "lexical_coverage": coverage,
                    "identifier_match": bool(identifier_match)}
        if coverage is None and dense_score is None:
            # The query consists only of stopwords/generic task words, so
            # coverage cannot be computed.  A BM25 candidate exists only when
            # the raw query still shares a token with the record; accept it and
            # record that the relevance gate had no content signal to judge.
            # Dense-only candidates (dense_score present) still need the dense
            # threshold, because nearest-neighbour search always returns a hit.
            return {"accepted": True, "reason": "lexical_signal_unavailable",
                    "filter_reason": None, "dense_score": None,
                    "lexical_coverage": None,
                    "identifier_match": bool(identifier_match)}
        reason = ("no_relevance_signal"
                  if dense_score is None and coverage is None
                  else "below_relevance_thresholds")
        return {"accepted": False, "reason": reason, "filter_reason": reason,
                "dense_score": dense_score, "lexical_coverage": coverage,
                "identifier_match": bool(identifier_match)}

    def describe(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "enabled": self.enabled,
            "identifier_bypass": self.identifier_bypass,
            "dense_min_score": float(self.dense_min_score),
            "lexical_min_coverage": float(self.lexical_min_coverage),
            "threshold_semantics": "independent OR; scores are not compared across channels",
            "source": self.source,
            "calibration": dict(self.calibration),
        }


def _builtin_policy() -> RelevancePolicy:
    """Packaging fallback only; the committed JSON file is authoritative."""
    return RelevancePolicy(
        policy_version="v2-relevance-2026-11-02",
        enabled=True,
        dense_min_score=0.782,
        lexical_min_coverage=0.55,
        identifier_bypass=True,
        calibration={"fallback": "relevance_policy.json missing; built-in defaults"},
        source="builtin_fallback",
    )


def load_relevance_policy(path=None) -> RelevancePolicy:
    """Load a JSON policy; an explicitly named missing file is an error."""
    explicit = path if path is not None else os.getenv(POLICY_ENV)
    candidate = Path(explicit) if explicit else DEFAULT_POLICY_PATH
    if not candidate.is_file():
        if explicit:
            raise FileNotFoundError(f"relevance_policy_missing:{candidate}")
        return _builtin_policy()
    data = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("relevance policy must be a JSON object")
    if data.get("schema_version") not in (None, POLICY_SCHEMA_VERSION):
        raise ValueError("unsupported relevance policy schema_version")
    return RelevancePolicy(
        policy_version=str(data.get("policy_version") or ""),
        enabled=bool(data.get("enabled", True)),
        dense_min_score=float(data.get("dense_min_score")),
        lexical_min_coverage=float(data.get("lexical_min_coverage")),
        identifier_bypass=bool(data.get("identifier_bypass", True)),
        calibration=dict(data.get("calibration") or {}),
        source=str(candidate),
    )
