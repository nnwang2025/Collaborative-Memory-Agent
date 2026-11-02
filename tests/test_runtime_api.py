"""FastAPI Session/Run endpoints, scope isolation, SSE replay and compatibility."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from src.api.runtime_routes import iter_run_stream
from src.evidence import EvidenceScope
from src.runtime.config import RuntimeConfig
from src.runtime.service import RunService

from tests.runtime_helpers import (
    agent_factory, event_types, make_evidence, make_result, simple_behaviour,
    wait_status,
)

PROJECT = "api-project"
OTHER = "other-project"


def parse_sse(body: str):
    frames = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        frame = {"id": None, "event": None, "data": None, "comment": False}
        for line in block.splitlines():
            if line.startswith(":"):
                frame["comment"] = True
            elif line.startswith("id: "):
                frame["id"] = int(line[4:])
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
        if frame["event"] or frame["comment"]:
            frames.append(frame)
    return frames


class RuntimeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="cma-runtime-api-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        self.scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")

    def start(self, behaviour, *, workers=2, app=None):
        service = RunService(
            self.db, agent_factory=agent_factory(behaviour),
            config=RuntimeConfig(db_path=self.db, max_workers=workers))
        self.addCleanup(service.close)
        app = app or api.create_app(runtime_service=service)
        client = TestClient(app)
        self.addCleanup(client.close)
        return service, client

    def poll(self, client, run_id, statuses, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = client.get(f"/runs/{run_id}", params={"project_id": PROJECT})
            self.assertEqual(response.status_code, 200)
            row = response.json()["run"]
            if row["status"] in statuses:
                return row
            time.sleep(0.01)
        return client.get(f"/runs/{run_id}",
                          params={"project_id": PROJECT}).json()["run"]

    def create_session(self, client, project=PROJECT, dataset="live"):
        response = client.post("/sessions",
                               json={"project_id": project, "dataset_kind": dataset})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["session"]

    def test_run_lifecycle_result_events_and_session_queries(self):
        evidence = make_evidence(self.db, self.scope)
        _, client = self.start(simple_behaviour(refs_factory=lambda: [evidence]))
        session = self.create_session(client)
        response = client.post("/runs", json={
            "session_id": session["session_id"], "question": "lifecycle"})
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run"]["run_id"]
        done = self.poll(client, run_id, {"succeeded"})
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["termination_reason"], "sufficient")
        self.assertTrue(done["result_available"])

        events = client.get(f"/runs/{run_id}/events",
                            params={"project_id": PROJECT}).json()
        types = [item["event_type"] for item in events["events"]]
        self.assertEqual(types[0], "run_created")
        self.assertEqual(types[-1], "run_finished")
        self.assertEqual(events["next_after_seq"],
                         events["events"][-1]["event_seq"])

        result = client.get(f"/runs/{run_id}/result",
                            params={"project_id": PROJECT}).json()
        self.assertEqual(result["answer"], "runtime answer")
        self.assertTrue(result["result_available"])
        self.assertEqual(result["status_at_result"], "succeeded")

        refs = client.get(f"/runs/{run_id}/evidence-refs",
                          params={"project_id": PROJECT}).json()["evidence_refs"]
        self.assertEqual(refs[0]["evidence_id"], evidence.evidence_id)
        self.assertEqual(refs[0]["content_hash"], evidence.content_hash)

        session_body = client.get(f"/sessions/{session['session_id']}").json()
        self.assertEqual([item["role"] for item in session_body["messages"]],
                         ["user", "assistant"])
        self.assertEqual(len(session_body["runs"]), 1)
        listed = client.get(f"/sessions/{session['session_id']}/runs").json()
        self.assertEqual(listed["runs"][0]["run_id"], run_id)

    def test_idempotency_header_and_conflict(self):
        _, client = self.start(simple_behaviour())
        session = self.create_session(client)
        body = {"session_id": session["session_id"], "question": "same"}
        first = client.post("/runs", json=body, headers={"Idempotency-Key": "api-key-1"})
        self.assertEqual(first.status_code, 202)
        run_id = first.json()["run"]["run_id"]
        self.poll(client, run_id, {"succeeded"})
        replay = client.post("/runs", json=body, headers={"Idempotency-Key": "api-key-1"})
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["run"]["run_id"], run_id)
        self.assertFalse(replay.json()["created"])
        self.assertTrue(replay.json()["idempotent_replay"])
        conflict = client.post("/runs", json={
            "session_id": session["session_id"], "question": "different"},
            headers={"Idempotency-Key": "api-key-1"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"]["code"],
                         "idempotency_key_reused_with_different_request")

    def test_validation_limits_and_cross_project_isolation(self):
        _, client = self.start(simple_behaviour())
        self.assertEqual(client.post("/sessions", json={}).status_code, 422)
        self.assertEqual(client.post("/sessions", json={
            "project_id": PROJECT, "dataset_kind": "staging"}).status_code, 422)
        session = self.create_session(client)
        self.assertEqual(client.post("/runs", json={
            "session_id": "missing", "question": "q"}).status_code, 404)
        self.assertEqual(client.post("/runs", json={
            "session_id": session["session_id"], "question": "q",
            "unexpected": True}).status_code, 422)
        self.assertEqual(client.post("/runs", json={
            "session_id": session["session_id"], "question": "   "}).status_code, 422)
        self.assertEqual(client.post("/runs", json={
            "session_id": session["session_id"], "question": "q",
            "timeout_seconds": 0}).status_code, 422)

        run_id = client.post("/runs", json={
            "session_id": session["session_id"], "question": "scoped"}).json()["run"]["run_id"]
        self.poll(client, run_id, {"succeeded"})
        self.assertEqual(client.get(f"/runs/{run_id}").status_code, 422)
        self.assertEqual(client.get(
            f"/runs/{run_id}", params={"project_id": OTHER}).status_code, 404)
        self.assertEqual(client.get(
            f"/runs/{run_id}/events", params={"project_id": OTHER}).status_code, 404)
        self.assertEqual(client.get(
            f"/runs/{run_id}/result", params={"project_id": OTHER}).status_code, 404)
        self.assertEqual(client.post(
            f"/runs/{run_id}/cancel", params={"project_id": OTHER}).status_code, 404)

        other_session = self.create_session(client, project=OTHER)
        other_run = client.post("/runs", json={
            "session_id": other_session["session_id"],
            "question": "other"}).json()["run"]["run_id"]
        self.poll_for_project(client, other_run, OTHER, {"succeeded"})
        self.assertEqual(client.get(f"/runs/{other_run}",
                                    params={"project_id": PROJECT}).status_code, 404)
        self.assertEqual([row["run_id"] for row in client.get(
            f"/sessions/{session['session_id']}/runs").json()["runs"]], [run_id])

    @staticmethod
    def poll_for_project(client, run_id, project, statuses, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = client.get(f"/runs/{run_id}",
                             params={"project_id": project}).json()["run"]
            if row["status"] in statuses:
                return row
            time.sleep(0.01)
        return None

    def test_cancel_running_timeout_and_result_before_terminal(self):
        started = threading.Event()

        def cancellable(agent, question, context):
            started.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if agent.cancel_check():
                    from src.runtime.errors import RunCancelled
                    raise RunCancelled()
                time.sleep(0.01)
            return make_result(agent.scope)

        def slow(agent, question, context):
            time.sleep(0.5)
            return make_result(agent.scope, answer="late")

        service, client = self.start(cancellable)
        session = self.create_session(client)
        run_id = client.post("/runs", json={
            "session_id": session["session_id"], "question": "cancel"}).json()["run"]["run_id"]
        self.assertTrue(started.wait(2))
        early = client.get(f"/runs/{run_id}/result", params={"project_id": PROJECT})
        self.assertEqual(early.status_code, 409)
        self.assertEqual(early.json()["detail"]["code"], "run_not_terminal")
        cancelled = client.post(f"/runs/{run_id}/cancel", params={"project_id": PROJECT})
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(cancelled.json()["cancel_requested"])
        done = self.poll(client, run_id, {"cancelled"})
        self.assertEqual(done["status"], "cancelled")
        self.assertIn("run_cancelled", event_types(service, run_id, PROJECT))
        self.assertFalse(client.get(
            f"/runs/{run_id}/result", params={"project_id": PROJECT}).json()[
                "result_available"])

        service.agent_factory = agent_factory(slow)
        timed = client.post("/runs", json={
            "session_id": session["session_id"], "question": "timeout",
            "timeout_seconds": 0.1}).json()["run"]["run_id"]
        done = self.poll(client, timed, {"timed_out"}, timeout=2)
        self.assertEqual(done["status"], "timed_out")
        self.assertEqual(done["termination_reason"], "timeout")
        time.sleep(0.6)
        self.assertEqual(client.get(f"/runs/{timed}",
                                    params={"project_id": PROJECT}).json()["run"]["status"],
                         "timed_out")

        service.agent_factory = agent_factory(simple_behaviour())
        completed = client.post("/runs", json={
            "session_id": session["session_id"], "question": "done"}).json()["run"]["run_id"]
        self.poll(client, completed, {"succeeded"})
        late_cancel = client.post(f"/runs/{completed}/cancel",
                                  params={"project_id": PROJECT})
        self.assertEqual(late_cancel.status_code, 200)
        self.assertFalse(late_cancel.json()["cancel_requested"])
        self.assertTrue(late_cancel.json()["terminal"])

    def test_sse_replay_reconnect_and_terminal_end(self):
        _, client = self.start(simple_behaviour())
        session = self.create_session(client)
        run_id = client.post("/runs", json={
            "session_id": session["session_id"], "question": "sse"}).json()["run"]["run_id"]
        self.poll(client, run_id, {"succeeded"})

        first = client.get(f"/runs/{run_id}/stream",
                           params={"project_id": PROJECT, "after_seq": 0})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.headers["content-type"].startswith("text/event-stream"))
        frames = parse_sse(first.text)
        self.assertGreater(len(frames), 2)
        ids = [frame["id"] for frame in frames if frame["id"] is not None]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(frames[-1]["event"], "stream_end")
        self.assertEqual(frames[-1]["data"]["status"], "succeeded")
        last_id = ids[-1]

        replay = client.get(
            f"/runs/{run_id}/stream", params={"project_id": PROJECT},
            headers={"Last-Event-ID": str(last_id)})
        replay_frames = parse_sse(replay.text)
        self.assertEqual([frame for frame in replay_frames if frame["id"] is not None], [])
        self.assertEqual(replay_frames[-1]["event"], "stream_end")
        self.assertEqual(replay_frames[-1]["data"]["last_event_seq"], last_id)

        resumed = client.get(f"/runs/{run_id}/stream",
                             params={"project_id": PROJECT, "after_seq": 2})
        resumed_frames = parse_sse(resumed.text)
        self.assertTrue(all(frame["id"] > 2 for frame in resumed_frames
                            if frame["id"] is not None))

    def test_sse_disconnect_does_not_cancel_background_run(self):
        started = threading.Event()
        release = threading.Event()

        def blocking(agent, question, context):
            started.set()
            release.wait(5)
            return make_result(agent.scope)

        service, _ = self.start(blocking)
        session = service.sessions.create(PROJECT, "live")
        run, _ = service.submit(session["session_id"], "disconnect")
        self.assertTrue(started.wait(2))

        checks = {"n": 0}

        async def is_disconnected():
            checks["n"] += 1
            return checks["n"] > 1

        async def collect():
            return [frame async for frame in iter_run_stream(
                service, run["run_id"], PROJECT, 0, is_disconnected,
                heartbeat_seconds=0.05, poll_seconds=0.02)]

        frames = asyncio.run(collect())
        self.assertTrue(frames)
        self.assertNotEqual(service.store.get_run(run["run_id"])["status"],
                            "cancelled")
        release.set()
        self.assertEqual(wait_status(service, run["run_id"], {"succeeded"})["status"],
                         "succeeded")

    def test_sse_heartbeat_while_run_is_active(self):
        started = threading.Event()
        release = threading.Event()

        def blocking(agent, question, context):
            started.set()
            release.wait(5)
            return make_result(agent.scope)

        service, _ = self.start(blocking)
        session = service.sessions.create(PROJECT, "live")
        run, _ = service.submit(session["session_id"], "heartbeat")
        self.assertTrue(started.wait(2))
        checks = {"n": 0}

        async def is_disconnected():
            checks["n"] += 1
            return checks["n"] > 6

        async def collect():
            return [frame async for frame in iter_run_stream(
                service, run["run_id"], PROJECT, 0, is_disconnected,
                heartbeat_seconds=0.005, poll_seconds=0.005)]

        frames = [frame for chunk in asyncio.run(collect())
                  for frame in parse_sse(chunk)]
        self.assertTrue(any(frame["comment"] for frame in frames))
        self.assertEqual(service.store.get_run(run["run_id"])["status"], "running")
        release.set()
        wait_status(service, run["run_id"], {"succeeded"})

    def test_chat_and_health_compatibility(self):
        fake = type("FakeAgent", (), {})()
        fake.run_result = lambda question: make_result(self.scope, answer="compat")
        with patch("api._get_agent", return_value=fake):
            with TestClient(api.app) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                response = client.post("/chat", json={
                    "question": "hello", "backend": "react"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["answer"], "compat")
                self.assertEqual(response.json()["backend"], "react")


if __name__ == "__main__":
    unittest.main()
