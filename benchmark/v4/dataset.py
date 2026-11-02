"""Frozen snapshot loading, validation and deterministic materialization.

The evaluator verifies the manifest before touching SQLite.  A snapshot is
immutable: ``load`` never regenerates it, and a hash mismatch is an error, not
a warning.  Runtime materialization is isolated per task via file copies of a
read-only template so one task cannot contaminate another.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore, bump_revision

V4_ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = V4_ROOT / "dataset"
REQUIRED_FILES = ("records.json", "decisions.sql", "tasks/public.json", "tasks/gold.json")
CATEGORIES = ("single_source", "cross_source", "multi_step", "decision_history",
              "conflict", "insufficient", "budget", "tool_failure", "multi_turn")
PUBLIC_FIELDS = {"task_id", "category", "project_id", "dataset_kind", "split",
                 "difficulty", "question", "task_set_version", "session_id", "turns"}
FORBIDDEN_PUBLIC_FIELDS = {"required_evidence_groups", "expected_facts", "known_at",
                           "expected_decision_state", "expected_conflicts",
                           "expected_abstention", "expected_limitations",
                           "scoring_rubric", "acceptable_alternative_evidence"}
STRUCTURED_FACT_TYPES = {"component", "version", "commit", "issue_id", "date",
                         "decision_state", "person"}


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


@dataclass(frozen=True)
class Snapshot:
    root: Path
    manifest: dict[str, Any]
    records: tuple[Evidence, ...]
    public_tasks: tuple[dict[str, Any], ...]
    gold_labels: dict[str, dict[str, Any]]

    @property
    def snapshot_id(self) -> str:
        return self.manifest["snapshot_id"]

    @property
    def task_set_version(self) -> str:
        return self.manifest["task_set_version"]

    @property
    def dataset_version(self) -> str:
        return self.manifest["dataset_version"]

    def task(self, task_id: str) -> dict[str, Any]:
        for task in self.public_tasks:
            if task["task_id"] == task_id:
                return task
        raise KeyError(task_id)

    def label(self, task_id: str) -> dict[str, Any]:
        return self.gold_labels[task_id]


class SnapshotError(ValueError):
    """The frozen snapshot or its labels are inconsistent."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load(root: Path | str = DEFAULT_DATASET) -> Snapshot:
    root = Path(root)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise SnapshotError(f"Missing manifest: {manifest_path}")
    manifest = _read_json(manifest_path)
    for relative in REQUIRED_FILES:
        path = root / relative
        if not path.exists():
            raise SnapshotError(f"Missing snapshot file: {relative}")
        blob = path.read_bytes()
        expected = manifest.get("files", {}).get(relative, {})
        if len(blob) != expected.get("bytes") or sha256_bytes(blob) != expected.get("sha256"):
            raise SnapshotError(f"Snapshot hash mismatch: {relative}")
    records = tuple(Evidence.model_validate(item) for item in _read_json(root / "records.json"))
    if len(records) != manifest.get("record_count"):
        raise SnapshotError("Record count does not match manifest")
    if len({record.evidence_id for record in records}) != manifest.get("evidence_identity_count"):
        raise SnapshotError("Evidence identity count does not match manifest")
    public_doc = _read_json(root / "tasks/public.json")
    gold_doc = _read_json(root / "tasks/gold.json")
    task_bytes = (root / "tasks/public.json").read_bytes() + b"\0" + (root / "tasks/gold.json").read_bytes()
    if sha256_bytes(task_bytes) != manifest.get("task_set_sha256"):
        raise SnapshotError("Task set hash does not match manifest")
    if public_doc.get("task_set_version") != manifest.get("task_set_version"):
        raise SnapshotError("Public task set version mismatch")
    if gold_doc.get("task_set_version") != manifest.get("task_set_version"):
        raise SnapshotError("Gold task set version mismatch")
    snapshot = Snapshot(root=root, manifest=manifest, records=records,
                        public_tasks=tuple(public_doc["tasks"]),
                        gold_labels=gold_doc["labels"])
    validate(snapshot)
    return snapshot


def _all_refs(label: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for group in label["required_evidence_groups"]:
        yield from group["any_of"]
    for conflict in label["expected_conflicts"]:
        for ref in conflict.get("evidence_refs", []):
            yield ref


def validate(snapshot: Snapshot) -> None:
    """Validate structure, label rules and referential integrity.

    These checks are deliberately label-side.  They cannot tell whether the
    synthetic story is interesting; that is what independent human review is
    for.  They do catch a broken snapshot before an expensive run starts.
    """
    records = snapshot.records
    by_identity = {(record.project_id, record.dataset_kind, record.evidence_id): record
                   for record in records}
    if len(snapshot.public_tasks) < 40 or len(snapshot.public_tasks) > 60:
        raise SnapshotError("Phase 4 task count must be between 40 and 60")
    task_ids = [task["task_id"] for task in snapshot.public_tasks]
    if len(task_ids) != len(set(task_ids)):
        raise SnapshotError("Duplicate task_id in public tasks")
    counts = {category: 0 for category in CATEGORIES}
    for task in snapshot.public_tasks:
        if task["category"] not in counts:
            raise SnapshotError(f"Unknown category: {task['category']}")
        counts[task["category"]] += 1
        if task.get("dataset_kind") != "live":
            raise SnapshotError(f"{task['task_id']}: dataset_kind must be live")
        if not task.get("question"):
            raise SnapshotError(f"{task['task_id']}: empty question")
        leaked = FORBIDDEN_PUBLIC_FIELDS.intersection(task)
        if leaked:
            raise SnapshotError(f"{task['task_id']}: public task leaks labels {sorted(leaked)}")
        if not set(task).issubset(PUBLIC_FIELDS):
            raise SnapshotError(f"{task['task_id']}: unexpected public field")
        if task["task_id"] not in snapshot.gold_labels:
            raise SnapshotError(f"{task['task_id']}: missing Gold label")
    if any(count < 5 for count in counts.values()):
        raise SnapshotError(f"Each category needs at least 5 tasks: {counts}")

    for task in snapshot.public_tasks:
        label = snapshot.gold_labels[task["task_id"]]
        if label["task_id"] != task["task_id"]:
            raise SnapshotError(f"{task['task_id']}: Gold task_id mismatch")
        if label["category"] != task["category"] or label["project_id"] != task["project_id"]:
            raise SnapshotError(f"{task['task_id']}: Gold scope mismatch")
        if label.get("difficulty") not in {"easy", "medium", "hard"}:
            raise SnapshotError(f"{task['task_id']}: invalid difficulty")
        if label.get("task_set_version") != snapshot.task_set_version:
            raise SnapshotError(f"{task['task_id']}: task set version mismatch")
        if not label.get("question"):
            raise SnapshotError(f"{task['task_id']}: Gold question missing")
        if label.get("annotation_status") != "synthetic_authored_pending_independent_human_review":
            raise SnapshotError(f"{task['task_id']}: annotation status must disclose synthetic authoring")
        if not label.get("required_evidence_groups"):
            raise SnapshotError(f"{task['task_id']}: no required evidence groups")
        for group in label["required_evidence_groups"]:
            if not group.get("group_id") or not group.get("any_of"):
                raise SnapshotError(f"{task['task_id']}: malformed evidence group")
            for ref in group["any_of"]:
                key = (task["project_id"], "live", ref.get("evidence_id"))
                record = by_identity.get(key)
                if record is None:
                    raise SnapshotError(f"{task['task_id']}: unknown evidence {ref.get('evidence_id')}")
                if ref.get("content_hash") != record.content_hash:
                    raise SnapshotError(f"{task['task_id']}: content hash mismatch for {record.evidence_id}")
                if record.metadata.get("benchmark_lifecycle") in {"deleted", "orphaned"}:
                    raise SnapshotError(f"{task['task_id']}: Gold cites non-current evidence")
        group_ids = {group["group_id"] for group in label["required_evidence_groups"]}
        for alternative in label.get("acceptable_alternative_evidence", []):
            if alternative.get("group_id") not in group_ids or not alternative.get("any_of"):
                raise SnapshotError(f"{task['task_id']}: malformed alternative evidence")
            for ref in alternative["any_of"]:
                key = (task["project_id"], "live", ref.get("evidence_id"))
                record = by_identity.get(key)
                if record is None:
                    raise SnapshotError(
                        f"{task['task_id']}: unknown alternative evidence {ref.get('evidence_id')}")
                if ref.get("content_hash") != record.content_hash:
                    raise SnapshotError(
                        f"{task['task_id']}: alternative content hash mismatch "
                        f"for {record.evidence_id}")
                if record.metadata.get("benchmark_lifecycle") in {"deleted", "orphaned"}:
                    raise SnapshotError(f"{task['task_id']}: Gold cites non-current alternative")
        if label.get("known_at") is not None:
            _parse_timestamp(label["known_at"], f"{task['task_id']}.known_at")
        for fact in label.get("expected_facts", []):
            if fact.get("fact_type") not in STRUCTURED_FACT_TYPES and not fact.get("marker_groups"):
                raise SnapshotError(f"{task['task_id']}: free-text fact lacks marker groups")
            if fact.get("required") not in {True, False}:
                raise SnapshotError(f"{task['task_id']}: every frozen fact must declare required")
        for group in label.get("forbidden_inferences", []):
            if (not isinstance(group, list) or not group
                    or not all(isinstance(marker, str) and marker.strip() for marker in group)):
                raise SnapshotError(f"{task['task_id']}: malformed forbidden inference group")
        decision = label.get("expected_decision_state")
        if decision:
            if not decision.get("decision_id"):
                raise SnapshotError(f"{task['task_id']}: decision_id required")
            if "expected_current_version_id" not in decision:
                raise SnapshotError(f"{task['task_id']}: expected_current_version_id required")
        abstention = label.get("expected_abstention")
        if abstention and abstention.get("required") and not abstention.get("must_disclose"):
            raise SnapshotError(f"{task['task_id']}: abstention must state what to disclose")
        if task["category"] == "insufficient" and not (abstention or {}).get("required"):
            raise SnapshotError(f"{task['task_id']}: insufficient task must expect abstention")
        if task["category"] == "tool_failure":
            failure = label.get("injected_failure") or {}
            if not failure.get("skill") or not failure.get("error"):
                raise SnapshotError(f"{task['task_id']}: tool_failure needs an injection config")
        turns = label.get("turns")
        if turns:
            if not label.get("session_id") or len(turns) < 2:
                raise SnapshotError(f"{task['task_id']}: multi-turn needs session_id and >=2 turns")
            if len(turns) != len(task.get("turns", [])):
                raise SnapshotError(f"{task['task_id']}: public/Gold turn count mismatch")
            if not any(turn.get("evaluation_target") for turn in turns[:-1]):
                raise SnapshotError(f"{task['task_id']}: prerequisite turns need evaluation targets")
            if not turns[-1].get("evaluation_target"):
                raise SnapshotError(f"{task['task_id']}: final turn needs an evaluation target")
        elif task["category"] == "multi_turn":
            raise SnapshotError(f"{task['task_id']}: multi_turn category requires explicit turns")


def _parse_timestamp(value: str, name: str) -> None:
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotError(f"{name}: invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise SnapshotError(f"{name}: timestamp must include a timezone")


def materialize(snapshot: Snapshot, workdir: Path | str) -> tuple[Path, Path]:
    """Write Evidence and Decision databases from the frozen snapshot.

    Returns ``(evidence_db, decisions_db)``.  States recorded in
    ``metadata.benchmark_lifecycle`` are applied after append, so deleted and
    orphaned observations remain readable historically but disappear from the
    active current view.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    evidence_db = workdir / "evidence.db"
    decisions_db = workdir / "decisions.db"
    if evidence_db.exists() or decisions_db.exists():
        raise FileExistsError(f"Materialization target must be new: {workdir}")
    store = EvidenceStore(str(evidence_db))
    store.append(list(snapshot.records))
    _apply_lifecycle(snapshot, evidence_db)
    connection = sqlite3.connect(str(decisions_db))
    try:
        connection.executescript((snapshot.root / "decisions.sql").read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
    return evidence_db, decisions_db


def _apply_lifecycle(snapshot: Snapshot, evidence_db: Path) -> None:
    changed = [record for record in snapshot.records
               if record.metadata.get("benchmark_lifecycle") in {"deleted", "orphaned"}]
    if not changed:
        return
    connection = sqlite3.connect(str(evidence_db))
    try:
        for record in changed:
            state = record.metadata["benchmark_lifecycle"]
            connection.execute(
                "UPDATE evidence_current SET state=?, revision=revision+1, updated_at=? "
                "WHERE project_id=? AND dataset_kind=? AND evidence_id=?",
                (state, "2026-10-15T08:00:00+00:00", record.project_id,
                 record.dataset_kind, record.evidence_id))
            if state == "deleted":
                tombstone = "tomb_" + sha256_bytes(
                    f"{record.evidence_id}|{record.content_hash}".encode("utf-8"))[:32]
                connection.execute(
                    "INSERT OR IGNORE INTO evidence_tombstones(tombstone_id,project_id,dataset_kind,"
                    "evidence_id,content_hash,source_id,original_record_id,sync_id,reason,deleted_at,"
                    "source_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tombstone, record.project_id, record.dataset_kind, record.evidence_id,
                     record.content_hash, None, record.original_record_id,
                     "benchmark-materialize", "benchmark_lifecycle_deleted",
                     "2026-10-15T08:00:00+00:00", None))
            bump_revision(connection, record.project_id, record.dataset_kind)
        connection.commit()
    finally:
        connection.close()


def materialize_template(snapshot: Snapshot, root: Path | str) -> tuple[Path, Path]:
    """Materialize once into ``root/_template`` and return its database paths."""
    root = Path(root)
    template = root / "_template"
    return materialize(snapshot, template)


def clone_databases(template: tuple[Path, Path], destination: Path | str) -> tuple[Path, Path]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    evidence_db, decisions_db = destination / "evidence.db", destination / "decisions.db"
    if evidence_db.exists() or decisions_db.exists():
        raise FileExistsError(f"Clone target must be new: {destination}")
    shutil.copyfile(template[0], evidence_db)
    shutil.copyfile(template[1], decisions_db)
    return evidence_db, decisions_db


def scope_for(task: dict[str, Any]) -> EvidenceScope:
    return EvidenceScope(project_id=task["project_id"], dataset_kind=task["dataset_kind"])


def summary(snapshot: Snapshot) -> dict[str, Any]:
    counts = {category: sum(1 for task in snapshot.public_tasks if task["category"] == category)
              for category in CATEGORIES}
    difficulty = {level: sum(1 for task in snapshot.public_tasks if task["difficulty"] == level)
                  for level in ("easy", "medium", "hard")}
    return {
        "dataset_version": snapshot.dataset_version,
        "task_set_version": snapshot.task_set_version,
        "snapshot_id": snapshot.snapshot_id,
        "task_set_sha256": snapshot.manifest["task_set_sha256"],
        "records": len(snapshot.records),
        "evidence_identities": len({record.evidence_id for record in snapshot.records}),
        "projects": len({record.project_id for record in snapshot.records}),
        "tasks": len(snapshot.public_tasks),
        "categories": counts,
        "difficulty": difficulty,
        "annotation_status": snapshot.manifest["annotation_status"],
    }
