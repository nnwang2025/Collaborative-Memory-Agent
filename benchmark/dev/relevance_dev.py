"""Independent relevance-development corpus and query set.

Content and queries here are intentionally disjoint from the V2 test corpus.
They are used only to choose conservative acceptance thresholds; the V2
benchmark never contributes to calibration.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DOCUMENTS = {
    "gateway.md": (
        "# Gateway timeout policy\n"
        "The API gateway retries idempotent requests three times with exponential "
        "backoff before returning a 504 response. Retry attempts are logged with "
        "the upstream request identifier.\n"
    ),
    "billing.md": (
        "# Billing payment service\n"
        "Card charges are rejected when the issuer declines authorization. "
        "Refunds post within five business days. Reconciliation runs every night.\n"
    ),
    "search.md": (
        "# Search index refresh\n"
        "The search index refreshes every thirty seconds. A force refresh is "
        "available to operations during an incident.\n"
    ),
    "release.md": (
        "# Release freeze\n"
        "No production deployment is allowed during the Friday release freeze "
        "unless an incident commander explicitly approves an exception.\n"
    ),
    "security.md": (
        "# Signing key rotation\n"
        "Rotate signing credentials every ninety days. Old keys remain valid for "
        "signature verification during a twenty-four hour overlap.\n"
    ),
    "incident.md": (
        "# Incident INC-77\n"
        "INC-77 recorded a gateway outage. The root cause was an expired upstream "
        "credential; the gateway retry policy masked the first failures.\n"
    ),
    "storage.md": (
        "# Storage capacity alert\n"
        "Disk usage above eighty-five percent triggers a capacity alert. Archive "
        "old snapshots before requesting additional volumes.\n"
    ),
    "oncall.md": (
        "# On-call escalation\n"
        "Page the secondary on-call engineer after ten minutes if the primary does "
        "not acknowledge the alert. Escalation is recorded in the incident log.\n"
    ),
    "payment_zh.md": (
        "# 支付服务说明\n"
        "信用卡拒付时需要人工审核。退款通常在五个工作日内到账。\n"
    ),
}

# Positives exercise exact identifiers, natural-language rewrites and CJK.
POSITIVE_CASES = [
    {"id": "dev_exact_id", "query": "INC-77", "expected": "incident.md",
     "kind": "exact_id"},
    {"id": "dev_semantic_cards", "query": "payment service rejects valid cards",
     "expected": "billing.md", "kind": "semantic_rewrite"},
    {"id": "dev_semantic_credentials", "query": "credential rotation schedule",
     "expected": "security.md", "kind": "semantic_rewrite"},
    {"id": "dev_gateway_terms", "query": "gateway timeout retry backoff",
     "expected": "gateway.md", "kind": "exact_terms"},
    {"id": "dev_storage_terms", "query": "disk capacity alert threshold",
     "expected": "storage.md", "kind": "exact_terms"},
    {"id": "dev_release_terms", "query": "Friday production deployment freeze",
     "expected": "release.md", "kind": "exact_terms"},
    {"id": "dev_oncall_semantic", "query": "who to page when primary does not respond",
     "expected": "oncall.md", "kind": "semantic_rewrite"},
    {"id": "dev_incident_terms", "query": "gateway outage root cause",
     "expected": "incident.md", "kind": "exact_terms"},
    {"id": "dev_chinese", "query": "信用卡 拒付 怎么处理",
     "expected": "payment_zh.md", "kind": "semantic_rewrite"},
]

# Negatives include completely unrelated topics and queries that share one or
# two surface terms with a document ("lexically similar, semantically wrong").
NEGATIVE_CASES = [
    {"id": "dev_unrelated_quantum", "query": "quantum banana telescope",
     "kind": "completely_unrelated"},
    {"id": "dev_unrelated_biology", "query": "photosynthesis chlorophyll wavelength",
     "kind": "completely_unrelated"},
    {"id": "dev_gateway_cafeteria", "query": "gateway cafeteria lunch menu",
     "kind": "lexically_similar_wrong"},
    {"id": "dev_gateway_timeout_lunch", "query": "gateway timeout cafeteria lunch",
     "kind": "lexically_similar_wrong"},
    {"id": "dev_billing_weather", "query": "billing concert ticket weather forecast",
     "kind": "lexically_similar_wrong"},
    {"id": "dev_security_parking", "query": "security guard parking permit policy",
     "kind": "lexically_similar_wrong"},
    {"id": "dev_release_concert", "query": "release concert schedule tickets",
     "kind": "lexically_similar_wrong"},
    {"id": "dev_storage_warehouse", "query": "storage warehouse rental listing",
     "kind": "lexically_similar_wrong"},
    {"id": "dev_oncall_menu", "query": "on-call cafeteria lunch menu",
     "kind": "lexically_similar_wrong"},
    {"id": "dev_payment_forecast", "query": "payment invoice weather forecast",
     "kind": "lexically_similar_wrong"},
    {"id": "dev_missing_identifier", "query": "INC-999",
     "kind": "missing_identifier"},
    {"id": "dev_chinese_weather", "query": "支付 天气 预报",
     "kind": "lexically_similar_wrong"},
]


def corpus_hash() -> str:
    payload = json.dumps(
        {"documents": DOCUMENTS, "positives": POSITIVE_CASES,
         "negatives": NEGATIVE_CASES},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_corpus(root: Path) -> Path:
    docs = Path(root) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for name, text in DOCUMENTS.items():
        (docs / name).write_text(text, encoding="utf-8")
    return docs
