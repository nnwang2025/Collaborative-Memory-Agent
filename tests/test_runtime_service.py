"""RunService state machine, idempotency, concurrency, cancel, timeout, recovery."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.evidence import EvidenceScope
from src.runtime.config import RuntimeConfig
from src.runtime.errors import (
    IdempotencyConflict, RunCancelled, ScopeMismatch, SessionBusy,
)
from src.runtime.service import RunService
from src.runtime.store import RuntimeStore

from tests.runtime_helpers import (
    agent_factory, event_types, make_evidence, make_result, simple_behaviour,
    wait_status,
)

PROJECT = "runtime-project"


class RuntimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="cma-runtime-service-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "runtime.sqlite3"
        self.scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")
        self.services = []

    def service(self, behaviour, *, workers=4, history_runs=3):
        service = RunService(
            self.db,
            agent_factory=agent_factory(behaviour),
            config=RuntimeConfig(db_path=self.db, max_workers=workers,
                                 session_history_runs=history_runs),
        )
        self.services.append(service)
        self.addCleanup(service.close)
        return service

    def session(self, service, project=PROJECT):
        return service.sessions.create(project, "live")

    def test_lifecycle_persists_result_events_tool_and_evidence_metadata(self):
        evidence = make_evidence(self.db, self.scope)
        service = self.service(simple_behaviour(refs_factory=lambda: [evidence]))
        session = self.session(service)
        run, created = service.submit(session["session_id"], "why")
        self.assertTrue(created)
        done = wait_status(service, run["run_id"], {"succeeded"})
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["termination_reason"], "sufficient")
        self.assertEqual(done["project_id"], PROJECT)
        self.assertEqual(done["dataset_kind"], "live")
        self.assertTrue(done["result_available"])
        self.assertEqual(done["retrieval_mode"], "hybrid")
        self.assertEqual(done["index_version"], "index-1")
        self.assertEqual(done["source_revision"], 1)
        self.assertEqual(json.loads(done["token_usage_json"])["input_tokens"], 1)
        self.assertEqual(json.loads(done["budget_json"])["deadline_seconds"],
                         service.config.run_timeout_seconds)

        events = service.events(run["run_id"], project_id=PROJECT)
        types = [event["event_type"] for event in events]
        self.assertEqual(types[0], "run_created")
        self.assertIn("run_started", types)
        self.assertIn("model_requested", types)
        self.assertIn("model_completed", types)
        self.assertIn("evidence_observed", types)
        self.assertEqual(types[-1], "run_finished")
        self.assertEqual([event["event_seq"] for event in events],
                         list(range(1, len(events) + 1)))

        refs = service.evidence_refs(run["run_id"], project_id=PROJECT)
        self.assertEqual(refs[0]["evidence_id"], evidence.evidence_id)
        self.assertEqual(refs[0]["content_hash"], evidence.content_hash)
        stored = service.result(run["run_id"], project_id=PROJECT)[1]
        self.assertEqual(stored["answer"], "runtime answer")
        messages = service.sessions.messages(session["session_id"])
        self.assertEqual([message["role"] for message in messages],
                         ["user", "assistant"])
        self.assertEqual(messages[-1]["run_id"], run["run_id"])

    def test_business_termination_reasons_are_distinct_statuses(self):
        service = self.service(simple_behaviour())
        cases = {
            "insufficient": "insufficient",
            "conflict": "conflict",
            "tool_failure": "failed",
            "budget": "failed",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                # Rebuild the scripted behaviour for each reason.
                service.agent_factory = agent_factory(simple_behaviour(reason=reason))
                session = self.session(service)
                run, _ = service.submit(session["session_id"], "q-" + reason)
                done = wait_status(service, run["run_id"], {"succeeded", "insufficient",
                                                             "conflict", "failed"})
                self.assertEqual(done["status"], expected)
                self.assertEqual(done["termination_reason"], reason)

    def test_idempotency_returns_same_run_and_rejects_changed_content(self):
        service = self.service(simple_behaviour())
        session = self.session(service)
        first, created = service.submit(session["session_id"], "same", idempotency_key="key-1")
        self.assertTrue(created)
        wait_status(service, first["run_id"], {"succeeded"})
        replay, created = service.submit(session["session_id"], "same", idempotency_key="key-1")
        self.assertFalse(created)
        self.assertEqual(replay["run_id"], first["run_id"])
        with self.assertRaises(IdempotencyConflict):
            service.submit(session["session_id"], "different", idempotency_key="key-1")
        messages = service.sessions.messages(session["session_id"])
        self.assertEqual(len(messages), 2)  # one user, one assistant

    def test_same_session_is_serial_and_different_sessions_run_concurrently(self):
        started = threading.Event()
        release = threading.Event()
        started_count = {"n": 0}

        def blocking(agent, question, context):
            started_count["n"] += 1
            started.set()
            release.wait(5)
            return make_result(agent.scope, answer="released")

        service = self.service(blocking, workers=2)
        first_session = self.session(service)
        first, _ = service.submit(first_session["session_id"], "first")
        self.assertTrue(started.wait(2))
        with self.assertRaises(SessionBusy):
            service.submit(first_session["session_id"], "second")
        release.set()
        wait_status(service, first["run_id"], {"succeeded"})
        # The session is free again after the terminal state.
        next_run, _ = service.submit(first_session["session_id"], "third")
        wait_status(service, next_run["run_id"], {"succeeded"})

        # Independent sessions can be inside the worker at the same time.
        started.clear()
        release.clear()
        started_count["n"] = 0

        def blocking_two(agent, question, context):
            started_count["n"] += 1
            if started_count["n"] == 2:
                started.set()
            release.wait(5)
            return make_result(agent.scope, answer="ok")

        service.agent_factory = agent_factory(blocking_two)
        one = self.session(service)
        two = self.session(service)
        run_one, _ = service.submit(one["session_id"], "one")
        run_two, _ = service.submit(two["session_id"], "two")
        self.assertTrue(started.wait(3), "both sessions should run concurrently")
        release.set()
        self.assertEqual(wait_status(service, run_one["run_id"], {"succeeded"})["status"],
                         "succeeded")
        self.assertEqual(wait_status(service, run_two["run_id"], {"succeeded"})["status"],
                         "succeeded")

    def test_cancel_before_start_and_after_completion(self):
        started = threading.Event()
        release = threading.Event()

        def blocking(agent, question, context):
            started.set()
            release.wait(5)
            return make_result(agent.scope)

        service = self.service(blocking, workers=1)
        first_session = self.session(service)
        second_session = self.session(service)
        first, _ = service.submit(first_session["session_id"], "blocking")
        self.assertTrue(started.wait(2))
        queued, _ = service.submit(second_session["session_id"], "queued")
        self.assertEqual(queued["status"], "queued")
        cancelled, changed = service.cancel(queued["run_id"], project_id=PROJECT)
        self.assertTrue(changed)
        self.assertEqual(cancelled["status"], "cancelled")
        release.set()
        wait_status(service, first["run_id"], {"succeeded"})
        wait_status(service, queued["run_id"], {"cancelled"})
        self.assertNotIn("run_started", event_types(service, queued["run_id"]))

        completed, _ = service.submit(second_session["session_id"], "completed")
        wait_status(service, completed["run_id"], {"succeeded"})
        row, changed = service.cancel(completed["run_id"], project_id=PROJECT)
        self.assertFalse(changed)
        self.assertEqual(row["status"], "succeeded")

    def test_running_cancel_is_cooperative_and_terminal(self):
        started = threading.Event()

        def cancellable(agent, question, context):
            started.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if agent.cancel_check():
                    raise RunCancelled()
                time.sleep(0.01)
            return make_result(agent.scope)

        service = self.service(cancellable)
        session = self.session(service)
        run, _ = service.submit(session["session_id"], "cancel me")
        self.assertTrue(started.wait(2))
        row, changed = service.cancel(run["run_id"], project_id=PROJECT)
        self.assertTrue(changed)
        done = wait_status(service, run["run_id"], {"cancelled"})
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(done["termination_reason"], "cancelled")
        types = event_types(service, run["run_id"])
        self.assertIn("run_cancel_requested", types)
        self.assertIn("run_cancelled", types)
        self.assertNotIn("run_finished", types)
        self.assertIsNone(service.result(run["run_id"], project_id=PROJECT)[1])

    def test_timeout_at_cooperative_boundary_is_not_recorded_as_cancel(self):
        started = threading.Event()

        def cooperative(agent, question, context):
            started.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if agent.cancel_check():
                    raise RunCancelled()
                time.sleep(0.005)
            return make_result(agent.scope)

        service = self.service(cooperative)
        session = self.session(service)
        run, _ = service.submit(session["session_id"], "short", timeout_seconds=0.05)
        self.assertTrue(started.wait(2))
        done = wait_status(service, run["run_id"], {"timed_out"}, timeout=2)
        self.assertEqual(done["status"], "timed_out")
        self.assertEqual(done["termination_reason"], "timeout")
        types = event_types(service, run["run_id"])
        self.assertNotIn("run_cancelled", types)
        self.assertEqual(types[-1], "run_finished")

    def test_timeout_is_terminal_and_discards_late_result(self):
        def slow(agent, question, context):
            time.sleep(0.6)
            return make_result(agent.scope, answer="late")

        service = self.service(slow)
        session = self.session(service)
        run, _ = service.submit(session["session_id"], "slow", timeout_seconds=0.15)
        done = wait_status(service, run["run_id"], {"timed_out"}, timeout=2)
        self.assertEqual(done["status"], "timed_out")
        self.assertEqual(done["termination_reason"], "timeout")
        time.sleep(0.7)
        again = service.store.get_run(run["run_id"])
        self.assertEqual(again["status"], "timed_out")
        self.assertFalse(again["result_available"])
        self.assertIsNone(service.result(run["run_id"], project_id=PROJECT)[1])
        self.assertIn("run_finished", event_types(service, run["run_id"]))

    def test_scope_is_immutable_after_session_creation(self):
        service = self.service(simple_behaviour())
        session = self.session(service)
        with self.assertRaises(ScopeMismatch):
            service.submit(session["session_id"], "q", project_id="other-project")
        with self.assertRaises(ScopeMismatch):
            service.submit(session["session_id"], "q", dataset_kind="fixture")
        # No run was created by the rejected requests; the session scope is unchanged.
        self.assertEqual(service.sessions.runs(session["session_id"]), [])
        stored = service.sessions.get(session["session_id"])
        self.assertEqual((stored["project_id"], stored["dataset_kind"]),
                         (PROJECT, "live"))

    def test_restart_recovery_marks_interrupted_and_keeps_terminal_history(self):
        store = RuntimeStore(self.db)
        active_session = store.create_session(PROJECT, "live")
        terminal_session = store.create_session(PROJECT, "live")
        active, _ = store.create_run(
            session_id=active_session["session_id"], project_id=PROJECT,
            dataset_kind="live", question="active", request_hash="h1")
        store.start_run(active["run_id"])
        terminal, _ = store.create_run(
            session_id=terminal_session["session_id"], project_id=PROJECT,
            dataset_kind="live", question="terminal", request_hash="h2")
        store.start_run(terminal["run_id"])
        store.complete_run(terminal["run_id"], status="succeeded",
                           termination_reason="sufficient", termination_detail=None,
                           result={"answer": "kept"}, answer="kept")

        restarted = RunService(self.db, agent_factory=agent_factory(simple_behaviour()),
                               config=RuntimeConfig(db_path=self.db))
        self.addCleanup(restarted.close)
        recovered = restarted.get_run(active["run_id"], project_id=PROJECT)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["error_code"], "process_restarted")
        self.assertEqual(restarted.get_run(terminal["run_id"], project_id=PROJECT)["status"],
                         "succeeded")
        self.assertEqual(restarted.result(terminal["run_id"], project_id=PROJECT)[1]["answer"],
                         "kept")
        self.assertEqual(
            [event["event_type"] for event in restarted.events(
                active["run_id"], project_id=PROJECT)][-1],
            "run_interrupted")

    def test_tool_failure_retry_and_tool_call_rows_are_persisted(self):
        from src.agents.react_agent import ReActAgent
        from src.investigation import ToolError

        def factory(*, scope, observer, cancel_check, budget):
            with patch("src.agents.react_agent._resolve_api_key", return_value="test-key"), \
                    patch("src.agents.answer_agent.resolve_api_key", return_value=""):
                agent = ReActAgent(scope=scope, db_path=self.db, budget=budget,
                                   observer=observer, cancel_check=cancel_check)
            agent.api_key = "test-key"
            attempts = {"n": 0}

            def execute(tool, query, filters=None, timeout=None):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return {
                        "tool": tool, "evidence": [], "sources": [],
                        "retrieval_diagnostics": {"effective_mode": "bm25"},
                        "error": ToolError(
                            error_code="tool_timeout", retryable=True,
                            message="tool timed out", tool_name=tool,
                            elapsed_ms=1.0).model_dump(),
                    }
                return {
                    "tool": tool, "skill": "document", "query": query, "evidence": [],
                    "sources": [], "decision_memories": [],
                    "retrieval_diagnostics": {"effective_mode": "bm25"},
                }

            agent.executor.execute = execute
            replies = iter([
                json.dumps({"action": "tool_call", "tool": "search_docs",
                            "query": "retry path"}),
                json.dumps({"action": "finish", "claims": [],
                            "gaps": ["scripted retry run"]}),
            ])
            agent._call_llm = lambda messages: next(replies)
            return agent

        service = RunService(self.db, agent_factory=factory,
                             config=RuntimeConfig(db_path=self.db))
        self.addCleanup(service.close)
        session = self.session(service)
        run, _ = service.submit(session["session_id"], "retry path")
        done = wait_status(service, run["run_id"], {"failed"}, timeout=5)
        self.assertEqual(done["status"], "failed", done)
        self.assertEqual(done["termination_reason"], "tool_failure")
        types = event_types(service, run["run_id"])
        self.assertIn("tool_failed", types)
        self.assertIn("retry_scheduled", types)
        self.assertEqual(types.count("tool_started"), 2)
        calls = service.tool_calls(run["run_id"], project_id=PROJECT)
        self.assertEqual([call["status"] for call in calls], ["failed", "completed"])
        self.assertEqual(calls[0]["error_code"], "tool_timeout")
        self.assertEqual(calls[0]["retryable"], 1)
        self.assertEqual(calls[1]["attempt"], 1)
        self.assertLess(calls[0]["trace_step_index"], calls[1]["trace_step_index"])

    def test_real_react_agent_lifecycle_and_tool_trace_association(self):
        from src.agents.react_agent import ReActAgent

        def factory(*, scope, observer, cancel_check, budget):
            with patch("src.agents.react_agent._resolve_api_key", return_value="test-key"), \
                    patch("src.agents.answer_agent.resolve_api_key", return_value=""):
                agent = ReActAgent(scope=scope, db_path=self.db, budget=budget,
                                   observer=observer, cancel_check=cancel_check)
            agent.api_key = "test-key"
            agent.executor.execute = lambda tool, query, filters=None, timeout=None: {
                "tool": tool, "skill": "document", "query": query, "evidence": [],
                "sources": [], "decision_memories": [],
                "retrieval_diagnostics": {"effective_mode": "bm25",
                                          "index_version": None,
                                          "source_revision": None},
            }
            replies = iter([
                json.dumps({"action": "tool_call", "tool": "search_docs",
                            "query": "runtime lifecycle"}),
                json.dumps({"action": "finish", "claims": [],
                            "gaps": ["no evidence in scripted run"]}),
            ])
            agent._call_llm = lambda messages: next(replies)
            return agent

        service = RunService(self.db, agent_factory=factory,
                             config=RuntimeConfig(db_path=self.db))
        self.addCleanup(service.close)
        session = self.session(service)
        run, _ = service.submit(session["session_id"], "runtime lifecycle")
        done = wait_status(service, run["run_id"], {"insufficient"}, timeout=5)
        self.assertEqual(done["status"], "insufficient")
        types = event_types(service, run["run_id"])
        for expected in ("model_requested", "model_completed", "tool_started",
                         "tool_completed", "claim_submitted", "claim_verified"):
            self.assertIn(expected, types)
        calls = service.tool_calls(run["run_id"], project_id=PROJECT)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "completed")
        self.assertEqual(calls[0]["tool_name"], "search_docs")
        stored = service.result(run["run_id"], project_id=PROJECT)[1]["result"]
        tool_steps = [index for index, step in enumerate(stored["trace"], start=1)
                      if step["action"] == "tool_attempt"]
        self.assertIn(calls[0]["trace_step_index"], tool_steps)


if __name__ == "__main__":
    unittest.main()
