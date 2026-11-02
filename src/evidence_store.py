"""Append-only SQLite evidence observations; reads never create a database."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from src.evidence import Evidence, EvidenceScope

DEFAULT_LIVE_DIR = Path(__file__).resolve().parents[1] / "data" / "live"


def default_db_path():
    return Path(os.getenv("EVIDENCE_DB_PATH", str(DEFAULT_LIVE_DIR / "evidence.sqlite3")))


class EvidenceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_db_path()

    def append(self, records, sync_states=()):
        # Validate the whole batch BEFORE touching storage; one transaction per batch.
        records = [Evidence.model_validate(r.model_dump()) if isinstance(r, Evidence)
                   else Evidence.model_validate(r) for r in records]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as connection, connection:
            # Serialize compare-and-append across threads/processes as one transaction.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS evidence_observations ("
                               "seq INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL, project_id TEXT NOT NULL, "
                               "dataset_kind TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS evidence_partition ON "
                               "evidence_observations(project_id, dataset_kind, evidence_id, seq)")
            connection.execute("CREATE TABLE IF NOT EXISTS sources (project_id TEXT, dataset_kind TEXT, "
                               "source_uri TEXT, source_type TEXT, PRIMARY KEY(project_id,dataset_kind,source_uri,source_type))")
            connection.execute("CREATE TABLE IF NOT EXISTS sync_observations (seq INTEGER PRIMARY KEY, "
                               "project_id TEXT, dataset_kind TEXT, source_uri TEXT, state_json TEXT)")
            added = 0
            for record in records:
                previous = connection.execute("SELECT payload FROM evidence_observations WHERE evidence_id=? "
                                              "ORDER BY seq DESC LIMIT 1", (record.evidence_id,)).fetchone()
                if previous:
                    old = Evidence.model_validate_json(previous[0])
                    # Re-collection time alone must not create a version.
                    if old.model_dump(exclude={"ingested_at"}) == record.model_dump(exclude={"ingested_at"}):
                        continue
                connection.execute("INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                                   (record.project_id, record.dataset_kind, record.source_uri, record.source_type))
                connection.execute("INSERT INTO evidence_observations "
                                   "(evidence_id,project_id,dataset_kind,content_hash,payload) VALUES (?,?,?,?,?)",
                                   (record.evidence_id, record.project_id, record.dataset_kind,
                                    record.content_hash, record.model_dump_json()))
                added += 1
            for state in sync_states:
                connection.execute("INSERT INTO sync_observations(project_id,dataset_kind,source_uri,state_json) VALUES (?,?,?,?)",
                                   (state["project_id"], state["dataset_kind"], state["source_uri"], json.dumps(state)))
        return added

    def list(self, scope: EvidenceScope):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT payload FROM evidence_observations WHERE seq IN "
                "(SELECT MAX(seq) FROM evidence_observations WHERE project_id=? AND dataset_kind=? GROUP BY evidence_id) "
                "ORDER BY evidence_id", (scope.project_id, scope.dataset_kind)).fetchall()
        return [Evidence.model_validate_json(row[0]) for row in rows]

    def get(self, evidence_id: str, scope: EvidenceScope, content_hash: str | None = None):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            sql = "SELECT payload FROM evidence_observations WHERE evidence_id=? AND project_id=? AND dataset_kind=?"
            args = [evidence_id, scope.project_id, scope.dataset_kind]
            if content_hash is not None:
                sql += " AND content_hash=?"
                args.append(content_hash)
            row = connection.execute(sql + " ORDER BY seq DESC LIMIT 1", args).fetchone()
        return Evidence.model_validate_json(row[0]) if row else None


def read_live_json(directory, scope):
    """Read existing exports without migrating or guessing missing project ownership."""
    for path in sorted(Path(directory).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data if isinstance(data, list) else data.get("records", [])
        for record in records:
            if record.get("project_id") != scope.project_id or record.get("dataset_kind") != scope.dataset_kind:
                continue  # old unscoped exports require explicit, reviewed project binding
            if "evidence_id" in record:
                yield Evidence.model_validate(record)
            else:
                from src.evidence import legacy_evidence
                yield legacy_evidence(record, scope=scope)


def bind_legacy_export(path, *, project_id, db_path=None, dry_run=True):
    """Explicit operator binding, not a migration: preserve JSON and append to SQLite.

    The default is inspection only. Missing provenance stays unverified and null.
    """
    from src.evidence import legacy_evidence
    scope = EvidenceScope(project_id=project_id, dataset_kind="live")
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else data["records"]
    converted = []
    for index, record in enumerate(records):
        if "evidence_id" in record:
            evidence = Evidence.model_validate(record)
            if (evidence.project_id, evidence.dataset_kind) != (project_id, "live"):
                raise ValueError("Explicit export scope conflicts with requested project binding")
        else:
            if record.get("project_id") not in (None, project_id) or record.get("dataset_kind") not in (None, "live"):
                raise ValueError("Legacy record conflicts with requested project binding")
            item = {**record, "record": record.get("id", index),
                    "source": record.get("source") or path.resolve().as_uri()}
            evidence = legacy_evidence(item, scope=scope)
            evidence = evidence.model_copy(update={"metadata": {**evidence.metadata,
                "explicit_project_binding": project_id, "legacy_export_uri": path.resolve().as_uri(),
                "legacy_original_payload": record}})
        converted.append(evidence)
    return {"records": len(converted), "appended_observations": 0 if dry_run else EvidenceStore(db_path).append(converted),
            "dry_run": dry_run}
