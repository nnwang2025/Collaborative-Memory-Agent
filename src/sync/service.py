"""Source registry and synchronization service.

The service separates scanning from writing so it can control transactions,
cursor advancement, and deletion reconciliation explicitly.  A connector scans
without touching SQLite; the service then applies batches and only advances the
cursor after every write succeeded.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore, ensure_schema, utcnow
from src.sync.connectors import CONNECTORS
from src.sync.models import ScanBatch, SourceSpec, SyncReport
from src.sync.writer import EvidenceWriter

BATCH_SIZE = 200
VALID_SOURCE_STATES = {"active", "paused", "error", "deleted"}


def _source_id(project_id, dataset_kind, connector_type, source_uri):
    identity = json.dumps([project_id, dataset_kind, connector_type, source_uri],
                          ensure_ascii=False, separators=(",", ":"))
    return "src_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


class SourceRegistry:
    def __init__(self, db_path):
        self.path = Path(db_path)
        with closing(_connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)

    def register(self, spec: SourceSpec) -> SourceSpec:
        if not spec.project_id or not spec.dataset_kind:
            raise ValueError("project_id and dataset_kind are required")
        if spec.connector_type not in CONNECTORS:
            raise ValueError(f"Unsupported connector type: {spec.connector_type}")
        if not spec.source_uri:
            raise ValueError("source_uri is required")
        if spec.dataset_kind not in {"fixture", "live"}:
            raise ValueError("dataset_kind must be fixture or live")
        try:
            config_json = json.dumps(spec.config or {}, ensure_ascii=False, sort_keys=True)
        except TypeError as exc:
            raise ValueError("source config must be JSON serializable") from exc
        sid = spec.source_id or _source_id(spec.project_id, spec.dataset_kind,
                                           spec.connector_type, spec.source_uri)
        now = utcnow()
        with closing(_connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO ingestion_sources(source_id,project_id,dataset_kind,connector_type,"
                "source_uri,display_name,config_json,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'active',?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET display_name=excluded.display_name, "
                "config_json=excluded.config_json, updated_at=excluded.updated_at",
                (sid, spec.project_id, spec.dataset_kind, spec.connector_type, spec.source_uri,
                 spec.display_name, config_json, now, now),
            )
        return SourceSpec(project_id=spec.project_id, dataset_kind=spec.dataset_kind,
                          connector_type=spec.connector_type, source_uri=spec.source_uri,
                          source_id=sid, config=dict(spec.config or {}),
                          display_name=spec.display_name)

    def get(self, source_id) -> SourceSpec | None:
        if not self.path.exists():
            return None
        with closing(_connect(self.path)) as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_sources WHERE source_id=?", (source_id,)
            ).fetchone()
        return self._spec(row) if row else None

    def list(self, *, project_id=None, dataset_kind=None, include_deleted=False):
        if not self.path.exists():
            return []
        sql = "SELECT * FROM ingestion_sources"
        conditions, args = [], []
        if project_id:
            conditions.append("project_id=?")
            args.append(project_id)
        if dataset_kind:
            conditions.append("dataset_kind=?")
            args.append(dataset_kind)
        if not include_deleted:
            conditions.append("status<>'deleted'")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        with closing(_connect(self.path)) as connection:
            rows = connection.execute(sql + " ORDER BY created_at,source_id", args).fetchall()
        return [self._spec(row) for row in rows]

    @staticmethod
    def _spec(row):
        return SourceSpec(project_id=row["project_id"], dataset_kind=row["dataset_kind"],
                          connector_type=row["connector_type"], source_uri=row["source_uri"],
                          source_id=row["source_id"],
                          config=json.loads(row["config_json"] or "{}"),
                          display_name=row["display_name"])

    def set_status(self, source_id, status):
        if status not in VALID_SOURCE_STATES:
            raise ValueError(f"Invalid source status: {status}")
        with closing(_connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE ingestion_sources SET status=?, updated_at=? WHERE source_id=?",
                (status, utcnow(), source_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown source: {source_id}")

    def pause(self, source_id):
        self.set_status(source_id, "paused")

    def resume(self, source_id):
        self.set_status(source_id, "active")

    def status(self, source_id):
        spec = self.get(source_id)
        return None if spec is None else self._raw_status(source_id)

    def _raw_status(self, source_id):
        with closing(_connect(self.path)) as connection:
            row = connection.execute(
                "SELECT status FROM ingestion_sources WHERE source_id=?", (source_id,)
            ).fetchone()
        return None if row is None else row["status"]


class SyncService:
    def __init__(self, db_path, *, batch_size=BATCH_SIZE):
        self.path = Path(db_path)
        self.registry = SourceRegistry(self.path)
        self.store = EvidenceStore(self.path)
        self.writer = EvidenceWriter(self.path)
        self.batch_size = batch_size
        with closing(_connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)

    # -- helpers ---------------------------------------------------------
    def _cursor(self, source_id):
        with closing(_connect(self.path)) as connection:
            row = connection.execute(
                "SELECT cursor_json,cursor_version FROM sync_cursors WHERE source_id=?",
                (source_id,),
            ).fetchone()
        if row is None:
            return {}, 0
        return json.loads(row["cursor_json"]), int(row["cursor_version"])

    def _known_ids(self, source_id):
        with closing(_connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT original_record_id FROM source_items WHERE source_id=? "
                "AND state IN ('active','orphaned','missing')", (source_id,),
            ).fetchall()
        return {row[0] for row in rows}

    @staticmethod
    def _source_revision(records, batch):
        digest = hashlib.sha256()
        for record in sorted(records, key=lambda r: r.original_record_id):
            digest.update(record.original_record_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(record.content_hash.encode("ascii"))
            digest.update(b"\0")
        digest.update(json.dumps(batch.next_cursor, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return digest.hexdigest()

    def _begin_run(self, source, cursor, version, revision_before):
        sync_id = "sync_" + uuid.uuid4().hex
        with closing(_connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                "SELECT 1 FROM sync_runs WHERE source_id=? AND status='running'",
                (source.source_id,),
            ).fetchone()
            if running:
                raise RuntimeError(f"Sync already running for source {source.source_id}")
            connection.execute(
                "INSERT INTO sync_runs(sync_id,source_id,mode,status,cursor_before_json,"
                "revision_before,started_at,stats_json) VALUES (?,?,?,'running',?,?,?,'{}')",
                (sync_id, source.source_id, "full" if not cursor else "incremental",
                 json.dumps(cursor, ensure_ascii=False), revision_before, utcnow()),
            )
        return sync_id

    def _finish_run(self, sync_id, *, status, stats, cursor_after=None, error=None,
                    revision_after=None):
        with closing(_connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT cursor_before_json,revision_before FROM sync_runs WHERE sync_id=?",
                (sync_id,),
            ).fetchone()
            if row is None:
                raise KeyError(sync_id)
            connection.execute(
                "UPDATE sync_runs SET status=?, finished_at=?, stats_json=?, "
                "cursor_after_json=?, error_json=?, revision_after=? WHERE sync_id=?",
                (status, utcnow(), json.dumps(stats, ensure_ascii=False),
                 None if cursor_after is None else json.dumps(cursor_after, ensure_ascii=False),
                 None if error is None else json.dumps(error, ensure_ascii=False),
                 revision_after if revision_after is not None else row["revision_before"],
                 sync_id),
            )

    def _complete_run(self, source_id, sync_id, expected_version, cursor, *,
                      status, stats, revision_after, advance):
        """Advance the cursor and mark success in one transaction."""
        with closing(_connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status,cursor_before_json FROM sync_runs WHERE sync_id=?", (sync_id,)
            ).fetchone()
            if run is None or run["status"] != "running":
                raise RuntimeError(f"Sync run {sync_id} is not running")
            cursor_after = cursor
            if advance:
                updated = connection.execute(
                    "UPDATE sync_cursors SET cursor_json=?, cursor_version=cursor_version+1, "
                    "updated_at=? WHERE source_id=? AND cursor_version=?",
                    (json.dumps(cursor, ensure_ascii=False), utcnow(), source_id,
                     expected_version),
                )
                if updated.rowcount == 0:
                    exists = connection.execute(
                        "SELECT cursor_version FROM sync_cursors WHERE source_id=?",
                        (source_id,),
                    ).fetchone()
                    if exists is None and expected_version == 0:
                        connection.execute(
                            "INSERT INTO sync_cursors(source_id,cursor_json,cursor_version,"
                            "updated_at) VALUES (?,?,1,?)",
                            (source_id, json.dumps(cursor, ensure_ascii=False), utcnow()),
                        )
                    else:
                        raise RuntimeError(
                            f"Cursor conflict for {source_id}: expected version "
                            f"{expected_version}"
                        )
            else:
                cursor_after = json.loads(run["cursor_before_json"] or "{}")
            connection.execute(
                "UPDATE sync_runs SET status=?, finished_at=?, stats_json=?, "
                "cursor_after_json=?, revision_after=? WHERE sync_id=?",
                (status, utcnow(), json.dumps(stats, ensure_ascii=False),
                 json.dumps(cursor_after, ensure_ascii=False), revision_after, sync_id),
            )
            return cursor_after

    def _dry_run_report(self, source, batch, cursor):
        added = updated = unchanged = 0
        with closing(_connect(self.path)) as connection:
            for record in batch.records:
                row = connection.execute(
                    "SELECT current_content_hash,state FROM source_items "
                    "WHERE source_id=? AND original_record_id=?",
                    (source.source_id, record["original_record_id"]),
                ).fetchone()
                if row and row[0] == record["content_hash"] and row[1] == "active":
                    unchanged += 1
                elif row:
                    updated += 1
                else:
                    added += 1
        return SyncReport(
            source_id=source.source_id, sync_id="dry_run", status="dry_run",
            mode="dry_run", scanned=len(batch.records), added=added, updated=updated,
            unchanged=unchanged, deleted=len(batch.deletions) if batch.complete else 0,
            orphaned=len(batch.orphaned) if batch.complete else 0,
            warnings=list(batch.warnings) + ["dry_run_no_writes"],
            revision_before=self.store.revision(self._scope(source)),
            revision_after=self.store.revision(self._scope(source)),
            cursor_after=batch.next_cursor,
        )

    @staticmethod
    def _scope(source):
        return EvidenceScope(project_id=source.project_id, dataset_kind=source.dataset_kind)

    def sync(self, source_or_id, *, dry_run=False):
        source = (self.registry.get(source_or_id) if isinstance(source_or_id, str)
                  else source_or_id)
        if source is None:
            raise KeyError(f"Unknown source: {source_or_id}")
        if source.source_id is None:
            source = self.registry.register(source)
        status = self.registry._raw_status(source.source_id)
        if status != "active":
            raise RuntimeError(f"Source {source.source_id} is {status}; resume it before syncing")
        connector = CONNECTORS[source.connector_type]
        scope = self._scope(source)
        cursor, cursor_version = self._cursor(source.source_id)
        known_ids = self._known_ids(source.source_id)
        revision_before = self.store.revision(scope)
        sync_id = None if dry_run else self._begin_run(
            source, cursor, cursor_version, revision_before)
        stats = {"scanned": 0, "added": 0, "updated": 0, "unchanged": 0,
                 "deleted": 0, "orphaned": 0, "failed": 0, "skipped": 0}
        try:
            batch = connector.scan(source, cursor, known_ids)
            stats["scanned"] = len(batch.records)
            if dry_run:
                return self._dry_run_report(source, batch, cursor)
            source_revision = self._source_revision(
                [Evidence.model_validate(r) for r in batch.records], batch)
            for start in range(0, len(batch.records), self.batch_size):
                chunk = batch.records[start:start + self.batch_size]
                counts = self.writer.apply(source, sync_id, chunk,
                                           source_revision=source_revision)
                for key in ("added", "updated", "unchanged", "skipped"):
                    stats[key] += counts[key]
            if batch.complete:
                stats["deleted"] = self.writer.delete(
                    source, sync_id, batch.deletions, source_revision=source_revision)
                stats["orphaned"] = self.writer.orphan(
                    source, sync_id, batch.orphaned, source_revision=source_revision)
            else:
                stats["skipped"] += len(batch.deletions) + len(batch.orphaned)
                if batch.deletions or batch.orphaned:
                    batch.warnings.append("incomplete_scan_deletes_suppressed")
            revision_after = self.store.revision(scope)
            allow_cursor = not any(w in batch.warnings for w in (
                "shallow_clone_history_incomplete", "commit_history_truncated"))
            status = "succeeded" if batch.complete or allow_cursor else "partial"
            if not allow_cursor:
                batch.warnings.append("cursor_not_advanced_incomplete_scan")
            cursor_after = self._complete_run(
                source.source_id, sync_id, cursor_version, batch.next_cursor,
                status=status, stats={**stats, "warnings": batch.warnings},
                revision_after=revision_after, advance=allow_cursor,
            )
            return SyncReport(
                source_id=source.source_id, sync_id=sync_id, status=status,
                mode=batch.next_cursor.get("history_mode", "sync"),
                scanned=stats["scanned"], added=stats["added"], updated=stats["updated"],
                unchanged=stats["unchanged"], deleted=stats["deleted"],
                orphaned=stats["orphaned"], failed=stats["failed"],
                skipped=stats["skipped"], warnings=list(batch.warnings),
                revision_before=revision_before, revision_after=revision_after,
                cursor_after=cursor_after,
            )
        except Exception as exc:
            if sync_id is not None:
                partial = any(stats[key] for key in ("added", "updated", "deleted", "orphaned"))
                self._finish_run(
                    sync_id, status="partial" if partial else "failed",
                    stats={**stats, "error": str(exc)},
                    cursor_after=cursor,
                    error={"type": type(exc).__name__, "message": str(exc)},
                    revision_after=self.store.revision(scope),
                )
            raise

    def status(self, source_id):
        source = self.registry.get(source_id)
        if source is None:
            return None
        cursor, version = self._cursor(source_id)
        with closing(_connect(self.path)) as connection:
            run = connection.execute(
                "SELECT sync_id,status,mode,started_at,finished_at,stats_json,error_json "
                "FROM sync_runs WHERE source_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            counts = connection.execute(
                "SELECT state,COUNT(*) FROM source_items WHERE source_id=? GROUP BY state",
                (source_id,),
            ).fetchall()
        return {
            "source": {
                "source_id": source.source_id, "project_id": source.project_id,
                "dataset_kind": source.dataset_kind,
                "connector_type": source.connector_type, "source_uri": source.source_uri,
                "display_name": source.display_name,
                "status": self.registry._raw_status(source_id),
            },
            "cursor": cursor,
            "cursor_version": version,
            "last_run": None if run is None else {
                "sync_id": run["sync_id"], "status": run["status"], "mode": run["mode"],
                "started_at": run["started_at"], "finished_at": run["finished_at"],
                "stats": json.loads(run["stats_json"] or "{}"),
                "error": json.loads(run["error_json"]) if run["error_json"] else None,
            },
            "item_counts": {row[0]: row[1] for row in counts},
        }
