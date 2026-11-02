"""Attach explicit human grades to an offline Phase 4 report.

Automatic checks never decide TaskSuccess.  This tool only merges grades that a
named reviewer supplied as an explicit boolean for each executed
(backend, task_id) run.  It writes a separate graded directory and never
rewrites the original report.

Grade file (JSON list, or {"grades": [...]}) fields:
    task_id, backend, reviewer, task_success, notes (optional)
    appropriate_abstention / semantic_support / conflict_interpretation /
    decision_interpretation (optional structured notes)

Usage:
    python -m benchmark.v4.grade --template --report benchmark/v4/reports/<run>
    python -m benchmark.v4.grade --report benchmark/v4/reports/<run> --grades grades.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import run_offline
from .scoring import (SCORING_VERSION, aggregate, aggregate_by, apply_manual_grades,
                      diagnose, pending_grading)

GRADING_VERSION = "cma-v4-grading-v1"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_report(source: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = Path(source)
    summary_path = source / "summary.json"
    results_path = source / "results.jsonl"
    if not summary_path.exists() or not results_path.exists():
        raise FileNotFoundError(f"Not a Phase 4 report directory: {source}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if summary.get("report_version") != run_offline.REPORT_VERSION:
        raise ValueError("Unsupported report_version")
    return summary, rows


def load_grades(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("grades")
    if not isinstance(payload, list):
        raise ValueError("Grade file must be a JSON list or {\"grades\": [...]}")
    return payload


def template(report: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "grading_version": GRADING_VERSION,
        "scoring_version": SCORING_VERSION,
        "report": report.get("run_id"),
        "instructions": ("Set task_success to an explicit true/false after reading the answer, "
                         "citations and automatic signals. Use the backend/task_id pair exactly. "
                         "Semantic support must be judged, not inferred from citation integrity."),
        "grades": [{"task_id": row["task_id"], "backend": row["backend"],
                    "reviewer": "", "task_success": None, "notes": ""}
                   for row in rows],
    }


def run(source: Path, grades_path: Path | None, output: Path | None = None
        ) -> tuple[Path, dict[str, Any]]:
    summary, rows = load_report(source)
    if grades_path is None:
        raise ValueError("A grade file is required (use --template to export one)")
    grades = load_grades(grades_path)
    if not grades:
        raise ValueError("Grade file contains no grades")
    rows = apply_manual_grades(rows, grades)
    output = Path(output) if output else Path(source).parent / (
        "graded-" + Path(source).name + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite an existing report: {output}")
    output.mkdir(parents=True, exist_ok=False)
    overall = aggregate(rows)
    report = dict(summary)
    report["grading"] = {
        "grading_version": GRADING_VERSION,
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(Path(source)),
        "source_summary_sha256": _hash(Path(source) / "summary.json"),
        "source_results_sha256": _hash(Path(source) / "results.jsonl"),
        "grade_file": str(Path(grades_path)),
        "grade_file_sha256": _hash(Path(grades_path)),
        "reviewers": sorted({grade["reviewer"] for grade in grades}),
        "graded_runs": sum(1 for row in rows if row.get("manual") is not None),
        "task_success_rate_status": overall.get("TaskSuccessRate_status"),
    }
    report["aggregates"] = {
        "overall": overall,
        "by_backend": aggregate_by(rows, "backend"),
        "by_category": aggregate_by(rows, "category"),
        "by_difficulty": aggregate_by(rows, "difficulty"),
    }
    report["failure_cases"] = diagnose(rows)
    report["pending_grading"] = pending_grading(rows)
    report["known_limitations"] = list(summary.get("known_limitations", [])) + [
        "Manual grades are reviewer judgments; inter-rater reliability was not measured.",
        "Only completed explicit grades can raise TaskSuccessRate above null.",
    ]
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (output / "grades.json").write_text(
        json.dumps({"grades": grades}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(run_offline._markdown(report), encoding="utf-8")
    return output, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--grades", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--template", action="store_true",
                        help="print an unfilled grade template and exit")
    args = parser.parse_args()
    summary, rows = load_report(args.report)
    if args.template:
        print(json.dumps(template(summary, rows), ensure_ascii=False, indent=2))
        return
    path, report = run(args.report, args.grades, args.output)
    print(json.dumps({"report": str(path), "TaskSuccessRate":
                      report["aggregates"]["overall"].get("TaskSuccessRate"),
                      "TaskSuccessRate_status":
                      report["aggregates"]["overall"].get("TaskSuccessRate_status")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
