"""SQLite-backed metadata for live chunk/index lifecycle."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.evidence_store import ensure_schema, utcnow


class IndexStaleError(RuntimeError):
    pass


class IndexStore:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def save_chunks(self, chunks):
        if not chunks:
            return 0
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = 0
            for chunk in chunks:
                cur = connection.execute(
                    "INSERT INTO evidence_chunks(chunk_id,project_id,dataset_kind,evidence_id,"
                    "content_hash,chunk_index,char_start,char_end,line_start,line_end,text,"
                    "text_hash,token_count,chunker_version,locator_status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(chunk_id) DO NOTHING",
                    (chunk["chunk_id"], chunk["project_id"], chunk["dataset_kind"],
                     chunk["evidence_id"], chunk["content_hash"], chunk["chunk_index"],
                     chunk.get("char_start"), chunk.get("char_end"),
                     chunk.get("line_start"), chunk.get("line_end"),
                     chunk["text"], chunk["text_hash"], chunk.get("token_count"),
                     chunk["chunker_version"], chunk["locator_status"], chunk["created_at"]),
                )
                inserted += cur.rowcount
        return inserted

    def chunks_for_evidence(self, scope, *, evidence_id, content_hash=None):
        with closing(self._connect()) as connection:
            sql = ("SELECT * FROM evidence_chunks WHERE project_id=? AND dataset_kind=? "
                   "AND evidence_id=?")
            args = [scope.project_id, scope.dataset_kind, evidence_id]
            if content_hash is not None:
                sql += " AND content_hash=?"
                args.append(content_hash)
            rows = connection.execute(sql + " ORDER BY chunk_index", args).fetchall()
        return [self._chunk(row) for row in rows]

    @staticmethod
    def _chunk(row):
        return {
            "chunk_id": row["chunk_id"], "project_id": row["project_id"],
            "dataset_kind": row["dataset_kind"], "evidence_id": row["evidence_id"],
            "content_hash": row["content_hash"], "chunk_index": row["chunk_index"],
            "char_start": row["char_start"], "char_end": row["char_end"],
            "line_start": row["line_start"], "line_end": row["line_end"],
            "text": row["text"], "text_hash": row["text_hash"],
            "token_count": row["token_count"], "chunker_version": row["chunker_version"],
            "locator_status": row["locator_status"], "created_at": row["created_at"],
        }

    def start_version(self, scope, *, version_id, embedding_model, embedding_dim,
                      chunker_version, source_revision, evidence_count, chunk_count, path):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            building = connection.execute(
                "SELECT index_version FROM index_versions WHERE project_id=? "
                "AND dataset_kind=? AND status='building'",
                (scope.project_id, scope.dataset_kind),
            ).fetchone()
            if building and building["index_version"] != version_id:
                raise RuntimeError(
                    f"Index build already running for {scope.project_id}/{scope.dataset_kind}"
                )
            connection.execute(
                "INSERT INTO index_versions(index_version,project_id,dataset_kind,"
                "embedding_model,embedding_dim,chunker_version,source_revision,evidence_count,"
                "chunk_count,faiss_sha256,chunks_sha256,status,path,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, '', '', 'building', ?, ?)",
                (version_id, scope.project_id, scope.dataset_kind, embedding_model,
                 embedding_dim, chunker_version, source_revision, evidence_count,
                 chunk_count, str(path), utcnow()),
            )

    def fail_version(self, version_id, error):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE index_versions SET status='failed', error_json=? "
                "WHERE index_version=? AND status='building'",
                (json.dumps(error, ensure_ascii=False), version_id),
            )

    def publish(self, version_id, *, faiss_sha256, chunks_sha256, chunk_ids,
                expected_revision):
        """Atomically switch the alias after re-checking the evidence snapshot."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute(
                "SELECT * FROM index_versions WHERE index_version=?", (version_id,)
            ).fetchone()
            if version is None:
                raise KeyError(version_id)
            if version["status"] != "building":
                raise RuntimeError(f"Index version {version_id} is not building")
            current_revision = connection.execute(
                "SELECT evidence_revision FROM project_state WHERE project_id=? AND dataset_kind=?",
                (version["project_id"], version["dataset_kind"]),
            ).fetchone()
            current_revision = int(current_revision[0]) if current_revision else 0
            if current_revision != expected_revision:
                raise IndexStaleError(
                    f"Evidence changed during index build ({expected_revision} -> {current_revision})"
                )
            connection.executemany(
                "INSERT INTO index_members(index_version,chunk_id,evidence_id,content_hash) "
                "VALUES (?,?,?,?)",
                [(version_id, c["chunk_id"], c["evidence_id"], c["content_hash"])
                 for c in chunk_ids],
            )
            now = utcnow()
            connection.execute(
                "UPDATE index_versions SET status='ready', faiss_sha256=?, chunks_sha256=?, "
                "activated_at=? WHERE index_version=?",
                (faiss_sha256, chunks_sha256, now, version_id),
            )
            connection.execute(
                "UPDATE index_versions SET status='retired', retired_at=? "
                "WHERE project_id=? AND dataset_kind=? AND index_version<>? AND status='ready'",
                (now, version["project_id"], version["dataset_kind"], version_id),
            )
            connection.execute(
                "INSERT INTO index_aliases(project_id,dataset_kind,active_index_version,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(project_id,dataset_kind) DO UPDATE SET "
                "active_index_version=excluded.active_index_version, updated_at=excluded.updated_at",
                (version["project_id"], version["dataset_kind"], version_id, now),
            )
        return version

    def active_version(self, scope):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT v.* FROM index_aliases a JOIN index_versions v "
                "ON v.index_version=a.active_index_version "
                "WHERE a.project_id=? AND a.dataset_kind=?",
                (scope.project_id, scope.dataset_kind),
            ).fetchone()
        return dict(row) if row else None

    def get_version(self, version_id):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM index_versions WHERE index_version=?", (version_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_versions(self, scope):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM index_versions WHERE project_id=? AND dataset_kind=? "
                "ORDER BY created_at DESC", (scope.project_id, scope.dataset_kind),
            ).fetchall()
        return [dict(row) for row in rows]

    def activate(self, version_id):
        """Explicit rollback/switch to a retained ready or retired version."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute(
                "SELECT * FROM index_versions WHERE index_version=?", (version_id,)
            ).fetchone()
            if version is None:
                raise KeyError(version_id)
            if version["status"] not in {"ready", "retired"}:
                raise RuntimeError("Only successfully built versions can be activated")
            now = utcnow()
            connection.execute(
                "UPDATE index_versions SET status='ready', activated_at=?, retired_at=NULL "
                "WHERE index_version=?", (now, version_id),
            )
            connection.execute(
                "UPDATE index_versions SET status='retired', retired_at=? "
                "WHERE project_id=? AND dataset_kind=? AND index_version<>? AND status='ready'",
                (now, version["project_id"], version["dataset_kind"], version_id),
            )
            connection.execute(
                "INSERT INTO index_aliases(project_id,dataset_kind,active_index_version,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(project_id,dataset_kind) DO UPDATE SET "
                "active_index_version=excluded.active_index_version, updated_at=excluded.updated_at",
                (version["project_id"], version["dataset_kind"], version_id, now),
            )
        return dict(version)

    def mark_interrupted_builds_failed(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT index_version FROM index_versions WHERE status='building'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE index_versions SET status='failed', error_json=? "
                    "WHERE index_version=?",
                    (json.dumps({"type": "InterruptedBuild",
                                 "message": "build did not finish"}), row[0]),
                )
        return [row[0] for row in rows]
