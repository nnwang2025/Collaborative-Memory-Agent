"""Append-only SQLite evidence observations with a current-state projection.

The observation table remains the source of truth: rows are never deleted or
rewritten.  ``evidence_current`` is a derived pointer to the latest valid
version for each Evidence identity.  Deletions are represented by state plus a
tombstone event, so historical claims and Decision Memory can still re-fetch
the exact Evidence version by ID/hash.

Reads never create a database.  Older databases that predate the current-state
tables keep working through an explicit migration or an automatic backfill on
the next write.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from src.evidence import Evidence, EvidenceScope

DEFAULT_LIVE_DIR = Path(__file__).resolve().parents[1] / "data" / "live"

CURRENT_STATES = {"active", "deleted", "orphaned", "missing"}

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS evidence_observations ("
    "seq INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL, project_id TEXT NOT NULL, "
    "dataset_kind TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS evidence_partition ON "
    "evidence_observations(project_id, dataset_kind, evidence_id, seq)",
    "CREATE TABLE IF NOT EXISTS sources (project_id TEXT, dataset_kind TEXT, "
    "source_uri TEXT, source_type TEXT, PRIMARY KEY(project_id,dataset_kind,source_uri,source_type))",
    "CREATE TABLE IF NOT EXISTS sync_observations (seq INTEGER PRIMARY KEY, "
    "project_id TEXT, dataset_kind TEXT, source_uri TEXT, state_json TEXT)",
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ingestion_sources ("
    "source_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL "
    "CHECK(dataset_kind IN ('fixture','live')), connector_type TEXT NOT NULL "
    "CHECK(connector_type IN ('git','directory','csv','json','legacy')), "
    "source_uri TEXT NOT NULL, display_name TEXT, config_json TEXT NOT NULL DEFAULT '{}', "
    "status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','error','deleted')), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "UNIQUE(project_id,dataset_kind,source_uri))",
    "CREATE TABLE IF NOT EXISTS sync_cursors ("
    "source_id TEXT PRIMARY KEY REFERENCES ingestion_sources(source_id), "
    "cursor_json TEXT NOT NULL, cursor_version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sync_runs ("
    "sync_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, "
    "mode TEXT NOT NULL CHECK(mode IN ('full','incremental','reconcile')), "
    "status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','partial')), "
    "cursor_before_json TEXT, cursor_after_json TEXT, revision_before INTEGER, revision_after INTEGER, "
    "started_at TEXT NOT NULL, finished_at TEXT, stats_json TEXT NOT NULL DEFAULT '{}', error_json TEXT)",
    "CREATE TABLE IF NOT EXISTS source_items ("
    "source_id TEXT NOT NULL, original_record_id TEXT NOT NULL, "
    "current_evidence_id TEXT NOT NULL, current_content_hash TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('active','deleted','orphaned','missing')), "
    "source_revision TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, deleted_at TEXT, "
    "metadata_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(source_id, original_record_id))",
    "CREATE TABLE IF NOT EXISTS source_item_versions ("
    "version_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, "
    "original_record_id TEXT NOT NULL, evidence_id TEXT NOT NULL, content_hash TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('active','deleted','orphaned','missing')), "
    "source_revision TEXT, observed_at TEXT NOT NULL, locator_json TEXT NOT NULL DEFAULT '{}')",
    "CREATE INDEX IF NOT EXISTS source_item_versions_lookup ON "
    "source_item_versions(source_id, original_record_id, observed_at)",
    "CREATE TABLE IF NOT EXISTS evidence_current ("
    "project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL, evidence_id TEXT NOT NULL, "
    "current_content_hash TEXT NOT NULL, state TEXT NOT NULL "
    "CHECK(state IN ('active','deleted','orphaned','missing')), source_id TEXT, "
    "original_record_id TEXT, revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, "
    "PRIMARY KEY(project_id, dataset_kind, evidence_id))",
    "CREATE INDEX IF NOT EXISTS evidence_current_active ON "
    "evidence_current(project_id, dataset_kind, state, evidence_id)",
    "CREATE INDEX IF NOT EXISTS evidence_current_source ON "
    "evidence_current(source_id, original_record_id)",
    "CREATE TABLE IF NOT EXISTS evidence_tombstones ("
    "tombstone_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL, "
    "evidence_id TEXT NOT NULL, content_hash TEXT, source_id TEXT, original_record_id TEXT, "
    "sync_id TEXT, reason TEXT NOT NULL, deleted_at TEXT NOT NULL, source_revision TEXT)",
    "CREATE TABLE IF NOT EXISTS project_state ("
    "project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL, "
    "evidence_revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, "
    "PRIMARY KEY(project_id, dataset_kind))",
    "CREATE TABLE IF NOT EXISTS evidence_chunks ("
    "chunk_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL, "
    "evidence_id TEXT NOT NULL, content_hash TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
    "char_start INTEGER, char_end INTEGER, line_start INTEGER, line_end INTEGER, "
    "text TEXT NOT NULL, text_hash TEXT NOT NULL, token_count INTEGER, chunker_version TEXT NOT NULL, "
    "locator_status TEXT NOT NULL CHECK(locator_status IN ('exact','approximate','unavailable')), "
    "created_at TEXT NOT NULL, UNIQUE(evidence_id, content_hash, chunker_version, chunk_index))",
    "CREATE INDEX IF NOT EXISTS chunks_evidence ON "
    "evidence_chunks(project_id, dataset_kind, evidence_id, content_hash)",
    "CREATE TABLE IF NOT EXISTS index_versions ("
    "index_version TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL, "
    "embedding_model TEXT NOT NULL, embedding_dim INTEGER NOT NULL, chunker_version TEXT NOT NULL, "
    "source_revision INTEGER NOT NULL, evidence_count INTEGER NOT NULL, chunk_count INTEGER NOT NULL, "
    "faiss_sha256 TEXT NOT NULL, chunks_sha256 TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN ('building','ready','failed','retired')), "
    "path TEXT NOT NULL, created_at TEXT NOT NULL, activated_at TEXT, retired_at TEXT, error_json TEXT)",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_building_index ON "
    "index_versions(project_id, dataset_kind) WHERE status='building'",
    "CREATE TABLE IF NOT EXISTS index_members ("
    "index_version TEXT NOT NULL, chunk_id TEXT NOT NULL, evidence_id TEXT NOT NULL, "
    "content_hash TEXT NOT NULL, PRIMARY KEY(index_version, chunk_id))",
    "CREATE TABLE IF NOT EXISTS index_aliases ("
    "project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL, active_index_version TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, PRIMARY KEY(project_id, dataset_kind))",
)


def default_db_path():
    return Path(os.getenv("EVIDENCE_DB_PATH", str(DEFAULT_LIVE_DIR / "evidence.sqlite3")))


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(connection):
    """Create additive V2 tables.  Safe to call inside ``BEGIN IMMEDIATE``."""
    for statement in SCHEMA:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) VALUES (2, ?, ?) "
        "ON CONFLICT(version) DO NOTHING",
        (utcnow(), "current-state, sync registry, chunks, index metadata"),
    )


def _table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def backfill_current(connection):
    """Project legacy observations into ``evidence_current`` without rewriting them."""
    if not _table_exists(connection, "evidence_current"):
        return 0
    now = utcnow()
    rows = connection.execute(
        "SELECT o.evidence_id, o.project_id, o.dataset_kind, o.content_hash, o.payload "
        "FROM evidence_observations o JOIN ("
        "  SELECT evidence_id, project_id, dataset_kind, MAX(seq) AS max_seq "
        "  FROM evidence_observations GROUP BY evidence_id, project_id, dataset_kind"
        ") latest ON latest.evidence_id=o.evidence_id AND latest.project_id=o.project_id "
        "AND latest.dataset_kind=o.dataset_kind AND latest.max_seq=o.seq "
        "LEFT JOIN evidence_current c ON c.project_id=o.project_id AND c.dataset_kind=o.dataset_kind "
        "AND c.evidence_id=o.evidence_id WHERE c.evidence_id IS NULL"
    ).fetchall()
    for evidence_id, project_id, dataset_kind, content_hash, payload in rows:
        try:
            record = Evidence.model_validate_json(payload)
        except Exception:
            continue
        connection.execute(
            "INSERT INTO evidence_current(project_id,dataset_kind,evidence_id,current_content_hash,"
            "state,source_id,original_record_id,revision,updated_at) VALUES (?,?,?,?, 'active', NULL, ?, 1, ?) "
            "ON CONFLICT(project_id,dataset_kind,evidence_id) DO NOTHING",
            (project_id, dataset_kind, evidence_id, content_hash, record.original_record_id, now),
        )
        connection.execute(
            "INSERT INTO project_state(project_id,dataset_kind,evidence_revision,updated_at) "
            "VALUES (?,?,1,?) ON CONFLICT(project_id,dataset_kind) DO UPDATE SET "
            "evidence_revision=MAX(evidence_revision,1), updated_at=excluded.updated_at",
            (project_id, dataset_kind, now),
        )
    return len(rows)


def bump_revision(connection, project_id, dataset_kind, amount=1):
    connection.execute(
        "INSERT INTO project_state(project_id,dataset_kind,evidence_revision,updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(project_id,dataset_kind) DO UPDATE SET "
        "evidence_revision=evidence_revision+excluded.evidence_revision, updated_at=excluded.updated_at",
        (project_id, dataset_kind, amount, utcnow()),
    )


class EvidenceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_db_path()

    # -- write -----------------------------------------------------------
    def append(self, records, sync_states=()):
        # Validate the whole batch BEFORE touching storage; one transaction per batch.
        records = [Evidence.model_validate(r.model_dump()) if isinstance(r, Evidence)
                   else Evidence.model_validate(r) for r in records]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)
            backfill_current(connection)
            added = 0
            for record in records:
                previous = connection.execute(
                    "SELECT payload FROM evidence_observations WHERE evidence_id=? "
                    "ORDER BY seq DESC LIMIT 1", (record.evidence_id,)
                ).fetchone()
                if previous:
                    old = Evidence.model_validate_json(previous[0])
                    # Re-collection time alone must not create a version.
                    if old.model_dump(exclude={"ingested_at"}) == record.model_dump(exclude={"ingested_at"}):
                        self._ensure_current(connection, record)
                        continue
                connection.execute(
                    "INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                    (record.project_id, record.dataset_kind, record.source_uri, record.source_type),
                )
                connection.execute(
                    "INSERT INTO evidence_observations "
                    "(evidence_id,project_id,dataset_kind,content_hash,payload) VALUES (?,?,?,?,?)",
                    (record.evidence_id, record.project_id, record.dataset_kind,
                     record.content_hash, record.model_dump_json()),
                )
                self._upsert_current(connection, record, source_id=None, state="active")
                bump_revision(connection, record.project_id, record.dataset_kind)
                added += 1
            for state in sync_states:
                connection.execute(
                    "INSERT INTO sync_observations(project_id,dataset_kind,source_uri,state_json) VALUES (?,?,?,?)",
                    (state["project_id"], state["dataset_kind"], state["source_uri"], json.dumps(state)),
                )
        return added

    @staticmethod
    def _ensure_current(connection, record):
        exists = connection.execute(
            "SELECT 1 FROM evidence_current WHERE project_id=? AND dataset_kind=? AND evidence_id=?",
            (record.project_id, record.dataset_kind, record.evidence_id),
        ).fetchone()
        if not exists:
            EvidenceStore._upsert_current(connection, record, source_id=None, state="active")

    @staticmethod
    def _upsert_current(connection, record, source_id, state, original_record_id=None):
        now = utcnow()
        connection.execute(
            "INSERT INTO evidence_current(project_id,dataset_kind,evidence_id,current_content_hash,"
            "state,source_id,original_record_id,revision,updated_at) "
            "VALUES (?,?,?,?,?,?,?,1,?) ON CONFLICT(project_id,dataset_kind,evidence_id) DO UPDATE SET "
            "current_content_hash=excluded.current_content_hash, state=excluded.state, "
            "source_id=COALESCE(excluded.source_id,evidence_current.source_id), "
            "original_record_id=COALESCE(excluded.original_record_id,evidence_current.original_record_id), "
            "revision=evidence_current.revision+1, updated_at=excluded.updated_at",
            (record.project_id, record.dataset_kind, record.evidence_id, record.content_hash,
             state, source_id, original_record_id or record.original_record_id, now),
        )

    # -- read ------------------------------------------------------------
    def list(self, scope: EvidenceScope, *, include_deleted=False, state=None):
        if not self.path.exists():
            return []
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            has_current = _table_exists(connection, "evidence_current")
            if has_current:
                count = connection.execute(
                    "SELECT COUNT(*) FROM evidence_current WHERE project_id=? AND dataset_kind=?",
                    (scope.project_id, scope.dataset_kind),
                ).fetchone()[0]
                if count:
                    conditions = ["c.project_id=?", "c.dataset_kind=?"]
                    args = [scope.project_id, scope.dataset_kind]
                    if state:
                        conditions.append("c.state=?")
                        args.append(state)
                    elif not include_deleted:
                        conditions.append("c.state='active'")
                    rows = connection.execute(
                        "SELECT o.payload FROM evidence_current c "
                        "JOIN evidence_observations o ON o.evidence_id=c.evidence_id "
                        "AND o.project_id=c.project_id AND o.dataset_kind=c.dataset_kind "
                        "AND o.content_hash=c.current_content_hash "
                        "WHERE " + " AND ".join(conditions) +
                        " AND o.seq=(SELECT MAX(seq) FROM evidence_observations o2 "
                        "WHERE o2.evidence_id=c.evidence_id AND o2.project_id=c.project_id "
                        "AND o2.dataset_kind=c.dataset_kind AND o2.content_hash=c.current_content_hash) "
                        "ORDER BY c.evidence_id", args,
                    ).fetchall()
                    return [Evidence.model_validate_json(row[0]) for row in rows]
            # Legacy database without a populated current-state projection.
            rows = connection.execute(
                "SELECT payload FROM evidence_observations WHERE seq IN "
                "(SELECT MAX(seq) FROM evidence_observations WHERE project_id=? AND dataset_kind=? "
                "GROUP BY evidence_id) ORDER BY evidence_id",
                (scope.project_id, scope.dataset_kind),
            ).fetchall()
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

    def current_state(self, evidence_id, scope, content_hash=None):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            if not _table_exists(connection, "evidence_current"):
                return None
            row = connection.execute(
                "SELECT current_content_hash,state,revision,updated_at FROM evidence_current "
                "WHERE evidence_id=? AND project_id=? AND dataset_kind=?",
                (evidence_id, scope.project_id, scope.dataset_kind),
            ).fetchone()
        if row is None:
            return None
        current_hash, state, revision, updated_at = row
        return {"content_hash": current_hash, "state": state, "revision": revision,
                "updated_at": updated_at,
                "matches": content_hash is None or content_hash == current_hash}

    def is_active(self, evidence_id, scope, content_hash=None):
        state = self.current_state(evidence_id, scope, content_hash)
        # Records from legacy JSON exports or directly appended before the
        # current-state projection are treated as active until a sync changes them.
        return True if state is None else state["state"] == "active" and state["matches"]

    def revision(self, scope):
        if not self.path.exists():
            return 0
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            if not _table_exists(connection, "project_state"):
                return 0
            row = connection.execute(
                "SELECT evidence_revision FROM project_state WHERE project_id=? AND dataset_kind=?",
                (scope.project_id, scope.dataset_kind),
            ).fetchone()
        return int(row[0]) if row else 0

    def list_versions(self, evidence_id, scope):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT payload FROM evidence_observations WHERE evidence_id=? AND project_id=? "
                "AND dataset_kind=? ORDER BY seq", (evidence_id, scope.project_id, scope.dataset_kind),
            ).fetchall()
        return [Evidence.model_validate_json(row[0]) for row in rows]

    def migrate(self):
        """Explicit additive migration; never rewrites observations."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)
            return backfill_current(connection)


def read_live_json(directory, scope):
    """Read existing exports without migrating or guessing missing project ownership."""
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = data if isinstance(data, list) else data.get("records", []) if isinstance(data, dict) else []
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
