"""Apply explicit human task grades to an existing task report without rerunning."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmark.tasks.scoring import aggregate, load_tasks, pending_grading


def _load_grades(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    grades = data.get("grades") if isinstance(data, dict) else data
    if not isinstance(grades, list):
        raise ValueError("Grades file must contain a list or {'grades': [...]}")
    keyed = {}
    for grade in grades:
        if not isinstance(grade, dict):
            raise ValueError("Each grade must be an object")
        key = (grade.get("task_id"), grade.get("backend"))
        if not all(key):
            raise ValueError("Each grade needs task_id and backend")
        if key in keyed:
            raise ValueError("Duplicate grade for " + "/".join(key))
        keyed[key] = grade
    return keyed


def apply_grade(row, label, grade):
    if row["status"] in {"unsupported", "unsupported_offline", "runtime_error", "execution_error",
                         "offline_fallback_not_real_model"}:
        raise ValueError("Cannot assign task success to an unsupported/runtime-error/offline-fallback row")
    if not grade.get("reviewer"):
        raise ValueError("Manual grade requires a reviewer")
    if type(grade.get("task_success")) is not bool:
        raise ValueError("Manual grade requires explicit boolean task_success")
    abstention = grade.get("appropriate_abstention")
    if abstention is not None and type(abstention) is not bool:
        raise ValueError("appropriate_abstention must be boolean or null")
    recovery = grade.get("tool_failure_recovery")
    if recovery is not None and type(recovery) is not bool:
        raise ValueError("tool_failure_recovery must be boolean or null")
    if abstention is not None and not label.abstention.required:
        raise ValueError("appropriate_abstention is only applicable to abstention tasks")
    if recovery is not None and not label.injected_failure:
        raise ValueError("tool_failure_recovery is only applicable to tool-failure tasks")
    updated = {**row, "status": "graded", "task_success": grade["task_success"],
               "human_appropriate_abstention": abstention if label.abstention.required else None,
               "human_tool_failure_recovery": recovery if label.injected_failure else None,
               "semantic_correctness": "graded",
               "human_review": {"reviewer": grade["reviewer"], "notes": grade.get("notes", "")}}
    return updated


def run(report, grades_path, output):
    report, output = Path(report), Path(output)
    if output.exists():
        raise FileExistsError("Output report exists; choose a new directory")
    if not output.parent.is_dir():
        raise FileNotFoundError("Create the output parent directory first")
    rows = [json.loads(line) for line in (report / "results.jsonl").read_text(encoding="utf-8").splitlines() if line]
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    tasks = load_tasks()
    labels = tasks["labels"].labels
    grades = _load_grades(grades_path)
    known = {(row["task_id"], row["backend"]) for row in rows}
    unknown = set(grades) - known
    if unknown:
        raise ValueError("Grade targets not present in report: " + ", ".join("/".join(item) for item in sorted(unknown)))
    graded = 0
    for index, row in enumerate(rows):
        grade = grades.get((row["task_id"], row["backend"]))
        if grade is not None:
            rows[index] = apply_grade(row, labels[row["task_id"]], grade)
            graded += 1
    output.mkdir(parents=True, exist_ok=False)
    (output / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                          encoding="utf-8")
    updated = {
        **summary,
        "status": "graded_complete" if all(row["status"] == "graded" for row in rows) else "graded_partial",
        "manual_grades": sum(row["status"] == "graded" for row in rows),
        "grades_applied_this_run": graded,
        "metrics": aggregate(rows),
        "grading_source": str(report),
        "grading_file": str(grades_path),
        "warnings": [warning for warning in summary.get("warnings", []) if "manual-only" not in warning] + [
            "TaskSuccessRate remains null for ungraded rows; it is never inferred from metrics.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "pending_grading.json").write_text(
        json.dumps(pending_grading(rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output, updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--grades", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path, summary = run(args.report, args.grades, args.output)
    print(json.dumps({"report": str(path), **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
