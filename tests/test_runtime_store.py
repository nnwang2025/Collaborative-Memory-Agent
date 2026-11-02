"""Runtime SQLite schema, append-only events, recovery and lock behavior."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from src.runtime.store import RuntimeStore, TERMINAL_STATUSES


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="cma-runtime-store-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        self.store = RuntimeStore(self.db)
        self.session = self.store.create_session("runtime-project", "live")

    def make_run(self, question="question", key=None, session=None):
        run, created = self.store.create_run(
            session_id=(session or self.session)["session_id"],
            project_id="runtime-project",
            dataset_kind="live",
            question=question,
            request_hash="hash-" + question,
            idempotency_key=key,
        )
        self.assertTrue(created)
        return run

    def test_schema_marker_and_append_only_event_order(self):
        run = self.make_run()
        self.store.append_event(run["run_id"], "model_requested", {"round": 1})
        self.store.append_event(run["run_id"], "model_completed", {"round": 1})
        events = self.store.list_events(run["run_id"])
        self.assertEqual([event["event_type"] for event in events],
                         ["run_created", "model_requested", "model_completed"])
        self.assertEqual([event["event_seq"] for event in events], [1, 2, 3])
        with closing(sqlite3.connect(self.db)) as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            self.assertGreaterEqual(version, 4)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO run_events(event_id,run_id,event_seq,event_type,"
                    "timestamp,payload_json) VALUES ('x',?,3,'x','now','{}')",
                    (run["run_id"],))

    def test_terminal_status_is_immutable_and_result_is_queryable(self):
        run = self.make_run()
        self.store.start_run(run["run_id"])
        done = self.store.complete_run(
            run["run_id"], status="succeeded", termination_reason="sufficient",
            termination_detail=None, result={"answer": "ok"},
            answer="ok", tool_call_count=2, usage={"input_tokens": 3},
            retrieval_mode="hybrid", fallback_reason="dense_unavailable",
            index_version="index-2", source_revision=7)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["fallback_reason"], "dense_unavailable")
        self.assertEqual(done["retrieval_mode"], "hybrid")
        self.assertEqual(done["index_version"], "index-2")
        self.assertEqual(done["source_revision"], 7)
        again = self.store.finish_run(
            run["run_id"], status="failed", termination_reason="system_error")
        self.assertEqual(again[0]["status"], "succeeded")
        cancelled, changed = self.store.request_cancel(run["run_id"])
        self.assertFalse(changed)
        self.assertEqual(cancelled["status"], "succeeded")
        stored = self.store.get_result(run["run_id"])
        self.assertEqual(stored["answer"], "ok")
        self.assertEqual(stored["result"]["answer"], "ok")
        self.assertEqual(self.store.get_run(run["run_id"])["tool_call_count"], 2)

    def test_tool_call_evidence_ref_and_trace_association(self):
        run = self.make_run()
        self.store.start_run(run["run_id"])
        self.store.start_tool_call(
            tool_call_id="call-1", run_id=run["run_id"], tool_name="search_docs",
            query="q", filters={"source_types": ["document"]}, attempt=0,
            trace_step_index=2)
        self.store.finish_tool_call(
            "call-1", run_id=run["run_id"], status="completed", elapsed_ms=4.5,
            result_summary={"evidence_count": 1}, trace_step_index=2)
        self.store.record_evidence_ref(
            run["run_id"], "ev_" + "a" * 64, "b" * 64,
            tool_name="search_docs", metadata={"retrieval_mode": "bm25"})
        calls = self.store.list_tool_calls(run["run_id"])
        self.assertEqual(calls[0]["tool_name"], "search_docs")
        self.assertEqual(calls[0]["trace_step_index"], 2)
        self.assertEqual(calls[0]["status"], "completed")
        refs = self.store.list_evidence_refs(run["run_id"])
        self.assertEqual(len(refs), 1)
        self.assertIn("bm25", refs[0]["metadata_json"])

    def test_event_payload_is_sanitized_before_persistence(self):
        run = self.make_run()
        self.store.append_event(run["run_id"], "tool_failed", {
            "api_key": "sk-live-secret-123456",
            "headers": {"Authorization": "Bearer supersecret"},
            "error": "failed at C:\\Users\\admin\\private\\token.txt",
            "nested": {"access_token": "abc"},
            "content_hash": "a" * 64,
        })
        raw = self.store.list_events(run["run_id"])[-1]["payload_json"]
        self.assertNotIn("sk-live-secret-123456", raw)
        self.assertNotIn("supersecret", raw)
        self.assertNotIn("C:\\Users", raw)
        self.assertIn("[REDACTED]", raw)
        self.assertIn("[PATH]", raw)
        # Evidence content hashes are identity data, not lease/API tokens.
        self.assertIn("a" * 64, raw)

        self.store.start_tool_call(
            tool_call_id="sanitized-call", run_id=run["run_id"],
            tool_name="search_docs", query="q")
        self.store.finish_tool_call(
            "sanitized-call", run_id=run["run_id"], status="failed",
            error_message="Bearer supersecret at C:\\Users\\admin\\private\\token.txt")
        self.store.finish_run(
            run["run_id"], status="failed", termination_reason="system_error",
            termination_detail="failed at C:\\Users\\admin\\private\\token.txt",
            error_code="system_error",
            error_message="Bearer supersecret at C:\\Users\\admin\\private\\token.txt")
        row = self.store.get_run(run["run_id"])
        self.assertNotIn("supersecret", row["error_message"])
        self.assertNotIn("C:\\Users", row["error_message"])
        self.assertIn("[REDACTED]", row["error_message"])
        self.assertIn("[PATH]", row["error_message"])
        self.assertNotIn("C:\\Users", row["termination_detail"])
        call = self.store.list_tool_calls(run["run_id"])[0]
        self.assertNotIn("supersecret", call["error_message"])
        self.assertNotIn("C:\\Users", call["error_message"])

    def test_restart_marks_only_active_runs_interrupted(self):
        active = self.make_run("active")
        terminal = self.make_run(
            "terminal", session=self.store.create_session("runtime-project", "live"))
        self.store.start_run(active["run_id"])
        self.store.start_run(terminal["run_id"])
        self.store.complete_run(
            terminal["run_id"], status="succeeded", termination_reason="sufficient",
            termination_detail=None, result={}, answer="done")
        recovered = self.store.recover_interrupted()
        self.assertEqual(recovered, [active["run_id"]])
        self.assertEqual(self.store.get_run(active["run_id"])["status"], "interrupted")
        self.assertEqual(self.store.get_run(terminal["run_id"])["status"], "succeeded")
        self.assertEqual(
            self.store.list_events(active["run_id"])[-1]["event_type"],
            "run_interrupted")

    def test_terminal_run_rejects_late_instrumentation_and_events_are_append_only(self):
        run = self.make_run()
        self.store.start_run(run["run_id"])
        self.store.complete_run(
            run["run_id"], status="succeeded", termination_reason="sufficient",
            termination_detail=None, result={}, answer="done")
        self.assertIsNone(self.store.append_event(
            run["run_id"], "model_completed", {"round": 9}))
        self.assertIsNone(self.store.start_tool_call(
            tool_call_id="late", run_id=run["run_id"], tool_name="search_docs",
            query="late"))
        self.assertFalse(self.store.record_evidence_ref(
            run["run_id"], "ev_" + "e" * 64, "f" * 64, tool_name="search_docs"))
        self.assertEqual(self.store.list_tool_calls(run["run_id"]), [])
        with closing(sqlite3.connect(self.db)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE run_events SET event_type='tampered' WHERE run_id=?",
                    (run["run_id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM run_events WHERE run_id=?", (run["run_id"],))

    def test_locked_database_retries_instead_of_dropping_write(self):
        run = self.make_run()
        started = threading.Event()
        result = {}

        def writer():
            started.set()
            result["seq"] = self.store.append_event(
                run["run_id"], "model_requested", {"round": 2})

        with closing(sqlite3.connect(self.db, timeout=15)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(
                "UPDATE runs SET updated_at='held' WHERE run_id=?", (run["run_id"],))
            worker = threading.Thread(target=writer)
            worker.start()
            self.assertTrue(started.wait(2))
            time.sleep(0.25)
            self.assertTrue(worker.is_alive(), "writer should wait for the lock")
            holder.commit()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(result.get("seq"), int)
        self.assertGreater(result["seq"], 1)


if __name__ == "__main__":
    unittest.main()
