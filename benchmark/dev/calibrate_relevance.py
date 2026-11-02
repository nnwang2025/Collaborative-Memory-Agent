"""Calibrate the final relevance-acceptance policy on an independent dev set.

Run:
    python -m benchmark.dev.calibrate_relevance

The V2 benchmark corpus is not imported here.  Thresholds are selected from
worst-case negative-query signals on the development corpus plus fixed,
pre-registered safety margins.  The report records every signal so the choice
can be audited and reproduced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from benchmark.dev.relevance_dev import (  # noqa: E402
    DOCUMENTS, NEGATIVE_CASES, POSITIVE_CASES, corpus_hash, write_corpus,
)
from src.evidence import EvidenceScope  # noqa: E402
from src.index.builder import IndexBuilder  # noqa: E402
from src.index.relevance import (  # noqa: E402
    DEFAULT_POLICY_PATH, RelevancePolicy,
)
from src.retrieval import retrieve_evidence  # noqa: E402
from src.sync.models import SourceSpec  # noqa: E402
from src.sync.service import SyncService  # noqa: E402

PROJECT = "relevance-dev"
MODES = ("bm25", "dense", "hybrid")
TOP_K = 10
DENSE_MARGIN = 0.04
COVERAGE_MARGIN = 0.05
POLICY_VERSION = "v2-relevance-2026-11-02"
REPORT_ROOT = Path(__file__).resolve().parent / "reports"


def _round_up(value: float) -> float:
    return min(1.0, math.ceil(value * 1000) / 1000)


def _signals(hit):
    relevance = hit.get("relevance") or {}
    return {
        "dense_score": relevance.get("dense_score"),
        "lexical_coverage": relevance.get("lexical_coverage"),
        "identifier_match": bool(relevance.get("identifier_match")),
    }


def _collect(db, index_root, scope, mode):
    positives = []
    for case in POSITIVE_CASES:
        hits = retrieve_evidence(
            case["query"], scope=scope, db_path=db, index_root=index_root,
            mode=mode, top_k=TOP_K, relevance_policy=RelevancePolicy.disabled())
        found = next((hit for hit in hits
                      if hit.get("original_record_id") == case["expected"]), None)
        positives.append({
            "id": case["id"], "kind": case["kind"], "query": case["query"],
            "expected": case["expected"], "recalled": found is not None,
            "signals": _signals(found) if found else None,
            "retrieved": [hit.get("original_record_id") for hit in hits],
            "diagnostics": getattr(hits, "diagnostics", {}),
        })
    negatives = []
    for case in NEGATIVE_CASES:
        hits = retrieve_evidence(
            case["query"], scope=scope, db_path=db, index_root=index_root,
            mode=mode, top_k=TOP_K, relevance_policy=RelevancePolicy.disabled())
        signals = [_signals(hit) for hit in hits]
        dense = [item["dense_score"] for item in signals
                 if item["dense_score"] is not None]
        coverage = [item["lexical_coverage"] for item in signals
                    if item["lexical_coverage"] is not None]
        negatives.append({
            "id": case["id"], "kind": case["kind"], "query": case["query"],
            "retrieved": [hit.get("original_record_id") for hit in hits],
            "max_dense": max(dense) if dense else None,
            "max_coverage": max(coverage) if coverage else None,
            "diagnostics": getattr(hits, "diagnostics", {}),
        })
    return positives, negatives


def _evaluate(positives, dense_threshold, coverage_threshold):
    for row in positives:
        if not row["recalled"]:
            row["accepted"] = None
            row["gate_reason"] = "not_recalled_by_channel"
            continue
        signals = row["signals"]
        identifier = signals["identifier_match"]
        dense = signals["dense_score"]
        coverage = signals["lexical_coverage"]
        if identifier:
            accepted, reason = True, "exact_identifier"
        elif dense is not None and dense >= dense_threshold:
            accepted, reason = True, "dense_threshold"
        elif coverage is not None and coverage >= coverage_threshold:
            accepted, reason = True, "lexical_coverage_threshold"
        else:
            accepted, reason = False, "below_relevance_thresholds"
        row["accepted"] = accepted
        row["gate_reason"] = reason


def run(root=None, output_policy=None, output_report=None):
    temporary = None
    if root is None:
        temporary = tempfile.TemporaryDirectory(prefix="cma-relevance-dev-")
        root = Path(temporary.name)
    root = Path(root)
    docs = write_corpus(root)
    db = root / "evidence.sqlite3"
    index_root = root / "live_index"
    scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")

    service = SyncService(db)
    source = service.registry.register(SourceSpec(
        project_id=PROJECT, dataset_kind="live", connector_type="directory",
        source_uri=docs.as_uri(), config={"root_path": str(docs)}))
    sync_report = service.sync(source.source_id)
    builder = IndexBuilder(db, index_root=index_root)
    built = builder.build(scope)
    verification = builder.verify(scope)

    per_mode = {}
    for mode in MODES:
        positives, negatives = _collect(db, index_root, scope, mode)
        per_mode[mode] = {"positives": positives, "negatives": negatives}

    dense_values = [row["max_dense"] for mode in MODES
                    for row in per_mode[mode]["negatives"]
                    if row["max_dense"] is not None]
    coverage_values = [row["max_coverage"] for mode in MODES
                       for row in per_mode[mode]["negatives"]
                       if row["max_coverage"] is not None]
    max_negative_dense = max(dense_values) if dense_values else 0.0
    max_negative_coverage = max(coverage_values) if coverage_values else 0.0
    dense_threshold = _round_up(max_negative_dense + DENSE_MARGIN)
    coverage_threshold = _round_up(max_negative_coverage + COVERAGE_MARGIN)

    false_rejections = {}
    negative_false_accepts = {}
    for mode in MODES:
        _evaluate(per_mode[mode]["positives"], dense_threshold, coverage_threshold)
        rejected = [row for row in per_mode[mode]["positives"]
                    if row["accepted"] is False]
        recalled = [row for row in per_mode[mode]["positives"] if row["recalled"]]
        false_rejections[mode] = {
            "found_candidates": len(recalled),
            "false_rejection_count": len(rejected),
            "false_rejection_rate": (len(rejected) / len(recalled) if recalled else None),
            "false_rejection_ids": [row["id"] for row in rejected],
        }
        accepted_negatives = []
        for row in per_mode[mode]["negatives"]:
            dense = row["max_dense"]
            coverage = row["max_coverage"]
            accepted = ((dense is not None and dense >= dense_threshold)
                        or (coverage is not None and coverage >= coverage_threshold))
            if accepted:
                accepted_negatives.append({
                    "id": row["id"], "max_dense": dense, "max_coverage": coverage})
        negative_false_accepts[mode] = {
            "query_count": len(per_mode[mode]["negatives"]),
            "false_accept_count": len(accepted_negatives),
            "false_accept_ids": [row["id"] for row in accepted_negatives],
        }

    policy_path = Path(output_policy or DEFAULT_POLICY_PATH)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    report_path = Path(output_report or (
        REPORT_ROOT / f"relevance-calibration-{stamp}.json"))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_rel = str(report_path.relative_to(Path.cwd())) if report_path.is_relative_to(
        Path.cwd()) else str(report_path)
    calibration = {
        "status": "calibrated",
        "dev_set": "benchmark/dev/relevance_dev.py",
        "dev_corpus_sha256": corpus_hash(),
        "test_set_used_for_calibration": False,
        "negative_max_dense": max_negative_dense,
        "negative_max_coverage": max_negative_coverage,
        "dense_margin": DENSE_MARGIN,
        "coverage_margin": COVERAGE_MARGIN,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "report": report_rel,
    }
    policy = RelevancePolicy(
        policy_version=POLICY_VERSION,
        enabled=True,
        dense_min_score=dense_threshold,
        lexical_min_coverage=coverage_threshold,
        identifier_bypass=True,
        calibration=calibration,
        source=str(policy_path),
    )
    policy_payload = {
        "schema_version": 1,
        "policy_version": policy.policy_version,
        "enabled": policy.enabled,
        "identifier_bypass": policy.identifier_bypass,
        "dense_min_score": float(policy.dense_min_score),
        "lexical_min_coverage": float(policy.lexical_min_coverage),
        "calibration": calibration,
    }
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(policy_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    report = {
        "report": "relevance-calibration",
        "generated_at": calibration["generated_at"],
        "independent_dev_set": True,
        "test_set_used_for_calibration": False,
        "dev_corpus_sha256": calibration["dev_corpus_sha256"],
        "document_count": len(DOCUMENTS),
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "modes": list(MODES),
        "top_k": TOP_K,
        "margins": {"dense": DENSE_MARGIN, "coverage": COVERAGE_MARGIN},
        "negative_signal_max": {
            "dense": max_negative_dense,
            "coverage": max_negative_coverage,
        },
        "selected_policy": policy.describe(),
        "policy_path": str(policy_path),
        "policy_sha256": hashlib.sha256(
            json.dumps(policy_payload, sort_keys=True).encode("utf-8")).hexdigest(),
        "false_rejections": false_rejections,
        "negative_false_accepts": negative_false_accepts,
        "per_mode": per_mode,
        "index": {
            "version": built["version"]["index_version"],
            "chunk_count": built["version"]["chunk_count"],
            "evidence_count": built["version"]["evidence_count"],
            "verification": verification,
        },
        "sync": sync_report.as_dict(),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if temporary is not None:
        temporary.cleanup()
    return report, policy_path, report_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="Keep the temporary dev corpus here")
    parser.add_argument("--output-policy", help="Policy JSON path")
    parser.add_argument("--output-report", help="Calibration report path")
    args = parser.parse_args()
    report, policy_path, report_path = run(
        args.root, args.output_policy, args.output_report)
    print(json.dumps({
        "policy": str(policy_path),
        "report": str(report_path),
        "selected_policy": report["selected_policy"],
        "negative_signal_max": report["negative_signal_max"],
        "false_rejections": report["false_rejections"],
        "negative_false_accepts": report["negative_false_accepts"],
        "index": report["index"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
