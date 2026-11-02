"""Retrieval / context ablations over one frozen Phase 4 snapshot.

Each arm is a separate offline report directory produced by
``benchmark.v4.run_offline``.  The arms share the snapshot, task set, scorer and
project scope; only the retrieval mode or the context construction strategy
changes.  A Dense/Hybrid arm that cannot use an index reports
``effective_mode = bm25_fallback`` rather than pretending the requested mode
ran.  The Phase 2 context arm is an explicit request-shape simulation, not a
replay of a frozen historical report.

Usage:
    python -m benchmark.v4.ablations --output benchmark/v4/reports/ablations-<id>
    python -m benchmark.v4.ablations --arms bm25_phase3,bm25_phase2_simulated
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import dataset, run_offline
from .dataset import summary

ROOT = Path(__file__).resolve().parents[2]
ABLATION_VERSION = "cma-v4-ablation-v1"

ARMS: dict[str, dict[str, str]] = {
    "bm25_phase3": {"retrieval_mode": "bm25", "context_mode": "phase3_current",
                    "label": "BM25 + Phase 3 structured context"},
    "dense_phase3": {"retrieval_mode": "dense", "context_mode": "phase3_current",
                     "label": "Dense + Phase 3 structured context"},
    "hybrid_phase3": {"retrieval_mode": "hybrid", "context_mode": "phase3_current",
                      "label": "Hybrid + Phase 3 structured context"},
    "bm25_phase2_simulated": {
        "retrieval_mode": "bm25", "context_mode": "phase2_simulated",
        "label": "BM25 + Phase 2 legacy request shape (simulated)"},
    "hybrid_phase2_simulated": {
        "retrieval_mode": "hybrid", "context_mode": "phase2_simulated",
        "label": "Hybrid + Phase 2 legacy request shape (simulated)"},
}
DEFAULT_ARMS = ["bm25_phase3", "dense_phase3", "hybrid_phase3", "bm25_phase2_simulated"]
LIMITATIONS = [
    "Arms may be run separately; a partial matrix is reported as partial, never imputed.",
    "Dense/Hybrid arms build and verify real FAISS indexes; if any run falls back to BM25 the arm aborts instead of reporting a mislabelled result.",
    "The Phase 2 context arm rebuilds the legacy request shape around the current ledger; it is a simulation, not a frozen Phase 2 rerun.",
    "Offline runs have no model loop, so differences measure retrieval/context plumbing, not semantic reasoning quality.",
    "TaskSuccessRate stays null pending independent human review.",
]


def _rows(path: Path) -> list[dict[str, Any]]:
    text = (path / "results.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _retrieval_observation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    requested, effective, fallbacks, versions = set(), set(), set(), set()
    for row in rows:
        retrieval = row.get("raw", {}).get("retrieval", {})
        requested.update(retrieval.get("requested_modes", []))
        effective.update(retrieval.get("effective_modes", []))
        fallbacks.update(retrieval.get("fallback_reasons", []))
        versions.update(retrieval.get("index_versions", []))
    return {
        "requested_modes": sorted(requested),
        "effective_modes": sorted(effective),
        "fallback_reasons": sorted(fallbacks),
        "index_versions": sorted(versions),
        "fallback_used": any(mode.endswith("fallback") for mode in effective),
    }


def _arm_entry(name: str, report: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    retrieval = _retrieval_observation(rows)
    spec = ARMS[name]
    requested = spec["retrieval_mode"]
    if not rows:
        status = "no_runs"
    elif requested in {"dense", "hybrid"}:
        if retrieval["fallback_used"]:
            status = "executed_with_bm25_fallback_not_" + requested
        elif (retrieval["effective_modes"] == [requested]
              and retrieval["index_versions"]):
            status = "executed_requested_configuration"
        else:
            status = "incomplete_requested_channel"
    else:
        status = "executed_requested_configuration"
    return {
        "arm": name,
        "label": spec["label"],
        "retrieval_mode": requested,
        "context_mode": spec["context_mode"],
        "context_strategy": report["config"]["context_strategy"],
        "report": str(Path(report.get("_path", ""))),
        "snapshot_id": report["snapshot"]["snapshot_id"],
        "task_set_version": report["snapshot"]["task_set_version"],
        "runs": len(rows),
        "arm_status": status,
        "index_preparation": report["config"].get("index_preparation"),
        "retrieval_observed": retrieval,
        "aggregates": report["aggregates"],
        "execution_seconds": report["execution_seconds"],
    }


def run(output: Path, *, arms: list[str] | None = None,
        categories: list[str] | None = None, task_ids: list[str] | None = None,
        backends: list[str] | None = None, limit: int | None = None,
        plan_only: bool = False) -> tuple[Path, dict[str, Any]]:
    snapshot = dataset.load()
    output = Path(output)
    selected = arms or list(DEFAULT_ARMS)
    unknown = [arm for arm in selected if arm not in ARMS]
    if unknown:
        raise ValueError(f"Unknown ablation arm(s): {', '.join(unknown)}")
    if len(set(selected)) != len(selected):
        raise ValueError("Ablation arms must be unique")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite an existing report: {output}")
    plan = {
        "ablation_version": ABLATION_VERSION,
        "snapshot": summary(snapshot),
        "arms": [{"arm": name, **ARMS[name],
                  "index_preparation_required": ARMS[name]["retrieval_mode"] in {"dense", "hybrid"}}
                 for name in selected],
        "selection": {"categories": categories or [], "task_ids": task_ids or [],
                      "backends": backends or ["workflow", "react"], "limit": limit},
        "execution_mode": "plan_only" if plan_only else "offline_no_model",
        "matrix_completeness": ("partial" if len(selected) < len(ARMS) else "all_arms_selected"),
    }
    if plan_only:
        return output, plan
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(timezone.utc)
    entries = []
    for name in selected:
        spec = ARMS[name]
        arm_output = output / "arms" / name
        _, report = run_offline.run(
            arm_output, categories=categories, task_ids=task_ids, backends=backends,
            limit=limit, retrieval_mode=spec["retrieval_mode"],
            context_mode=spec["context_mode"])
        rows = _rows(arm_output)
        report["_path"] = arm_output
        entries.append(_arm_entry(name, report, rows))
    index = {
        "ablation_version": ABLATION_VERSION,
        "run_id": str(uuid4()),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "offline_no_model",
        "online_executed": False,
        "snapshot": summary(snapshot),
        "selection": plan["selection"],
        "matrix_completeness": ("partial" if len(selected) < len(ARMS) else "all_arms_selected"),
        "arms": entries,
        "known_limitations": list(LIMITATIONS),
    }
    (output / "ablation.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "ablation.md").write_text(_markdown(index), encoding="utf-8")
    return output, index


def _markdown(index: dict[str, Any]) -> str:
    lines = [
        "# Phase 4 Retrieval / Context Ablations", "",
        f"- run_id: `{index['run_id']}`",
        f"- snapshot: `{index['snapshot']['snapshot_id']}`",
        f"- matrix: `{index['matrix_completeness']}`",
        "- execution: offline, no model, no network", "",
        "| arm | retrieval | context | runs | status | EvidenceRecall@K | FactAccuracy |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in index["arms"]:
        overall = arm["aggregates"]["overall"]
        lines.append(
            f"| {arm['arm']} | {arm['retrieval_mode']} | {arm['context_mode']} | "
            f"{arm['runs']} | {arm['arm_status']} | "
            f"{overall.get('EvidenceRecall@K')} | {overall.get('FactAccuracy')} |")
    lines.extend(["", "## Observed retrieval modes", ""])
    for arm in index["arms"]:
        observed = arm["retrieval_observed"]
        lines.append(f"- `{arm['arm']}` requested {observed['requested_modes']}, "
                     f"effective {observed['effective_modes']}, "
                     f"fallback {observed['fallback_reasons'] or 'none'}, "
                     f"index_versions {observed['index_versions'] or 'none'}")
    lines.extend(["", "## Index preparation", "", "| arm | status | projects | chunks |", "|---|---|---|---|"])
    for arm in index["arms"]:
        preparation = arm.get("index_preparation") or {}
        lines.append(f"| {arm['arm']} | {preparation.get('status')} | "
                     f"{preparation.get('index_count', 0)} | "
                     f"{preparation.get('chunk_count', 0)} |")
    lines.extend(["", "## Known limitations", ""])
    lines.extend(f"- {item}" for item in index["known_limitations"])
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "benchmark/v4/reports" /
                        ("ablations-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") +
                         "-" + uuid4().hex[:8]))
    parser.add_argument("--arm", action="append", default=[],
                        help=f"repeatable; choose from {', '.join(ARMS)}")
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--backend", action="append", choices=["workflow", "react"], default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    path, index = run(args.output, arms=args.arm or None, categories=args.category,
                      task_ids=args.task, backends=args.backend or None,
                      limit=args.limit, plan_only=args.plan_only)
    if args.plan_only:
        print(json.dumps(index, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"report": str(path), "arms": [
            {"arm": arm["arm"], "status": arm["arm_status"],
             "EvidenceRecall@K": arm["aggregates"]["overall"].get("EvidenceRecall@K"),
             "FactAccuracy": arm["aggregates"]["overall"].get("FactAccuracy")}
            for arm in index["arms"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
