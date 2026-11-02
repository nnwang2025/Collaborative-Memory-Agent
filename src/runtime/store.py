"""SQLite persistence for sessions, runs, events, tool calls and results.

The runtime tables are additive members of the existing evidence SQLite
database and use the same ``schema_migrations`` marker table.  Run events are
append-only and strictly ordered per run; terminal run rows are immutable.
Connections use ``BEGIN IMMEDIATE`` plus a bounded locked-retry so concurrent
sessions do not silently lose writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

RUN_STATUSES = (
    "queued", "running", "succeeded", "insufficient", "conflict",
    "failed", "timed_out", "cancelled", "interrupted",
)
TERMINAL_STATUSES = frozenset({
    "succeeded", "insufficient", "conflict", "failed",
    "timed_out", "cancelled", "interrupted",
})
ACTIVE_STATUSES = frozenset({"queued", "running"})
EVENT_TYPES = frozenset({
    "run_created", "run_started", "run_finished", "run_cancelled",
    "run_interrupted", "run_cancel_requested", "run_retry_scheduled",
    "model_requested", "model_completed", "model_skipped",
    "tool_started", "tool_completed", "tool_failed",
    "evidence_observed", "claim_submitted", "claim_verified",
    "conflict_detected", "retry_scheduled",
    "context_selected", "context_limit",
})
SCHEMA_VERSION = 4

RUNTIME_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sessions ("
    "session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
    "dataset_kind TEXT NOT NULL CHECK(dataset_kind IN ('fixture','live')), "
    "status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT)",
    "CREATE INDEX IF NOT EXISTS sessions_scope ON sessions(project_id,dataset_kind,created_at)",
    "CREATE TABLE IF NOT EXISTS session_messages ("
    "message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), "
    "seq INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant','system')), "
    "content TEXT NOT NULL, run_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', "
    "created_at TEXT NOT NULL, UNIQUE(session_id,seq))",
    "CREATE INDEX IF NOT EXISTS session_messages_order ON session_messages(session_id,seq)",
    "CREATE TABLE IF NOT EXISTS runs ("
    "run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), "
    "project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL CHECK(dataset_kind IN ('fixture','live')), "
    "question TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN "
    "('queued','running','succeeded','insufficient','conflict','failed','timed_out','cancelled','interrupted')), "
    "termination_reason TEXT, termination_detail TEXT, backend TEXT NOT NULL DEFAULT 'react', "
    "model_name TEXT, model_config_json TEXT NOT NULL DEFAULT '{}', budget_json TEXT NOT NULL DEFAULT '{}', "
    "retrieval_mode TEXT, fallback_reason TEXT, index_version TEXT, "
    "source_revision INTEGER, "
    "tool_call_count INTEGER NOT NULL DEFAULT 0, token_usage_json TEXT NOT NULL DEFAULT '{}', "
    "error_code TEXT, error_message TEXT, result_available INTEGER NOT NULL DEFAULT 0, "
    "idempotency_key TEXT, request_hash TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, "
    "cancel_requested_at TEXT, created_at TEXT NOT NULL, queued_at TEXT NOT NULL, "
    "started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL, "
    "UNIQUE(session_id,idempotency_key))",
    "CREATE INDEX IF NOT EXISTS runs_session_order ON runs(session_id,created_at)",
    "CREATE INDEX IF NOT EXISTS runs_status ON runs(status,created_at)",
    "CREATE INDEX IF NOT EXISTS runs_scope ON runs(project_id,dataset_kind,created_at)",
    "CREATE TABLE IF NOT EXISTS run_events ("
    "event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), "
    "event_seq INTEGER NOT NULL, event_type TEXT NOT NULL, timestamp TEXT NOT NULL, "
    "payload_json TEXT NOT NULL DEFAULT '{}', schema_version INTEGER NOT NULL DEFAULT 1, "
    "UNIQUE(run_id,event_seq))",
    "CREATE INDEX IF NOT EXISTS run_events_order ON run_events(run_id,event_seq)",
    "CREATE TABLE IF NOT EXISTS run_tool_calls ("
    "tool_call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), "
    "ordinal INTEGER NOT NULL, tool_name TEXT NOT NULL, query TEXT NOT NULL, "
    "filters_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL CHECK(status IN "
    "('started','completed','failed','timeout','cancelled')), "
    "attempt INTEGER NOT NULL DEFAULT 0, trace_step_index INTEGER, "
    "started_at TEXT NOT NULL, finished_at TEXT, elapsed_ms REAL, "
    "error_code TEXT, error_message TEXT, retryable INTEGER, "
    "result_summary_json TEXT NOT NULL DEFAULT '{}', UNIQUE(run_id,ordinal))",
    "CREATE INDEX IF NOT EXISTS run_tool_calls_order ON run_tool_calls(run_id,ordinal)",
    "CREATE TABLE IF NOT EXISTS run_evidence_refs ("
    "run_id TEXT NOT NULL REFERENCES runs(run_id), evidence_id TEXT NOT NULL, "
    "content_hash TEXT NOT NULL, tool_name TEXT, observed_at TEXT NOT NULL, "
    "metadata_json TEXT NOT NULL DEFAULT '{}', "
    "PRIMARY KEY(run_id,evidence_id,content_hash))",
    "CREATE INDEX IF NOT EXISTS run_evidence_order ON run_evidence_refs(run_id,observed_at)",
    "CREATE TABLE IF NOT EXISTS run_results ("
    "run_id TEXT PRIMARY KEY REFERENCES runs(run_id), status TEXT NOT NULL, "
    "termination_reason TEXT, termination_detail TEXT, answer TEXT NOT NULL, "
    "result_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS run_events_immutable_update "
    "BEFORE UPDATE ON run_events BEGIN "
    "SELECT RAISE(ABORT, 'run_events are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS run_events_immutable_delete "
    "BEFORE DELETE ON run_events BEGIN "
    "SELECT RAISE(ABORT, 'run_events are append-only'); END",
)

_SECRET_KEYS = {
    "api_key", "apikey", "authorization", "auth", "secret", "password",
    "credential", "credentials", "access_token", "refresh_token", "id_token",
}
_SECRET_KEY_PARTS = ("api_key", "apikey", "password", "secret_key", "access_token")
# Content hashes are SHA-256 hex strings.  They are runtime identity data, not
# bearer tokens, so the generic long-hex redaction must not destroy them.
_SAFE_IDENTIFIER_KEYS = {
    "content_hash", "evidence_id", "event_id", "message_id", "request_hash",
    "run_id", "session_id", "tool_call_id",
}
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)bearer\s+[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\b(?:sk|ak|key)-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
)
_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:\\[^\s\"',;]+"),
    re.compile(r"(?i)/(?:home|users|tmp|var/folders)/[^\s\"',;]+"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: str, limit: int = 2000) -> str:
    return value if len(value) <= limit else value[:limit] + "…[truncated]"


def sanitize(value, *, depth: int = 0):
    """Redact credentials, paths and oversized blobs before persistence."""
    if depth > 6:
        return "[depth-limited]"
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            name = str(key).lower()
            if name in _SECRET_KEYS or any(part in name for part in _SECRET_KEY_PARTS):
                cleaned[str(key)] = "[REDACTED]"
            elif name in _SAFE_IDENTIFIER_KEYS and isinstance(item, str):
                cleaned[str(key)] = _truncate(item)
            else:
                cleaned[str(key)] = sanitize(item, depth=depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_TEXT_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        for pattern in _PATH_PATTERNS:
            text = pattern.sub("[PATH]", text)
        return _truncate(text)
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if value is None or isinstance(value, (int, bool)):
        return value
    return _truncate(str(value))


def _safe_text(value, limit: int = 2000) -> str | None:
    if value is None:
        return None
    return _truncate(str(sanitize(str(value))), limit)


def _json(value) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False, separators=(",", ":"))


def _loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


class RuntimeStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        if self.path is None:
            from src.evidence_store import default_db_path
            self.path = default_db_path()

    # -- connections -----------------------------------------------------
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _write(self, action, *, retries: int = 6):
        delay = 0.02
        for attempt in range(retries):
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    ensure_runtime_schema(connection)
                    result = action(connection)
                    connection.commit()
                    return result
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if attempt + 1 >= retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        raise RuntimeError("unreachable")

    def _read(self, action, default=None):
        if not self.path.exists():
            return default
        with closing(self._connect()) as connection:
            return action(connection)

    # -- schema ----------------------------------------------------------
    def migrate(self):
        return self._write(lambda connection: 3)

    # -- sessions --------------------------------------------------------
    def create_session(self, project_id: str, dataset_kind: str = "live", session_id=None):
        session_id = session_id or str(uuid.uuid4())
        now = utcnow()

        def action(connection):
            connection.execute(
                "INSERT INTO sessions(session_id,project_id,dataset_kind,status,created_at,updated_at) "
                "VALUES (?,?,?,'open',?,?)",
                (session_id, project_id, dataset_kind, now, now))
            return _row(connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())

        return self._write(action)

    def get_session(self, session_id):
        return self._read(lambda connection: _row(connection.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()))

    def close_session(self, session_id):
        def action(connection):
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return None
            if row["status"] != "closed":
                now = utcnow()
                connection.execute(
                    "UPDATE sessions SET status='closed',closed_at=?,updated_at=? "
                    "WHERE session_id=?", (now, now, session_id))
            return _row(connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())

        return self._write(action)

    def append_session_message(self, session_id, role, content, run_id=None, metadata=None):
        now = utcnow()
        content = _truncate(str(content or ""), 4000)

        def action(connection):
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM session_messages WHERE session_id=?",
                (session_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO session_messages(message_id,session_id,seq,role,content,run_id,"
                "metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), session_id, seq, role, content, run_id,
                 _json(metadata or {}), now))
            connection.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            return seq

        return self._write(action)

    def list_session_messages(self, session_id, *, limit=50, after_seq=0):
        limit = max(1, min(int(limit), 200))

        def action(connection):
            rows = connection.execute(
                "SELECT * FROM session_messages WHERE session_id=? AND seq>? "
                "ORDER BY seq ASC LIMIT ?", (session_id, int(after_seq), limit)).fetchall()
            return [dict(row) for row in rows]

        return self._read(action, [])

    # -- runs ------------------------------------------------------------
    def create_run(self, *, session_id, project_id, dataset_kind, question, request_hash,
                   backend="react", model_name=None, model_config=None, budget=None,
                   retrieval_mode=None, idempotency_key=None, timeout_seconds=None):
        now = utcnow()

        def action(connection):
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if session is None:
                from src.runtime.errors import SessionNotFound
                raise SessionNotFound(session_id)
            if (session["project_id"], session["dataset_kind"]) != (project_id, dataset_kind):
                from src.runtime.errors import ScopeMismatch
                raise ScopeMismatch("session scope is immutable")
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM runs WHERE session_id=? AND idempotency_key=?",
                    (session_id, idempotency_key)).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        from src.runtime.errors import IdempotencyConflict
                        raise IdempotencyConflict(idempotency_key)
                    return _row(existing), False
            if session["status"] != "open":
                from src.runtime.errors import SessionClosed
                raise SessionClosed(session_id)
            active = connection.execute(
                "SELECT run_id FROM runs WHERE session_id=? AND status IN ('queued','running') "
                "ORDER BY created_at LIMIT 1", (session_id,)).fetchone()
            if active is not None:
                from src.runtime.errors import SessionBusy
                raise SessionBusy(active["run_id"])
            run_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO runs(run_id,session_id,project_id,dataset_kind,question,status,"
                "backend,model_name,model_config_json,budget_json,retrieval_mode,idempotency_key,"
                "request_hash,created_at,queued_at,updated_at) "
                "VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)",
                (run_id, session_id, project_id, dataset_kind, question, backend,
                 model_name, _json(model_config or {}), _json(budget or {}),
                 retrieval_mode, idempotency_key, request_hash, now, now, now))
            self._insert_event(connection, run_id, "run_created", {
                "session_id": session_id, "project_id": project_id,
                "dataset_kind": dataset_kind, "backend": backend,
                "retrieval_mode": retrieval_mode,
            })
            return _row(connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()), True

        return self._write(action)

    def get_run(self, run_id):
        return self._read(lambda connection: _row(connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()))

    def list_runs(self, session_id=None, *, status=None, project_id=None,
                  limit=20, offset=0):
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        conditions, args = [], []
        if session_id:
            conditions.append("session_id=?")
            args.append(session_id)
        if status:
            conditions.append("status=?")
            args.append(status)
        if project_id:
            conditions.append("project_id=?")
            args.append(project_id)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        args.extend([limit, offset])

        def action(connection):
            rows = connection.execute(
                "SELECT * FROM runs" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                args).fetchall()
            return [dict(row) for row in rows]

        return self._read(action, [])

    def count_runs(self, session_id=None, *, status=None):
        conditions, args = [], []
        if session_id:
            conditions.append("session_id=?")
            args.append(session_id)
        if status:
            conditions.append("status=?")
            args.append(status)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        return self._read(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM runs" + where, args).fetchone()[0], 0)

    def start_run(self, run_id):
        now = utcnow()

        def action(connection):
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            if row["status"] in TERMINAL_STATUSES:
                return _row(row)
            if row["status"] != "queued":
                from src.runtime.errors import InvalidRunTransition
                raise InvalidRunTransition(f"{row['status']} -> running")
            connection.execute(
                "UPDATE runs SET status='running',started_at=?,updated_at=? WHERE run_id=?",
                (now, now, run_id))
            self._insert_event(connection, run_id, "run_started", {})
            return _row(connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

        return self._write(action)

    def complete_run(self, run_id, *, status, termination_reason, termination_detail,
                     result, answer, tool_call_count=0, usage=None, retrieval_mode=None,
                     fallback_reason=None, index_version=None, source_revision=None,
                     model_config=None):
        if status not in TERMINAL_STATUSES:
            raise ValueError("complete_run requires a terminal status")
        now = utcnow()

        def action(connection):
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            if row["status"] in TERMINAL_STATUSES:
                return _row(row)
            connection.execute(
                "UPDATE runs SET status=?,termination_reason=?,termination_detail=?,"
                "tool_call_count=?,token_usage_json=?,retrieval_mode=COALESCE(?,retrieval_mode),"
                "fallback_reason=COALESCE(?,fallback_reason),"
                "index_version=COALESCE(?,index_version),"
                "source_revision=COALESCE(?,source_revision),"
                "model_config_json=COALESCE(?,model_config_json),result_available=1,"
                "finished_at=?,updated_at=? WHERE run_id=?",
                (status, termination_reason, _safe_text(termination_detail),
                 int(tool_call_count), _json(usage or {}), retrieval_mode,
                 fallback_reason, index_version, source_revision,
                 _json(model_config) if model_config is not None else None,
                 now, now, run_id))
            connection.execute(
                "INSERT INTO run_results(run_id,status,termination_reason,termination_detail,"
                "answer,result_json,created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,"
                "termination_reason=excluded.termination_reason,"
                "termination_detail=excluded.termination_detail,answer=excluded.answer,"
                "result_json=excluded.result_json,created_at=excluded.created_at",
                (run_id, status, termination_reason, _safe_text(termination_detail),
                 _truncate(str(answer or ""), 12000), _json(result), now))
            # The assistant turn lands in the same transaction as the result so a
            # completed run is never observable without its session history.
            message_seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM session_messages WHERE session_id=?",
                (row["session_id"],)).fetchone()[0]
            connection.execute(
                "INSERT INTO session_messages(message_id,session_id,seq,role,content,"
                "run_id,metadata_json,created_at) VALUES (?,?,?,'assistant',?,?,?,?)",
                (str(uuid.uuid4()), row["session_id"], message_seq,
                 _truncate(str(answer or ""), 4000), run_id,
                 _json({"status": status, "termination_reason": termination_reason,
                        "result_available": True}), now))
            connection.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id=?",
                (now, row["session_id"]))
            self._insert_event(connection, run_id, "run_finished", {
                "status": status, "termination_reason": termination_reason,
                "termination_detail": termination_detail,
                "tool_call_count": int(tool_call_count),
                "usage": sanitize(usage or {}),
            })
            return _row(connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

        return self._write(action)

    def finish_run(self, run_id, *, status, termination_reason=None,
                   termination_detail=None, error_code=None, error_message=None):
        """Finish a run that has no persisted successful result."""
        if status not in TERMINAL_STATUSES:
            raise ValueError("finish_run requires a terminal status")
        now = utcnow()
        event_type = {
            "cancelled": "run_cancelled",
            "interrupted": "run_interrupted",
        }.get(status, "run_finished")

        def action(connection):
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None, False
            if row["status"] in TERMINAL_STATUSES:
                return _row(row), False
            connection.execute(
                "UPDATE runs SET status=?,termination_reason=?,termination_detail=?,"
                "error_code=?,error_message=?,finished_at=?,updated_at=? WHERE run_id=?",
                (status, termination_reason, _safe_text(termination_detail),
                 error_code, _safe_text(error_message, 500),
                 now, now, run_id))
            payload = {"status": status, "termination_reason": termination_reason,
                       "termination_detail": termination_detail,
                       "error_code": error_code}
            self._insert_event(connection, run_id, event_type, payload)
            return _row(connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()), True

        return self._write(action)

    def request_cancel(self, run_id):
        now = utcnow()

        def action(connection):
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None, False
            if row["status"] in TERMINAL_STATUSES:
                return _row(row), False
            if row["status"] == "queued":
                connection.execute(
                    "UPDATE runs SET status='cancelled',termination_reason='cancelled',"
                    "cancel_requested=1,cancel_requested_at=?,finished_at=?,updated_at=? "
                    "WHERE run_id=?", (now, now, now, run_id))
                self._insert_event(connection, run_id, "run_cancelled", {
                    "status": "cancelled", "termination_reason": "cancelled",
                    "before_start": True,
                })
                return _row(connection.execute(
                    "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()), True
            connection.execute(
                "UPDATE runs SET cancel_requested=1,cancel_requested_at=?,updated_at=? "
                "WHERE run_id=?", (now, now, run_id))
            self._insert_event(connection, run_id, "run_cancel_requested", {})
            return _row(connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()), True

        return self._write(action)

    def cancel_requested(self, run_id) -> bool:
        row = self.get_run(run_id)
        return bool(row and row["cancel_requested"])

    def recover_interrupted(self):
        """Mark leftover queued/running rows interrupted; never rerun them."""
        now = utcnow()

        def action(connection):
            rows = connection.execute(
                "SELECT * FROM runs WHERE status IN ('queued','running') "
                "ORDER BY created_at").fetchall()
            recovered = []
            for row in rows:
                connection.execute(
                    "UPDATE runs SET status='interrupted',termination_reason='interrupted',"
                    "error_code='process_restarted',"
                    "error_message='Runtime restarted before this run reached a terminal state.',"
                    "finished_at=?,updated_at=? WHERE run_id=? AND status IN ('queued','running')",
                    (now, now, row["run_id"]))
                self._insert_event(connection, row["run_id"], "run_interrupted", {
                    "status": "interrupted", "termination_reason": "interrupted",
                    "reason": "process_restarted",
                })
                recovered.append(row["run_id"])
            return recovered

        return self._write(action)

    # -- events ----------------------------------------------------------
    @staticmethod
    def _insert_event(connection, run_id, event_type, payload):
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {event_type}")
        seq = connection.execute(
            "SELECT COALESCE(MAX(event_seq),0)+1 FROM run_events WHERE run_id=?",
            (run_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO run_events(event_id,run_id,event_seq,event_type,timestamp,"
            "payload_json,schema_version) VALUES (?,?,?,?,?,?,1)",
            (str(uuid.uuid4()), run_id, seq, event_type, utcnow(), _json(payload)))
        return seq

    def append_event(self, run_id, event_type, payload=None):
        def action(connection):
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] in TERMINAL_STATUSES:
                # Instrumentation that arrives after the terminal transition is
                # discarded; terminal event history is immutable.
                return None
            return self._insert_event(connection, run_id, event_type, payload or {})

        return self._write(action)

    def list_events(self, run_id, *, after_seq=0, limit=500):
        limit = max(1, min(int(limit), 2000))

        def action(connection):
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id=? AND event_seq>? "
                "ORDER BY event_seq ASC LIMIT ?",
                (run_id, int(after_seq), limit)).fetchall()
            return [dict(row) for row in rows]

        return self._read(action, [])

    def event_count(self, run_id):
        return self._read(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=?", (run_id,)).fetchone()[0], 0)

    # -- tool calls ------------------------------------------------------
    def start_tool_call(self, *, tool_call_id, run_id, tool_name, query, filters=None,
                        attempt=0, trace_step_index=None):
        def action(connection):
            run = connection.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["status"] in TERMINAL_STATUSES:
                return None
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM run_tool_calls WHERE run_id=?",
                (run_id,)).fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO run_tool_calls(tool_call_id,run_id,ordinal,tool_name,"
                "query,filters_json,status,attempt,trace_step_index,started_at) "
                "VALUES (?,?,?,?,?,?, 'started',?,?,?)",
                (tool_call_id, run_id, ordinal, tool_name, _truncate(query, 2000),
                 _json(filters or {}), int(attempt), trace_step_index, utcnow()))
            return ordinal

        return self._write(action)

    def finish_tool_call(self, tool_call_id, *, run_id, status, elapsed_ms=None,
                         error_code=None, error_message=None, retryable=None,
                         result_summary=None, trace_step_index=None):
        if status not in {"completed", "failed", "timeout", "cancelled"}:
            raise ValueError("invalid tool call status")

        def action(connection):
            run = connection.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["status"] in TERMINAL_STATUSES:
                return False
            connection.execute(
                "UPDATE run_tool_calls SET status=?,finished_at=?,elapsed_ms=?,"
                "error_code=?,error_message=?,retryable=?,result_summary_json=?,"
                "trace_step_index=COALESCE(?,trace_step_index) "
                "WHERE tool_call_id=? AND run_id=?",
                (status, utcnow(), elapsed_ms, error_code,
                 _safe_text(error_message, 500),
                 None if retryable is None else int(bool(retryable)),
                 _json(result_summary or {}), trace_step_index, tool_call_id, run_id))
            return True

        return self._write(action)

    def list_tool_calls(self, run_id):
        return self._read(lambda connection: [dict(row) for row in connection.execute(
            "SELECT * FROM run_tool_calls WHERE run_id=? ORDER BY ordinal",
            (run_id,)).fetchall()], [])

    # -- evidence refs / results ----------------------------------------
    def record_evidence_ref(self, run_id, evidence_id, content_hash, *, tool_name=None,
                            metadata=None):
        def action(connection):
            run = connection.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["status"] in TERMINAL_STATUSES:
                return False
            connection.execute(
                "INSERT OR IGNORE INTO run_evidence_refs(run_id,evidence_id,content_hash,"
                "tool_name,observed_at,metadata_json) VALUES (?,?,?,?,?,?)",
                (run_id, evidence_id, content_hash, tool_name, utcnow(),
                 _json(metadata or {})))
            return True

        return self._write(action)

    def list_evidence_refs(self, run_id):
        return self._read(lambda connection: [dict(row) for row in connection.execute(
            "SELECT * FROM run_evidence_refs WHERE run_id=? ORDER BY observed_at,evidence_id",
            (run_id,)).fetchall()], [])

    def get_result(self, run_id):
        def action(connection):
            row = connection.execute(
                "SELECT * FROM run_results WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["result"] = _loads(result.pop("result_json"), {})
            return result

        return self._read(action)

    def update_run_snapshot(self, run_id, *, model_name=None, model_config=None,
                            retrieval_mode=None, fallback_reason=None,
                            index_version=None, source_revision=None):
        def action(connection):
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] in TERMINAL_STATUSES:
                return False
            connection.execute(
                "UPDATE runs SET model_name=COALESCE(?,model_name),"
                "model_config_json=COALESCE(?,model_config_json),"
                "retrieval_mode=COALESCE(?,retrieval_mode),"
                "fallback_reason=COALESCE(?,fallback_reason),"
                "index_version=COALESCE(?,index_version),"
                "source_revision=COALESCE(?,source_revision),updated_at=? WHERE run_id=?",
                (model_name, _json(model_config) if model_config is not None else None,
                 retrieval_mode, fallback_reason, index_version, source_revision,
                 utcnow(), run_id))
            return True

        return self._write(action)


def ensure_runtime_schema(connection):
    """Create additive runtime tables and apply incremental migrations."""
    for statement in RUNTIME_SCHEMA:
        connection.execute(statement)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    if "fallback_reason" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN fallback_reason TEXT")
    connection.execute(
        "INSERT INTO schema_migrations(version,applied_at,description) VALUES (3,?,?) "
        "ON CONFLICT(version) DO NOTHING",
        (utcnow(), "agent runtime sessions, runs, append-only events, tool calls, results"),
    )
    connection.execute(
        "INSERT INTO schema_migrations(version,applied_at,description) VALUES (4,?,?) "
        "ON CONFLICT(version) DO NOTHING",
        (utcnow(), "run retrieval fallback_reason metadata"),
    )
