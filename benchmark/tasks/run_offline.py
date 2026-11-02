"""Offline task-level evaluation: real backends, no model, no labels in runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from benchmark.dataset import ROOT as BENCHMARK_ROOT, load as load_dataset, materialize
from benchmark.runner import run_backend
from benchmark.tasks.scoring import (aggregate, configuration, diagnose, load_tasks,
                                     pending_grading, score_task, validate_tasks)


def _case(task, snapshot_id):
    # Only public question/scope fields cross the runtime boundary. Labels stay outside.
    return {
        "case_id": task.case_id,
        "category": task.category,
        "question": task.question,
        "project_id": task.project_id,
        "dataset_kind": task.dataset_kind,
        "corpus_snapshot_id": snapshot_id,
        "split": task.split,
    }


def run(output=None, backends=("workflow", "react"), task_ids=None, root=BENCHMARK_ROOT):
    data = load_dataset(root)
    tasks = load_tasks()
    validation = validate_tasks(tasks, data)
    selected = [task for task in tasks["public"].tasks if not task_ids or task.task_id in task_ids]
    if not selected:
        raise ValueError("No tasks selected")
    output = Path(output) if output else (Path(root) / "reports" /
        (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-task-offline"))
    output.mkdir(parents=True, exist_ok=False)
    records = {record.evidence_id: record for record in data["records"]}
    rows = []
    with tempfile.TemporaryDirectory(prefix="cma-task-offline-") as temporary:
        for task in selected:
            label = tasks["labels"].labels[task.task_id]
            for backend in backends:
                runtime = materialize(data, Path(temporary) / f"{task.task_id}-{backend}")
                failure = label.injected_failure.model_dump() if label.injected_failure else None
                raw = run_backend(_case(task, validation["corpus_snapshot_id"]), runtime, backend, failure)
                row = score_task(raw, task, label, records, execution_mode="offline_no_model",
                                 supported_capabilities=set())
                row["diagnosis"] = diagnose(row)
                rows.append(row)
    summary = {
        "report_type": "offline_task_eval_no_model",
        "online_executed": False,
        "model_calls": 0,
        "manual_grades": 0,
        "config": configuration(data, validation, execution_mode="offline_no_model",
                                params={"backends": list(backends), "task_ids": [t.task_id for t in selected],
                                        "offline_fallback_only": True}),
        "validation": validation,
        "execution": {
            "tasks": len(selected),
            "backends": list(backends),
            "rows": len(rows),
            "runtime_errors": sum(row["status"] == "runtime_error" for row in rows),
        },
        "metrics": aggregate(rows),
        "diagnosis_counts": {
            hypothesis: sum(hypothesis in row["diagnosis"]["hypotheses"] for row in rows)
            for hypothesis in sorted({item for row in rows for item in row["diagnosis"]["hypotheses"]})
        },
        "warnings": [
            "Offline adapters are not a real model result.",
            "TaskSuccessRate and AppropriateAbstentionRate stay null without explicit human grades.",
            "Deterministic fact coverage is lexical marker matching, not semantic correctness.",
            "Capability tasks are marked unsupported_offline, but operational signals are still reported for diagnosis.",
        ],
    }
    (output / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                          encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "pending_grading.json").write_text(
        json.dumps(pending_grading(rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new report directory; must not exist")
    parser.add_argument("--backend", action="append", choices=["workflow", "react"], dest="backends",
                        help="repeatable; default runs both")
    parser.add_argument("--task", action="append", dest="task_ids", help="repeatable task_id")
    parser.add_argument("--root", type=Path, default=BENCHMARK_ROOT)
    args = parser.parse_args()
    path, summary = run(args.output, tuple(args.backends or ("workflow", "react")), args.task_ids, args.root)
    print(json.dumps({"report": str(path), **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
