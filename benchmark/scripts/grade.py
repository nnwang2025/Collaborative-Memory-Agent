"""Apply explicit human assessments to an existing report, without rerunning agents."""
import hashlib
import json
from pathlib import Path

from benchmark.dataset import ROOT, load
from benchmark.metrics import aggregate
from benchmark.runner import score


def apply_grades(report, grades_file, output, root=ROOT):
    report, grades_file, output = Path(report), Path(grades_file), Path(output)
    data = load(root)
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((data["root"] / "manifest.json").read_bytes()).hexdigest()
    if summary["config"]["manifest_sha256"] != digest:
        raise ValueError("Report and grading corpus manifests differ")
    rows = [json.loads(line) for line in (report / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    by_run = {row["run_id"]: row for row in rows}
    if len(by_run) != len(rows): raise ValueError("Report contains duplicate run IDs")
    grades = json.loads(grades_file.read_text(encoding="utf-8"))
    if not isinstance(grades, list): raise ValueError("Grades must be a list of human assessments")
    seen = set()
    cases = {case["case_id"]: case for case in data["cases"]}
    for grade in grades:
        identifier = grade["run_id"]
        if identifier in seen or identifier not in by_run: raise ValueError("Duplicate or unknown assessment run ID")
        seen.add(identifier)
        raw = by_run[identifier]
        if (grade["case_id"], grade["backend"]) != (raw["case_id"], raw["backend"]):
            raise ValueError("Assessment is for a different case/backend")
        if not isinstance(grade.get("reviewer"), str) or not grade["reviewer"].strip():
            raise ValueError("Human reviewer must be identified")
        if type(grade.get("task_success")) is not bool:
            raise ValueError("Human task_success must be an explicit boolean")
        if raw["status"] == "unsupported": raise ValueError("Unsupported execution cannot be graded as task success")
        by_run[identifier] = score(raw, cases[raw["case_id"]], data["labels"][raw["case_id"]], grade)
    updated = [by_run[row["run_id"]] for row in rows]
    summary["backends"] = {backend: aggregate([r for r in updated if r["backend"] == backend]) for backend in ("workflow", "react")}
    summary["human_assessment"] = {"source_report": str(report.resolve()), "assessment_count": len(grades),
        "grades_sha256": hashlib.sha256(grades_file.read_bytes()).hexdigest(),
        "grader_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    output.mkdir(parents=True, exist_ok=False)
    (output / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in updated), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--grades", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = apply_grades(args.report, args.grades, args.output, args.root)
    print(json.dumps(result["human_assessment"], ensure_ascii=False, indent=2))
