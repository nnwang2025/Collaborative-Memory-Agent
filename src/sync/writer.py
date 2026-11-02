"""Transactional Evidence writes for V2 synchronization.

The writer never changes ``evidence_observations`` in place.  It appends new
versions, updates the derived current pointer, and records tombstones.  All
writes in one call run inside a single ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.evidence import Evidence
from src.evidence_store import backfill_current, bump_revision, ensure_schema, utcnow


def _id(prefix, value):
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()


class EvidenceWriter:
    def __init__(self, db_path):
        self.path = Path(db_path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return closing(sqlite3.connect(self.path, timeout=15))

    def apply(self, source, sync_id, records, *, source_revision=None):
        records = [Evidence.model_validate(r.model_dump()) if isinstance(r, Evidence)
                   else Evidence.model_validate(r) for r in records]
        # Validate duplicate identities before opening the transaction.
        seen = set()
        for record in records:
            if record.original_record_id in seen:
                raise ValueError(
                    f"Duplicate source record ID {record.original_record_id!r} in one sync batch"
                )
            seen.add(record.original_record_id)
        counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}
        if not records:
            return counts
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)
            backfill_current(connection)
            now = utcnow()
            for record in records:
                item = connection.execute(
                    "SELECT current_content_hash,state FROM source_items "
                    "WHERE source_id=? AND original_record_id=?",
                    (source.source_id, record.original_record_id),
                ).fetchone()
                previous = connection.execute(
                    "SELECT payload FROM evidence_observations WHERE evidence_id=? "
                    "ORDER BY seq DESC LIMIT 1", (record.evidence_id,),
                ).fetchone()
                same_content = False
                if previous:
                    old = Evidence.model_validate_json(previous[0])
                    # Re-collection time alone is not a new version.
                    same_content = (old.model_dump(exclude={"ingested_at"})
                                    == record.model_dump(exclude={"ingested_at"}))
                if item and item[0] == record.content_hash and item[1] == "active":
                    counts["unchanged"] += 1
                    connection.execute(
                        "UPDATE source_items SET last_seen_at=?, "
                        "source_revision=COALESCE(?,source_revision) "
                        "WHERE source_id=? AND original_record_id=?",
                        (now, source_revision, source.source_id, record.original_record_id),
                    )
                    if not same_content:
                        self._insert_observation(connection, record)
                    continue
                if not same_content:
                    self._insert_observation(connection, record)
                connection.execute(
                    "INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                    (record.project_id, record.dataset_kind, record.source_uri, record.source_type),
                )
                is_update = item is not None or previous is not None
                counts["updated" if is_update else "added"] += 1
                self._upsert_current(connection, record, source.source_id, "active")
                connection.execute(
                    "INSERT INTO source_items(source_id,original_record_id,current_evidence_id,"
                    "current_content_hash,state,source_revision,first_seen_at,last_seen_at,"
                    "deleted_at,metadata_json) VALUES (?,?,?,?, 'active', ?,?,?,NULL,?) "
                    "ON CONFLICT(source_id,original_record_id) DO UPDATE SET "
                    "current_evidence_id=excluded.current_evidence_id, "
                    "current_content_hash=excluded.current_content_hash, state='active', "
                    "source_revision=excluded.source_revision, last_seen_at=excluded.last_seen_at, "
                    "deleted_at=NULL, metadata_json=excluded.metadata_json",
                    (source.source_id, record.original_record_id, record.evidence_id,
                     record.content_hash, source_revision, now, now,
                     json.dumps(record.metadata, ensure_ascii=False)),
                )
                self._insert_version(connection, source.source_id, record, "active",
                                     source_revision, now)
                bump_revision(connection, record.project_id, record.dataset_kind)
        return counts

    @staticmethod
    def _insert_observation(connection, record):
        connection.execute(
            "INSERT INTO evidence_observations(evidence_id,project_id,dataset_kind,"
            "content_hash,payload) VALUES (?,?,?,?,?)",
            (record.evidence_id, record.project_id, record.dataset_kind,
             record.content_hash, record.model_dump_json()),
        )

    @staticmethod
    def _upsert_current(connection, record, source_id, state):
        connection.execute(
            "INSERT INTO evidence_current(project_id,dataset_kind,evidence_id,"
            "current_content_hash,state,source_id,original_record_id,revision,updated_at) "
            "VALUES (?,?,?,?,?,?,?,1,?) ON CONFLICT(project_id,dataset_kind,evidence_id) "
            "DO UPDATE SET current_content_hash=excluded.current_content_hash, "
            "state=excluded.state, source_id=COALESCE(excluded.source_id,evidence_current.source_id), "
            "original_record_id=COALESCE(excluded.original_record_id,evidence_current.original_record_id), "
            "revision=evidence_current.revision+1, updated_at=excluded.updated_at",
            (record.project_id, record.dataset_kind, record.evidence_id,
             record.content_hash, state, source_id, record.original_record_id, utcnow()),
        )

    @staticmethod
    def _insert_version(connection, source_id, record, state, source_revision, observed_at):
        connection.execute(
            "INSERT INTO source_item_versions(source_id,original_record_id,evidence_id,"
            "content_hash,state,source_revision,observed_at,locator_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (source_id, record.original_record_id, record.evidence_id, record.content_hash,
             state, source_revision, observed_at,
             json.dumps(record.locator, ensure_ascii=False)),
        )

    def _change_state(self, source, record_ids, state, *, reason, sync_id,
                      source_revision, write_tombstone):
        if not record_ids:
            return 0
        changed = 0
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)
            now = utcnow()
            for record_id in sorted(set(record_ids)):
                row = connection.execute(
                    "SELECT current_evidence_id,current_content_hash,state FROM source_items "
                    "WHERE source_id=? AND original_record_id=?",
                    (source.source_id, record_id),
                ).fetchone()
                if row is None or row[2] == state:
                    continue
                evidence_id, content_hash, _ = row
                evidence_row = connection.execute(
                    "SELECT payload FROM evidence_observations WHERE evidence_id=? "
                    "AND content_hash=? ORDER BY seq DESC LIMIT 1",
                    (evidence_id, content_hash),
                ).fetchone()
                if evidence_row is None:
                    continue
                record = Evidence.model_validate_json(evidence_row[0])
                if write_tombstone:
                    connection.execute(
                        "INSERT INTO evidence_tombstones(tombstone_id,project_id,dataset_kind,"
                        "evidence_id,content_hash,source_id,original_record_id,sync_id,reason,"
                        "deleted_at,source_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (_id("tomb_", f"{source.source_id}|{record_id}|{content_hash}|{now}"),
                         record.project_id, record.dataset_kind, evidence_id, content_hash,
                         source.source_id, record_id, sync_id, reason, now, source_revision),
                    )
                connection.execute(
                    "UPDATE evidence_current SET state=?, revision=revision+1, updated_at=? "
                    "WHERE project_id=? AND dataset_kind=? AND evidence_id=?",
                    (state, now, record.project_id, record.dataset_kind, evidence_id),
                )
                connection.execute(
                    "UPDATE source_items SET state=?, last_seen_at=?, "
                    "deleted_at=CASE WHEN ?='deleted' THEN ? ELSE deleted_at END "
                    "WHERE source_id=? AND original_record_id=?",
                    (state, now, state, now, source.source_id, record_id),
                )
                self._insert_version(connection, source.source_id, record, state,
                                     source_revision, now)
                bump_revision(connection, record.project_id, record.dataset_kind)
                changed += 1
        return changed

    def delete(self, source, sync_id, record_ids, *, reason="source_record_deleted",
               source_revision=None):
        return self._change_state(source, record_ids, "deleted", reason=reason,
                                  sync_id=sync_id, source_revision=source_revision,
                                  write_tombstone=True)

    def orphan(self, source, sync_id, record_ids, *, source_revision=None):
        # Orphaned records are retained and excluded from default retrieval, but
        # they are not deletions: no tombstone is written.
        return self._change_state(source, record_ids, "orphaned",
                                  reason="git_commit_unreachable", sync_id=sync_id,
                                  source_revision=source_revision, write_tombstone=False)
