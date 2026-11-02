"""Manifest validation and isolated runtime materialization, never model-facing labels."""
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore
from src.memory.decisions import DecisionStore

ROOT = Path(__file__).resolve().parent
CATEGORIES = ("single_source", "cross_source", "multi_round", "decision_history", "conflict", "insufficient", "tool_failure")


def load(root=ROOT):
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    expected = set(manifest["files"])
    actual = {p.relative_to(root).as_posix() for name in ("corpus", "cases", "labels") for p in (root / name).rglob("*") if p.is_file()}
    if expected != actual: raise ValueError("Manifest file inventory mismatch")
    for relative, check in manifest["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or (root / relative).is_symlink():
            raise ValueError("Snapshot path escaped the dataset")
        blob = path.read_bytes()
        if len(blob) != check["bytes"] or hashlib.sha256(blob).hexdigest() != check["sha256"]:
            raise ValueError("Snapshot hash mismatch: " + relative)
    corpus_hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
                     for p in sorted((root / "corpus").iterdir())}
    snapshot_id = "cma-v1-" + hashlib.sha256(json.dumps(corpus_hashes, sort_keys=True).encode()).hexdigest()[:16]
    if manifest["snapshot_id"] != snapshot_id:
        raise ValueError("Corpus snapshot ID mismatch")
    records = [Evidence.model_validate(r) for r in json.loads((root / "corpus/evidence.json").read_text(encoding="utf-8"))]
    cases = json.loads((root / "cases/public.json").read_text(encoding="utf-8"))
    labels = json.loads((root / "labels/gold.json").read_text(encoding="utf-8"))
    return {"root": root, "manifest": manifest, "records": records, "cases": cases,
            "labels": {r["case_id"]: r for r in labels},
            "relations": json.loads((root / "corpus/relations.json").read_text(encoding="utf-8"))}


def materialize(data, destination):
    """Only corpus records enter the runtime directory; cases and labels stay outside."""
    directory = Path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    evidence_path, memory_path = directory / "evidence.sqlite3", directory / "decisions.sqlite3"
    if evidence_path.exists() or memory_path.exists():
        raise FileExistsError("Each run needs a new isolated runtime directory")
    EvidenceStore(evidence_path).append(data["records"])
    with closing(sqlite3.connect(memory_path)) as connection:
        def authorize(action, first, second, db, trigger):
            if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH): return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        connection.executescript((data["root"] / "corpus/decisions.sql").read_text(encoding="utf-8"))
    return evidence_path, memory_path


def validate(data, directory):
    evidence_path, memory_path = materialize(data, directory)
    memory = DecisionStore(memory_path, evidence_db_path=evidence_path)
    records = {r.evidence_id: r for r in data["records"]}
    if len(records) != len(data["records"]): raise ValueError("Duplicate evidence identity in snapshot")
    ids = [c["case_id"] for c in data["cases"]]
    if len(ids) != len(set(ids)) or set(ids) != set(data["labels"]):
        raise ValueError("Case/label IDs mismatch")
    counts = {name: 0 for name in CATEGORIES}
    for case in data["cases"]:
        label = data["labels"][case["case_id"]]
        if case["corpus_snapshot_id"] != data["manifest"]["snapshot_id"]:
            raise ValueError("Case snapshot mismatch")
        if case["category"] not in CATEGORIES or case["split"] not in {"dev", "test"}: raise ValueError("Invalid case category/split")
        if not all(k in label for k in ("expected_behavior", "gold_evidence_ids", "expected_termination", "grading_notes", "annotation_status")):
            raise ValueError("Incomplete label")
        for identifier in label["gold_evidence_ids"]:
            record = records.get(identifier)
            if record is None or (record.project_id, record.dataset_kind) != (case["project_id"], case["dataset_kind"]):
                raise ValueError("Gold evidence missing or out of scope")
            if label["gold_content_hashes"].get(identifier) != record.content_hash:
                raise ValueError("Gold content hash mismatch")
        if "decision_id" in label:
            snapshot = memory.snapshot(label["decision_id"], EvidenceScope(project_id=case["project_id"], dataset_kind=case["dataset_kind"]), known_at=label.get("known_at"))
            versions = {v["version_id"]: v for v in snapshot["versions"]}
            if not set(label.get("gold_decision_version_ids", [])).issubset(versions): raise ValueError("Gold version missing")
            current = (snapshot["current_confirmed"] or {}).get("version_id")
            if current != label.get("expected_current_version_id"): raise ValueError("Gold current version mismatch")
            for version, previous in label.get("expected_supersedes", {}).items():
                if versions[version]["supersedes_version_id"] != previous: raise ValueError("Supersedes label mismatch")
            if label.get("no_confirmed_version") and (current or not any(p["review_status"] == "pending" for p in snapshot["proposals"])):
                raise ValueError("No-confirmation label is inconsistent with review history")
        if case["category"] == "conflict":
            relation = next((r for r in data["relations"] if r["project_id"] == case["project_id"] and r["field"] == label["conflict_field"]), None)
            if not relation or set(relation["evidence_ids"]) != set(label["gold_evidence_ids"]): raise ValueError("Conflict relation missing")
            values = [records[i].metadata["assertions"][relation["field"]] for i in relation["evidence_ids"]]
            if values[0] == values[1]: raise ValueError("Conflict has identical values")
        if label.get("missing_fact"):
            if any(label["missing_fact"] in r.metadata.get("assertions", {}) for r in data["records"] if r.project_id == case["project_id"]):
                raise ValueError("Missing-fact label contradicts corpus")
        if case["split"] == "test": counts[case["category"]] += 1
    if any(n < 2 for n in counts.values()): raise ValueError("Need at least two test cases per category")
    return {"snapshot_id": data["manifest"]["snapshot_id"], "evidence_records": len(records),
            "test_cases": sum(counts.values()), "category_counts": counts,
            "dev_cases": sum(c["split"] == "dev" for c in data["cases"]),
            "label_status": "structurally_valid_pending_human_review"}


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory(prefix="cma-label-check-") as directory:
        print(json.dumps(validate(load(), directory), ensure_ascii=False, indent=2))
