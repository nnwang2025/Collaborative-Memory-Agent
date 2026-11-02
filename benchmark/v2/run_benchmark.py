"""V2 live-retrieval benchmark over real temporary sources.

Run:
    python -m benchmark.v2.run_benchmark

This benchmark is independent of frozen Benchmark V1.  It uses the locally
cached bge-small model in offline mode and never calls a paid LLM.
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
from statistics import mean, median

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from benchmark.v2.dataset import (  # noqa: E402
    add_release_commit, apply_deletion, apply_update, create_dataset,
)
from src.evidence import EvidenceScope  # noqa: E402
from src.evidence_store import EvidenceStore  # noqa: E402
from src.index.builder import IndexBuilder  # noqa: E402
from src.index.relevance import RelevancePolicy, load_relevance_policy  # noqa: E402
from src.index.retriever import LiveRetriever  # noqa: E402
from src.retrieval import retrieve_evidence  # noqa: E402
from src.sync.models import SourceSpec  # noqa: E402
from src.sync.service import SyncService  # noqa: E402

PROJECT = "v2-benchmark"
OTHER_PROJECT = "v2-other-project"
MODES = ("bm25", "dense", "hybrid")
TOP_K = 10


def _ids(hits):
    return list(dict.fromkeys(hit["original_record_id"] for hit in hits))


def _recall(ranked, expected, k):
    return len(set(ranked[:k]) & set(expected)) / len(expected) if expected else None


def _mrr(ranked, expected):
    if not expected:
        return None
    for rank, identifier in enumerate(ranked, start=1):
        if identifier in expected:
            return 1.0 / rank
    return 0.0


def _ndcg(ranked, expected, k=10):
    if not expected:
        return None
    gains = [1.0 if identifier in set(expected) else 0.0 for identifier in ranked[:k]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(set(expected)), k)))
    return dcg / ideal if ideal else 0.0


def _percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    position = max(0, math.ceil(len(values) * q) - 1)
    return values[position]


def _index_size(path):
    return sum(file.stat().st_size for file in Path(path).rglob("*") if file.is_file())


def _sync_sources(service, dataset):
    definitions = {
        "directory": SourceSpec(
            project_id=PROJECT, dataset_kind="live", connector_type="directory",
            source_uri=dataset["docs"].as_uri(), config={"root_path": str(dataset["docs"])}),
        "csv": SourceSpec(
            project_id=PROJECT, dataset_kind="live", connector_type="csv",
            source_uri=dataset["issues_csv"].as_uri(),
            config={"path": str(dataset["issues_csv"]), "id_column": "id",
                    "content_column": "description", "category": "issue",
                    "time_column": "updated_at"}),
        "json": SourceSpec(
            project_id=PROJECT, dataset_kind="live", connector_type="json",
            source_uri=dataset["events_json"].as_uri(),
            config={"path": str(dataset["events_json"]), "record_path": "records",
                    "id_field": "id", "content_fields": ["title", "body"],
                    "time_field": "event_time", "category": "document"}),
        "jsonl": SourceSpec(
            project_id=PROJECT, dataset_kind="live", connector_type="json",
            source_uri=dataset["stream_jsonl"].as_uri(),
            config={"path": str(dataset["stream_jsonl"]), "id_field": "id",
                    "content_fields": ["message"], "time_field": "ts",
                    "category": "logs"}),
        "git": SourceSpec(
            project_id=PROJECT, dataset_kind="live", connector_type="git",
            source_uri=dataset["git_repo"].as_uri(),
            config={"repo_path": str(dataset["git_repo"]), "branch": "main"}),
    }
    sources = {name: service.registry.register(spec) for name, spec in definitions.items()}
    reports = {}
    for name, source in sources.items():
        reports[f"{name}:initial"] = service.sync(source.source_id).as_dict()
    return sources, reports


def _run_queries(db, index_root, scope, cases, mode, relevance_policy=None):
    rows = []
    for case in cases:
        latencies = []
        hits = []
        diagnostics = {}
        for _ in range(3):
            started = time.perf_counter()
            hits = retrieve_evidence(
                case["query"], scope=scope, db_path=db, index_root=index_root,
                mode=mode, top_k=TOP_K, filters=case.get("filters"),
                relevance_policy=relevance_policy,
            )
            diagnostics = dict(getattr(hits, "diagnostics", {}) or {})
            latencies.append((time.perf_counter() - started) * 1000)
        ranked = _ids(hits)
        rows.append({
            "case_id": case["id"], "kind": case["kind"], "query": case["query"],
            "expected": case["expected"], "retrieved": ranked[:TOP_K],
            "recall@5": _recall(ranked, case["expected"], 5),
            "recall@10": _recall(ranked, case["expected"], 10),
            "mrr": _mrr(ranked, case["expected"]),
            "ndcg@10": _ndcg(ranked, case["expected"]),
            "latency_ms": median(latencies),
            "requested_mode": mode,
            "effective_mode": diagnostics.get("effective_mode", mode),
            "retrieval_modes": sorted({
                hit.get("retrieval_mode", mode) for hit in hits
            } or {diagnostics.get("effective_mode") or mode}),
            "fallback_reason": diagnostics.get("fallback_reason"),
            "fallback_reasons": sorted({hit.get("fallback_reason") for hit in hits
                                        if hit.get("fallback_reason")}),
            "index_state": diagnostics.get("index_state"),
            "candidate_count": diagnostics.get("candidate_count"),
            "accepted_count": diagnostics.get("accepted_count"),
            "filtered_count": diagnostics.get("filtered_count"),
            "filter_reasons": diagnostics.get("filter_reasons") or {},
            "empty_reason": diagnostics.get("empty_reason"),
            "relevance_policy_version": diagnostics.get("policy_version"),
            "empty_correct": (not hits) if not case["expected"] else None,
        })
    return rows


def _aggregate(rows, mode, build_seconds, size_bytes):
    graded = [row for row in rows if row["recall@5"] is not None]
    negative = [row for row in rows if not row["expected"]]
    latencies = [row["latency_ms"] for row in rows]
    empty_recognized = sum(1 for row in negative if row["empty_correct"])
    return {
        "mode": mode,
        "query_count": len(rows),
        "graded_query_count": len(graded),
        "negative_query_count": len(negative),
        "Recall@5": mean(row["recall@5"] for row in graded) if graded else None,
        "Recall@10": mean(row["recall@10"] for row in graded) if graded else None,
        "MRR": mean(row["mrr"] for row in graded) if graded else None,
        "nDCG@10": mean(row["ndcg@10"] for row in graded) if graded else None,
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "empty_result_accuracy": (
            empty_recognized / len(negative) if negative else None),
        "empty_result_recognized": empty_recognized,
        "empty_result_false_positives": len(negative) - empty_recognized,
        "gated_empty_positive_queries": sum(
            1 for row in graded if not row["retrieved"]),
        "filtered_candidate_total": sum(
            row.get("filtered_count") or 0 for row in rows),
        "index_build_seconds": build_seconds,
        "index_size_bytes": size_bytes,
        "actual_modes": sorted({value for row in rows for value in row["retrieval_modes"]}),
        "fallback_query_count": sum(bool(row["fallback_reasons"]) for row in rows),
    }


def _comparison(gated_rows, ungated_rows):
    """Compare the calibrated gate against the same candidates without it."""
    by_case = {row["case_id"]: row for row in ungated_rows}
    positive = [row for row in gated_rows if row["expected"]]
    lost_queries = []
    lost_occurrences = 0
    expected_occurrences = 0
    for row in positive:
        before = by_case[row["case_id"]]
        expected = set(row["expected"])
        before_ids = set(before["retrieved"])
        after_ids = set(row["retrieved"])
        lost = expected & (before_ids - after_ids)
        expected_occurrences += len(expected)
        if lost:
            lost_queries.append({
                "case_id": row["case_id"], "lost_expected_ids": sorted(lost),
                "ungated_retrieved": before["retrieved"],
                "gated_retrieved": row["retrieved"],
            })
            lost_occurrences += len(lost)
    negatives = []
    for row in gated_rows:
        if row["expected"]:
            continue
        before = by_case[row["case_id"]]
        negatives.append({
            "case_id": row["case_id"],
            "ungated_retrieved": before["retrieved"],
            "gated_retrieved": row["retrieved"],
            "gated_empty": not row["retrieved"],
            "candidate_count": row.get("candidate_count"),
            "filtered_count": row.get("filtered_count"),
            "filter_reasons": row.get("filter_reasons"),
            "effective_mode": row.get("effective_mode"),
        })
    return {
        "normal_query_count": len(positive),
        "expected_occurrences_in_ungated_top_k": expected_occurrences,
        "lost_expected_occurrences": lost_occurrences,
        "queries_with_expected_lost": len(lost_queries),
        "normal_evidence_false_rejection_rate": (
            len(lost_queries) / len(positive) if positive else None),
        "lost_expected_examples": lost_queries,
        "gate_filtered_candidate_total": sum(
            row.get("filtered_count") or 0 for row in gated_rows),
        "negative_queries": negatives,
        "empty_result_recognition_rate": (
            sum(1 for item in negatives if item["gated_empty"]) / len(negatives)
            if negatives else None),
    }


def _deleted_false_recall(db, index_root, scope, cases):
    result = {}
    for mode in MODES:
        affected = 0
        for case in cases:
            hits = retrieve_evidence(
                case["query"], scope=scope, db_path=db, index_root=index_root,
                mode=mode, top_k=TOP_K, filters=case.get("filters"),
            )
            if any(hit["original_record_id"] == "obsolete.md" for hit in hits):
                affected += 1
        result[mode] = {"queries_returning_deleted": affected,
                        "query_count": len(cases),
                        "rate": affected / len(cases) if cases else 0.0}
    return result


def _contamination(db, index_root, scope, cases):
    total_hits = contaminated = 0
    for case in cases:
        hits = retrieve_evidence(
            case["query"], scope=scope, db_path=db, index_root=index_root,
            mode="hybrid", top_k=TOP_K, filters=case.get("filters"))
        for hit in hits:
            total_hits += 1
            if hit["project_id"] != scope.project_id:
                contaminated += 1
    return {"hits": total_hits, "out_of_scope_hits": contaminated,
            "rate": contaminated / total_hits if total_hits else 0.0}


def run(root=None, output=None):
    temporary = None
    if root is None:
        temporary = tempfile.TemporaryDirectory(prefix="cma-v2-benchmark-")
        root = Path(temporary.name)
    root = Path(root)
    dataset = create_dataset(root)
    db = root / "evidence.sqlite3"
    index_root = root / "live_index"
    scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")

    service = SyncService(db)
    sources, sync_reports = _sync_sources(service, dataset)

    apply_update(dataset)
    sync_reports["csv:update"] = service.sync(sources["csv"].source_id).as_dict()
    add_release_commit(dataset)
    sync_reports["git:incremental"] = service.sync(sources["git"].source_id).as_dict()

    current_issue = next(record for record in service.store.list(scope)
                         if record.original_record_id == "ISSUE-102")
    old_issue_versions = service.store.list_versions(current_issue.evidence_id, scope)
    obsolete = next(record for record in service.store.list(scope)
                    if record.original_record_id == "obsolete.md")

    builder = IndexBuilder(db, index_root=index_root)
    started = time.perf_counter()
    built = builder.build(scope)
    build_seconds = time.perf_counter() - started
    size_bytes = _index_size(built["version"]["path"])
    verification = builder.verify(scope)

    other_service = SyncService(db)
    other_source = other_service.registry.register(SourceSpec(
        project_id=OTHER_PROJECT, dataset_kind="live", connector_type="directory",
        source_uri=dataset["other_project"].as_uri(),
        config={"root_path": str(dataset["other_project"])}))
    sync_reports["other:initial"] = other_service.sync(other_source.source_id).as_dict()

    policy = load_relevance_policy()
    results = {}
    comparisons = {}
    for mode in MODES:
        ungated_rows = _run_queries(
            db, index_root, scope, dataset["cases"], mode,
            relevance_policy=RelevancePolicy.disabled())
        rows = _run_queries(
            db, index_root, scope, dataset["cases"], mode,
            relevance_policy=policy)
        results[mode] = {
            "rows": rows,
            "summary": _aggregate(rows, mode, build_seconds, size_bytes),
            "ablation_ungated": {
                "rows": ungated_rows,
                "summary": _aggregate(
                    ungated_rows, mode, build_seconds, size_bytes),
            },
        }
        comparisons[mode] = _comparison(rows, ungated_rows)

    # Verify chunk -> parent hydration using the best hybrid hit for a long doc.
    hybrid_rows = results["hybrid"]["rows"]
    long_case = next(row for row in hybrid_rows if row["case_id"] == "long_document")
    hydration = {"checked": False, "ok": None, "unavailable_chunks_skipped": 0}
    if long_case["retrieved"]:
        hits = retrieve_evidence(long_case["query"], scope=scope, db_path=db,
                                 index_root=index_root, mode="hybrid", top_k=TOP_K)
        evidence_store = EvidenceStore(db)
        for hit in hits:
            parent = evidence_store.get(hit["evidence_id"], scope, hit["content_hash"])
            for chunk in hit.get("matched_chunks") or []:
                if chunk.get("locator_status") != "exact":
                    hydration["unavailable_chunks_skipped"] += 1
                    continue
                hydration["checked"] = True
                hydration["ok"] = bool(
                    parent and parent.content[chunk["char_start"]:chunk["char_end"]]
                    == chunk["text"])
                if not hydration["ok"]:
                    break
            if hydration["checked"]:
                break

    apply_deletion(dataset)
    sync_reports["directory:delete"] = service.sync(sources["directory"].source_id).as_dict()
    deleted_before_rebuild = _deleted_false_recall(db, index_root, scope, dataset["cases"])
    historical_refetch = service.store.get(
        obsolete.evidence_id, scope, obsolete.content_hash) is not None

    started = time.perf_counter()
    rebuilt = builder.build(scope, force=True)
    rebuild_seconds = time.perf_counter() - started
    verification_after = builder.verify(scope)
    deleted_after_rebuild = _deleted_false_recall(db, index_root, scope, dataset["cases"])

    contamination = _contamination(db, index_root, scope, dataset["cases"])
    active_path = Path(builder.store.active_version(scope)["path"])
    hidden_path = active_path.with_name(active_path.name + ".probe-hidden")
    os.replace(active_path, hidden_path)
    try:
        fallback = LiveRetriever(db, mode="hybrid").retrieve(
            "tombstones", scope=scope, top_k=3)
    finally:
        os.replace(hidden_path, active_path)
    failure_probe = {
        "retrieval_mode": fallback[0]["retrieval_mode"] if fallback else None,
        "fallback_reason": fallback[0]["fallback_reason"] if fallback else None,
        "index_state": fallback[0]["index_state"] if fallback else None,
        "diagnostics": dict(getattr(fallback, "diagnostics", {}) or {}),
    }

    corpus_hash = hashlib.sha256(json.dumps(dataset["cases"], sort_keys=True).encode()).hexdigest()
    report = {
        "benchmark": "v2-live-retrieval",
        "benchmark_variant": "relevance-gated",
        "dataset": "benchmark/v2 synthetic held-out test corpus",
        "frozen_v1_modified": False,
        "relevance_gate": {
            "policy": policy.describe(),
            "calibration_uses_this_test_set": False,
            "independent_dev_set": "benchmark/dev/relevance_dev.py",
            "calibration_report": policy.calibration.get("report"),
        },
        "project": PROJECT,
        "corpus_cases_sha256": corpus_hash,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "chunker_version": "v2.0",
        "top_k": TOP_K,
        "queries": len(dataset["cases"]),
        "sync_reports": sync_reports,
        "index": {
            "version": built["version"]["index_version"],
            "evidence_count": built["version"]["evidence_count"],
            "chunk_count": built["version"]["chunk_count"],
            "build_seconds": build_seconds,
            "rebuild_seconds": rebuild_seconds,
            "size_bytes": size_bytes,
            "verification": verification,
            "verification_after_rebuild": verification_after,
            "rebuilt_version": rebuilt["version"]["index_version"],
            "chunk_parent_hydration": hydration,
        },
        "lifecycle": {
            "updated_record": {"record_id": "ISSUE-102",
                               "version_count": len(old_issue_versions),
                               "identity_stable": len({v.evidence_id for v in old_issue_versions}) == 1},
            "deleted_record_historical_refetch": historical_refetch,
            "deleted_false_recall_before_rebuild": deleted_before_rebuild,
            "deleted_false_recall_after_rebuild": deleted_after_rebuild,
            "cross_project_contamination": contamination,
            "index_failure_probe": failure_probe,
        },
        "modes": results,
        "retrieval_comparison": comparisons,
    }
    if output is None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        output = (Path(__file__).resolve().parent / "reports"
                  / f"v2-live-retrieval-relevance-gated-{stamp}.json")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if temporary is not None:
        temporary.cleanup()
    return report, output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="Keep generated data under this directory")
    parser.add_argument("--output", help="Report JSON path")
    args = parser.parse_args()
    report, output = run(args.root, args.output)
    print(json.dumps({
        "report": str(output),
        "policy": report["relevance_gate"]["policy"],
        "gated_modes": {name: report["modes"][name]["summary"] for name in MODES},
        "ungated_modes": {
            name: report["modes"][name]["ablation_ungated"]["summary"]
            for name in MODES},
        "comparison": report["retrieval_comparison"],
        "lifecycle": report["lifecycle"],
        "index": {key: report["index"][key] for key in
                  ("build_seconds", "size_bytes", "chunk_count", "verification")},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
