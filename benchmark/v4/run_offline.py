"""Run the Phase 4 benchmark offline against the real ReAct / Workflow chains.

Offline means: no provider key, no network, no paid model.  Both backends are
executed on cloned copies of the same frozen snapshot.  Because neither backend
can use model reasoning here, the result is a deterministic architecture
baseline, not a semantic capability score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import dataset, indexing
from .dataset import Snapshot, clone_databases, materialize_template, summary
from .indexing import IndexPreparationError
from .runtime import CAPABILITY_NOTES, run_task
from .scoring import (SCORING_VERSION, aggregate, aggregate_by, diagnose, pending_grading,
                      score_run)

ROOT = Path(__file__).resolve().parents[2]
REPORT_VERSION = "cma-v4-report-v1"
SOURCE_FILES = [
    "src/graph.py", "src/agents/react_agent.py", "src/agents/coordinator_agent.py",
    "src/agents/answer_agent.py", "src/retrieval.py", "src/skills/registry.py",
    "src/investigation.py", "src/memory/decisions.py", "src/investigation_context.py",
    "src/context_budget.py", "benchmark/v4/runtime.py", "benchmark/v4/scoring.py",
    "benchmark/v4/dataset.py", "benchmark/v4/run_offline.py",
    "benchmark/v4/indexing.py",
]
VERSION_GROUPS = {
    "agent_version": ["src/agents/react_agent.py", "src/investigation.py",
                      "src/agents/answer_agent.py", "src/skills/registry.py"],
    "workflow_version": ["src/graph.py", "src/agents/coordinator_agent.py"],
    "retrieval_version": ["src/retrieval.py"],
    "context_strategy_version": ["src/investigation_context.py", "src/context_budget.py"],
}
DEFAULT_LIMITATIONS = [
    "Offline ReAct runs offline_fallback without a model decision loop; it cannot parse known_at, choose time filters, or adapt queries semantically.",
    "Workflow has fixed keyword routing, no Claim Ledger, no budget protocol and no multi-round investigation.",
    "The comparison is an end-to-end architecture baseline under no-model conditions, not a real-model capability comparison.",
    "TaskSuccessRate and semantic support remain null pending independent human review.",
    "AppropriateAbstentionRate, ConflictDetectionRate and DecisionStateAccuracy are deterministic signals, not semantic judgments.",
    "Labels are synthetic authoring records; independent human review has not been performed.",
]


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_hashes() -> dict[str, str]:
    return {relative: _hash(ROOT / relative) for relative in SOURCE_FILES}


def _group_hash(paths: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / relative).read_bytes())
    return digest.hexdigest()


def _git_state() -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                       text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    head = run("rev-parse", "HEAD")
    porcelain = run("status", "--porcelain")
    return {
        "head_commit": head,
        "worktree_dirty": bool(porcelain),
        "worktree_sha256": hashlib.sha256((porcelain or "").encode("utf-8")).hexdigest(),
        "worktree_changed_files": len(porcelain.splitlines()) if porcelain else 0,
    }


class RetrievalChannelError(RuntimeError):
    """A requested dense/hybrid channel did not actually execute."""


def assert_requested_channel(raw: dict[str, Any], retrieval_mode: str,
                             task_id: str) -> None:
    """Refuse to record a dense/hybrid run that fell back to BM25.

    A task may legitimately attempt no retrieval (for example an injected tool
    failure).  Once retrieval is attempted, every event must report the
    requested mode over a verified index; a fallback aborts the formal run.
    """
    if retrieval_mode == "bm25":
        return
    retrieval = raw.get("retrieval") or {}
    requested = set(retrieval.get("requested_modes") or [])
    effective = set(retrieval.get("effective_modes") or [])
    if not requested and not effective:
        return
    if retrieval.get("fallback_used"):
        raise RetrievalChannelError(
            f"{task_id}: requested {retrieval_mode} but executed "
            f"bm25_fallback ({sorted(retrieval.get('fallback_reasons') or [])})")
    if effective != {retrieval_mode}:
        raise RetrievalChannelError(
            f"{task_id}: requested {retrieval_mode}, effective modes were "
            f"{sorted(effective) or ['none']}")
    if not retrieval.get("index_versions"):
        raise RetrievalChannelError(
            f"{task_id}: {retrieval_mode} ran without a verified index_version")


def configuration(snapshot: Snapshot, *, retrieval_mode: str,
                  context_mode: str = "phase3_current",
                  index_report: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "dataset_version": snapshot.dataset_version,
        "task_set_version": snapshot.task_set_version,
        "snapshot_id": snapshot.snapshot_id,
        "dataset_manifest_sha256": _hash(snapshot.root / "manifest.json"),
        "task_set_sha256": snapshot.manifest["task_set_sha256"],
        "scoring_version": SCORING_VERSION,
        "scoring_source_sha256": _hash(Path(__file__).with_name("scoring.py")),
        "source_sha256": _source_hashes(),
        **{name: _group_hash(paths) for name, paths in VERSION_GROUPS.items()},
        "git": _git_state(),
        "execution_mode": "offline_no_model",
        "online_executed": False,
        "model": None,
        "sampling_parameters": None,
        "provider": None,
        "retrieval_mode": retrieval_mode,
        "index_preparation": indexing.summary(index_report),
        "index_versions": [entry["index_version"] for entry
                           in (index_report or {}).get("indexes", [])],
        "context_mode": context_mode,
        "context_strategy": ("phase3_current_structured_context"
                             if context_mode == "phase3_current"
                             else "phase2_legacy_request_simulated_not_frozen"),
        "timeout_seconds": 120,
        "react_budget_default": {
            "max_model_rounds": 5, "max_tool_calls": 8, "max_tokens": 64000,
            "deadline_seconds": 120,
        },
        "workflow_budget": "not_implemented",
        "model_calls": 0,
        "network": "urlopen patched to fail closed",
        "capabilities": CAPABILITY_NOTES,
        "comparison_type": ("end_to_end_architecture_comparison_under_offline_no_model_conditions; "
                            "control_variables: snapshot, tasks, project scope, evidence DB, "
                            "retrieval mode, scorer, no provider credentials"),
    }


@contextmanager
def _context_mode(mode: str):
    """Phase 2 legacy request shape is an explicit simulation, not a frozen run."""
    if mode == "phase3_current":
        yield
        return
    from benchmark.v3.runner import legacy_request
    from src.context_budget import TokenCounter
    from src.investigation_context import InvestigationContext

    original = InvestigationContext.request

    def simulated(self, messages, ledger, trace, memories, coverage, max_bytes,
                  gaps=(), *, claims=(), question=None, session_context=None,
                  context_budget=None, model=""):
        request, stats = legacy_request(list(self.views.values()), messages, ledger, trace,
                                        coverage, gaps, max_bytes)
        counter = TokenCounter(model or "deepseek-chat", "estimate")
        usage = {
            "estimated_input_tokens": counter.count_messages(request),
            "input_bytes": len(json.dumps(request, ensure_ascii=False).encode("utf-8")),
            "context_schema_version": "phase2_legacy_simulated",
            "phase2_simulated": True,
            "omitted_excerpt_bodies": stats.get("omitted_excerpt_bodies", 0),
            "candidate_evidence_count": len(self.views),
            "selected_evidence_count": len(self.views),
            "deduped_evidence_count": len(self.views),
            "pruned_count": 0,
            "pruning_reasons": {},
            "important_conflicts_retained": 0,
        }
        return request, usage

    InvestigationContext.request = simulated
    try:
        yield
    finally:
        InvestigationContext.request = original


def _selected(snapshot: Snapshot, categories: list[str], task_ids: list[str],
              limit: int | None) -> list[dict[str, Any]]:
    tasks = list(snapshot.public_tasks)
    if categories:
        tasks = [task for task in tasks if task["category"] in categories]
    if task_ids:
        tasks = [task for task in tasks if task["task_id"] in set(task_ids)]
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


def run(output: Path, *, categories: list[str] | None = None, task_ids: list[str] | None = None,
        backends: list[str] | None = None, limit: int | None = None,
        retrieval_mode: str = "bm25", context_mode: str = "phase3_current",
        plan_only: bool = False) -> tuple[Path, dict[str, Any]]:
    snapshot = dataset.load()
    categories = categories or []
    task_ids = task_ids or []
    backends = backends or ["workflow", "react"]
    for backend in backends:
        if backend not in {"workflow", "react"}:
            raise ValueError(f"Unknown backend: {backend}")
    if retrieval_mode not in {"bm25", "dense", "hybrid"}:
        raise ValueError("retrieval_mode must be bm25, dense or hybrid")
    if context_mode not in {"phase3_current", "phase2_simulated"}:
        raise ValueError("context_mode must be phase3_current or phase2_simulated")
    tasks = _selected(snapshot, categories, task_ids, limit)
    if not tasks:
        raise ValueError("No tasks selected")
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite an existing report: {output}")
    index_required = retrieval_mode in {"dense", "hybrid"}
    if plan_only:
        return output, {"snapshot": summary(snapshot), "tasks": [task["task_id"] for task in tasks],
                        "backends": backends, "retrieval_mode": retrieval_mode,
                        "context_mode": context_mode, "execution_mode": "plan_only",
                        "index_preparation_required": index_required}
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    index_report: dict[str, Any] | None = None
    os.environ["LIVE_RETRIEVAL_MODE"] = retrieval_mode
    try:
        with tempfile.TemporaryDirectory(prefix="cma-v4-offline-") as temporary:
            template = materialize_template(snapshot, Path(temporary) / "template")
            if index_required:
                # Build real indexes on the read-only template before any clone
                # exists.  Clones share the versioned files while the template
                # directory stays alive for the duration of the run.
                index_report = indexing.prepare_indexes(template[0], snapshot)
            output.mkdir(parents=True, exist_ok=False)
            for task in tasks:
                label = snapshot.label(task["task_id"])
                failure = label.get("injected_failure")
                budget = label.get("budget_override")
                for backend in backends:
                    clone_root = Path(temporary) / "runs" / task["task_id"] / backend
                    databases = clone_databases(template, clone_root)
                    with _context_mode(context_mode):
                        raw = run_task(snapshot, task, backend, databases,
                                       injected_failure=failure, budget_override=budget)
                    if raw.get("mode") == "react" or raw.get("model_calls"):
                        raise RuntimeError(
                            "Offline runner refused a run that attempted a model call")
                    assert_requested_channel(raw, retrieval_mode, task["task_id"])
                    row = score_run(raw, task, label, evidence_db=databases[0])
                    rows.append(row)
    except (IndexPreparationError, RetrievalChannelError):
        # Never leave a partial report that could be mistaken for a successful
        # dense/hybrid comparison.  The directory did not exist before this call.
        shutil.rmtree(output, ignore_errors=True)
        raise
    execution_seconds = time.perf_counter() - started
    finished_at = datetime.now(timezone.utc)
    overall = aggregate(rows)
    by_backend = aggregate_by(rows, "backend")
    report = {
        "report_version": REPORT_VERSION,
        "run_id": str(uuid4()),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "execution_seconds": execution_seconds,
        "execution_mode": "offline_no_model",
        "online_executed": False,
        "snapshot": summary(snapshot),
        "config": configuration(snapshot, retrieval_mode=retrieval_mode,
                                context_mode=context_mode,
                                index_report=index_report),
        "selection": {"categories": categories, "task_ids": task_ids, "limit": limit,
                      "backends": backends, "tasks_executed": len(tasks),
                      "runs_executed": len(rows)},
        "aggregates": {
            "overall": overall,
            "by_backend": by_backend,
            "by_category": aggregate_by(rows, "category"),
            "by_difficulty": aggregate_by(rows, "difficulty"),
        },
        "failure_cases": diagnose(rows),
        "pending_grading": pending_grading(rows),
        "known_limitations": list(DEFAULT_LIMITATIONS),
    }
    _write_report(output, report, rows)
    return output, report


def _write_report(output: Path, report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (output / "config.json").write_text(
        json.dumps(report["config"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "failure_cases.json").write_text(
        json.dumps(report["failure_cases"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "pending_grading.json").write_text(
        json.dumps(report["pending_grading"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(_markdown(report), encoding="utf-8")


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Phase 4 Benchmark Offline Report", "",
        f"- run_id: `{report['run_id']}`",
        f"- snapshot: `{report['snapshot']['snapshot_id']}`",
        f"- task_set_version: `{report['snapshot']['task_set_version']}`",
        f"- execution: `{report['execution_mode']}` (no model, no network)",
        f"- retrieval_mode: `{report['config']['retrieval_mode']}`",
        f"- index preparation: `{report['config']['index_preparation']['status']}`",
        f"- runs: {report['selection']['runs_executed']}",
        f"- execution_seconds: {report['execution_seconds']:.2f}", "",
        "## Index preparation", "",
        "| project | index_version | chunks | verified |",
        "|---|---|---|---|",
    ]
    indexes = report["config"]["index_preparation"].get("indexes", [])
    for entry in indexes:
        lines.append(f"| {entry['project_id']} | `{entry['index_version']}` | "
                     f"{entry['chunk_count']} | {entry['verified']} |")
    if not indexes:
        lines.append("| - | not required | - | - |")
    lines.extend([
        "",
        "Dense/Hybrid runs abort if any retrieval event falls back to BM25 or lacks "
        "a verified `index_version`.", "",
        "## Aggregate metrics", "",
    ])
    lines.extend([
        "| metric | overall |",
        "|---|---|",
    ])
    for key in ("TaskSuccessRate", "EvidenceRecall@K", "CitationValidity", "FactAccuracy",
                "OptionalFactAccuracy", "ForbiddenInferenceRate",
                "AppropriateAbstentionRate", "ConflictDetectionRate", "DecisionStateAccuracy",
                "ToolFailureRecoveryRate", "false_refusal_rate", "unsupported_answer_rate",
                "average_tool_calls", "average_model_calls", "estimated_tokens",
                "actual_cost_usd"):
        value = report["aggregates"]["overall"].get(key)
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Metric coverage", "",
                  "| metric | measured / total runs |", "|---|---|"])
    coverage = report["aggregates"]["overall"].get("metric_coverage", {})
    for name, values in coverage.items():
        lines.append(f"| {name} | {values['measured']} / {values['total']} |")
    lines.extend(["", "## Termination reasons", "", "| reason | runs |", "|---|---|"])
    for reason, count in report["aggregates"]["overall"].get(
            "termination_reason_distribution", {}).items():
        lines.append(f"| {reason} | {count} |")
    lines.extend(["", "## By backend", "", "| backend | runs | TaskSuccessRate | EvidenceRecall@K | CitationValidity | FactAccuracy |", "|---|---|---|---|---|---|"])
    for backend, values in report["aggregates"]["by_backend"].items():
        lines.append(f"| {backend} | {values['count']} | {values['TaskSuccessRate']} | "
                     f"{values['EvidenceRecall@K']} | {values['CitationValidity']} | {values['FactAccuracy']} |")
    lines.extend(["", "## Known limitations", ""])
    lines.extend(f"- {item}" for item in report["known_limitations"])
    lines.extend(["", "TaskSuccessRate is null until independent human review completes. See pending_grading.json.", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "benchmark/v4/reports" /
                        ("offline-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") +
                         "-" + uuid4().hex[:8]))
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--backend", action="append", choices=["workflow", "react"], default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retrieval-mode", choices=["bm25", "dense", "hybrid"], default="bm25")
    parser.add_argument("--context-mode", choices=["phase3_current", "phase2_simulated"],
                        default="phase3_current")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    path, report = run(args.output, categories=args.category, task_ids=args.task,
                       backends=args.backend or None, limit=args.limit,
                       retrieval_mode=args.retrieval_mode, context_mode=args.context_mode,
                       plan_only=args.plan_only)
    if args.plan_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"report": str(path), "overall": report["aggregates"]["overall"],
                          "by_backend": {key: {"count": value["count"],
                                               "TaskSuccessRate": value["TaskSuccessRate"],
                                               "EvidenceRecall@K": value["EvidenceRecall@K"]}
                                         for key, value in report["aggregates"]["by_backend"].items()}},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
