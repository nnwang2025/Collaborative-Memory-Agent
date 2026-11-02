"""Session persistence, bounded follow-up context and evidence revalidation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.evidence import EvidenceScope
from src.runtime.config import RuntimeConfig
from src.runtime.conversation import ConversationContextBuilder
from src.runtime.errors import SessionClosed
from src.runtime.service import RunService
from src.runtime.store import RuntimeStore

from tests.runtime_helpers import (
    agent_factory, make_evidence, make_result, simple_behaviour, wait_status,
)

PROJECT = "session-project"


class SessionContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="cma-runtime-session-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        self.scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")

    def service(self, behaviour, *, workers=2, runs=3, bytes_=6000):
        service = RunService(
            self.db,
            agent_factory=agent_factory(behaviour),
            config=RuntimeConfig(db_path=self.db, max_workers=workers,
                                 session_history_runs=runs,
                                 session_history_bytes=bytes_),
        )
        self.addCleanup(service.close)
        return service

    def test_follow_up_receives_bounded_prior_context_and_current_run_is_excluded(self):
        evidence = make_evidence(self.db, self.scope)
        service = self.service(
            simple_behaviour(refs_factory=lambda: [evidence]), runs=1)
        session = service.sessions.create(PROJECT, "live")
        first, _ = service.submit(session["session_id"], "first question")
        wait_status(service, first["run_id"], {"succeeded"})

        contexts = []

        def capture(agent, question, context):
            contexts.append(json.loads(context) if context else None)
            return make_result(agent.scope, answer="follow-up answer")

        service.agent_factory = agent_factory(capture)
        second, _ = service.submit(session["session_id"], "follow-up question")
        wait_status(service, second["run_id"], {"succeeded"})
        context = contexts[0]
        self.assertIsNotNone(context)
        self.assertIn("不是事实来源", context["note"])
        self.assertEqual(len(context["previous_investigations"]), 1)
        previous = context["previous_investigations"][0]
        self.assertEqual(previous["question"], "first question")
        self.assertEqual(previous["answer_hint"], "runtime answer")
        self.assertEqual(previous["verified_evidence_refs"][0]["evidence_id"],
                         evidence.evidence_id)

        # With history_runs=1 the third run sees only the second, never the first.
        third, _ = service.submit(session["session_id"], "third question")
        wait_status(service, third["run_id"], {"succeeded"})
        latest = contexts[1]["previous_investigations"]
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["question"], "follow-up question")
        self.assertNotIn("first question", json.dumps(contexts[1], ensure_ascii=False))

    def test_context_builder_drops_stale_refs_and_respects_byte_budget(self):
        evidence = make_evidence(self.db, self.scope)
        store = RuntimeStore(self.db)
        session = store.create_session(PROJECT, "live")
        run_ids = []
        for index in range(3):
            run, _ = store.create_run(
                session_id=session["session_id"], project_id=PROJECT,
                dataset_kind="live", question=f"question {index}",
                request_hash=f"hash-{index}")
            store.start_run(run["run_id"])
            store.record_evidence_ref(
                run["run_id"], evidence.evidence_id, evidence.content_hash,
                tool_name="search_docs")
            store.record_evidence_ref(
                run["run_id"], "ev_" + "c" * 64, "d" * 64,
                tool_name="search_docs")
            store.complete_run(
                run["run_id"], status="succeeded", termination_reason="sufficient",
                termination_detail=None,
                result={"answer": "answer " + "x" * 500, "gaps": []},
                answer="answer " + "x" * 500)
            run_ids.append(run["run_id"])

        builder = ConversationContextBuilder(
            store, max_runs=2, max_bytes=1400, evidence_store=None)
        rendered = builder.build(session["session_id"])
        self.assertIsNotNone(rendered)
        self.assertLessEqual(len(rendered.encode("utf-8")), 1400)
        context = json.loads(rendered)
        self.assertLessEqual(len(context["previous_investigations"]), 2)
        text = json.dumps(context)
        self.assertIn(evidence.evidence_id, text)
        self.assertNotIn("ev_" + "c" * 64, text)

    def test_history_answer_context_is_not_trusted_as_evidence_by_react_agent(self):
        from src.agents.react_agent import ReActAgent

        evidence = make_evidence(self.db, self.scope)
        context = json.dumps({
            "note": "历史回答不是事实来源；仅可用来理解追问。",
            "previous_investigations": [{
                "question": "old question", "answer_hint": "old answer",
                "verified_evidence_refs": [{
                    "evidence_id": evidence.evidence_id,
                    "content_hash": evidence.content_hash}]}],
        })
        seen = {}
        with patch("src.agents.react_agent._resolve_api_key", return_value="test-key"), \
                patch("src.agents.answer_agent.resolve_api_key", return_value=""):
            agent = ReActAgent(scope=self.scope, db_path=self.db)
        agent.api_key = "test-key"
        reply = json.dumps({
            "action": "finish",
            "claims": [{
                "text": "claim copied from history", "kind": "historical",
                "refs": [{"evidence_id": evidence.evidence_id,
                          "content_hash": evidence.content_hash}],
            }],
            "gaps": [],
        })

        def scripted(messages):
            seen["messages"] = messages
            return reply

        agent._call_llm = scripted
        result = agent.run_result("follow up", conversation_context=context)
        user_turn = seen["messages"][1]["content"]
        self.assertIn("[会话上下文", user_turn)
        self.assertIn("不是事实来源", user_turn)
        self.assertIn(evidence.evidence_id, user_turn)
        self.assertEqual(result.claims, [])
        self.assertTrue(any("引用校验失败" in gap for gap in result.gaps))
        self.assertEqual(result.termination_reason, "insufficient")

    def test_closed_session_rejects_new_runs_but_keeps_history(self):
        service = self.service(simple_behaviour())
        session = service.sessions.create(PROJECT, "live")
        run, _ = service.submit(session["session_id"], "before close",
                                idempotency_key="closed-key")
        wait_status(service, run["run_id"], {"succeeded"})
        closed = service.sessions.close(session["session_id"])
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(SessionClosed):
            service.submit(session["session_id"], "after close")
        # An idempotent retry of an already accepted request still resolves to
        # the original run after the session is closed.
        replay, created = service.submit(
            session["session_id"], "before close", idempotency_key="closed-key")
        self.assertFalse(created)
        self.assertEqual(replay["run_id"], run["run_id"])
        self.assertEqual([item["role"] for item in
                          service.sessions.messages(session["session_id"])],
                         ["user", "assistant"])
        self.assertEqual(len(service.sessions.runs(session["session_id"])), 1)


if __name__ == "__main__":
    unittest.main()
