"""Phase 3 context engineering: budgets, structured state, selection, trace.

These tests use scripted models and real local storage only.  They assert the
*messages actually handed to the model*, not just helper return values.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import api
from src.agents.react_agent import ReActAgent, ToolExecutor
from src.context_budget import (
    BUDGET_POLICY_VERSION, CONTEXT_SCHEMA_VERSION, ContextBudget,
    MODEL_CONTEXT_WINDOWS, TokenCounter, build_policy,
    estimate_text_tokens, resolve_model_context_window,
)
from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore
from src.ingest import normalize_record
from src.investigation import Budget, EvidenceLedger
from src.investigation_context import (
    ContextLimitError, InvestigationContext, select_excerpt,
)
from src.runtime.config import RuntimeConfig
from src.runtime.service import RunService

from offline_env import OFFLINE_PROVIDER_ENV
from tests.runtime_helpers import agent_factory, make_result, wait_status


def item(record, query, score=0.0):
    parent = Evidence.model_validate(record)
    excerpt = select_excerpt(parent, query)
    return {**parent.model_dump(mode="json"), "score": score,
            "score_kind": "retrieval_relevance",
            "excerpt": excerpt.model_dump(mode="json")}


def request_messages(context, views, question, *, observation=None,
                     system="System rules: never invent evidence."):
    payload = {"evidence": views}
    payload.update(observation or {})
    return [{"role": "system", "content": system},
            {"role": "user", "content": question},
            {"role": "assistant", "content": '{"action":"tool_call"}'},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def test_budget(*, model="deepseek-chat", remaining=10**9, window=None,
                reserved=2048, safety=0, max_excerpts=8, max_per=1,
                max_bytes=48000):
    policy = build_policy(model=model, max_tokens=64000,
                          max_output_tokens=reserved,
                          context_window_tokens=window,
                          safety_margin_tokens=safety,
                          max_context_bytes=max_bytes,
                          max_excerpts=max_excerpts,
                          max_excerpts_per_evidence=max_per)
    return ContextBudget(policy, TokenCounter(model, "estimate"),
                         remaining_total_tokens=remaining)


class TokenBudgetTests(unittest.TestCase):
    def test_estimate_is_deterministic_and_labelled_not_billed(self):
        self.assertEqual(estimate_text_tokens("abcd" * 4), 5)
        self.assertGreaterEqual(estimate_text_tokens("中文内容"), 4)
        counter = TokenCounter("deepseek-chat", "estimate")
        trace = counter.trace()
        self.assertEqual(trace["tokenizer_mode"], "estimated")
        self.assertEqual(trace["tokenizer_name"], "heuristic_v1")
        self.assertEqual(trace["estimate_kind"], "estimated_not_billed_tokens")
        self.assertEqual(trace["tokenizer_requested_mode"], "estimate")

    def test_requested_real_tokenizer_falls_back_auditably(self):
        counter = TokenCounter("model-without-encoding", "tiktoken")
        trace = counter.trace()
        self.assertIn(trace["tokenizer_mode"], {"exact", "estimated"})
        if not counter.exact:
            self.assertIn(trace["tokenizer_fallback_reason"],
                          {"tiktoken_unavailable", "encoding_unavailable",
                           "estimate_requested"})
            self.assertEqual(trace["estimate_kind"],
                             "estimated_not_billed_tokens")
        else:
            self.assertEqual(trace["estimate_kind"], "provider_tokenizer")

    def test_model_window_resolution_and_policy_caps(self):
        self.assertEqual(resolve_model_context_window("deepseek-chat"), 64000)
        self.assertEqual(resolve_model_context_window("gpt-4o"), 128000)
        self.assertEqual(resolve_model_context_window("unknown-model"), 64000)
        self.assertEqual(resolve_model_context_window("deepseek-chat", 9000), 9000)
        policy = build_policy(model="deepseek-chat", max_tokens=64000,
                              max_output_tokens=10**9,
                              safety_margin_tokens=10**9)
        self.assertLessEqual(policy.reserved_output_tokens,
                             policy.model_context_window // 2)
        self.assertLessEqual(policy.safety_margin_tokens,
                             policy.model_context_window // 4)

    def test_context_budget_is_min_of_window_and_remaining_total(self):
        policy = build_policy(model="deepseek-chat", max_output_tokens=2048,
                              safety_margin_tokens=512)
        counter = TokenCounter("deepseek-chat")
        roomy = ContextBudget(policy, counter, remaining_total_tokens=10**9)
        self.assertEqual(roomy.input_ceiling_tokens,
                         MODEL_CONTEXT_WINDOWS["deepseek-chat"] - 2048 - 512)
        tight = ContextBudget(policy, counter, remaining_total_tokens=100)
        self.assertEqual(tight.input_ceiling_tokens, 100)
        exhausted = ContextBudget(policy, counter, remaining_total_tokens=-5)
        self.assertEqual(exhausted.input_ceiling_tokens, 0)


class StructuredContextTests(unittest.TestCase):
    def setUp(self):
        self.scope = EvidenceScope(project_id="phase3-context",
                                   dataset_kind="live")
        self.ledger = EvidenceLedger(self.scope)

    def record(self, content, record_id="r1", source="benchmark://phase3/docs",
               category="docs", source_type="document", **kwargs):
        return normalize_record(content, source, category, record_id,
                                project_id=self.scope.project_id,
                                source_type=source_type, **kwargs)

    def test_conflict_body_survives_low_retrieval_score(self):
        original = self.record("The approved timeout is thirty seconds.",
                              "conflict-1", locator={"path": "a.md"})
        revised = self.record("The approved timeout is now sixty seconds.",
                             "conflict-1", locator={"path": "a.md"})
        self.assertEqual(original["evidence_id"], revised["evidence_id"])
        self.assertNotEqual(original["content_hash"], revised["content_hash"])
        ledger = EvidenceLedger(self.scope)
        ledger.register([original, revised], "search_docs")
        self.assertEqual(len(ledger.conflicts), 1)
        context = InvestigationContext()
        views = context.observe([item(original, "unrelated", score=0.01),
                                 item(revised, "unrelated", score=0.01)])
        request, usage = context.request(
            request_messages(context, views, "What is the timeout?"),
            ledger, [], {}, {}, 48000,
            question="What is the timeout?",
            context_budget=test_budget(max_excerpts=4))
        state = json.loads(request[2]["content"])
        identities = {(view["evidence_id"], view["content_hash"])
                      for view in state["retained_excerpts"]}
        self.assertEqual(identities, {(original["evidence_id"],
                                       original["content_hash"]),
                                      (revised["evidence_id"],
                                       revised["content_hash"])})
        bodies = {view["content"] for view in state["retained_excerpts"]
                  if view.get("content")}
        for message in request[3:]:
            payload = json.loads(message["content"])
            bodies.update(view.get("content") for view in payload.get("evidence", [])
                          if view.get("content"))
        self.assertEqual(bodies, {original["content"], revised["content"]})
        self.assertEqual(usage["conflict_count"], 2)
        self.assertEqual(usage["conflict_bodies_retained"], 2)
        self.assertTrue(usage["important_conflicts_retained"])
        self.assertTrue(state["conflicts"])
        reasons = {view["selection_reason"] for view in
                   state["retained_excerpts"]}
        self.assertEqual(reasons, {"critical_conflict_evidence"})

    def test_verified_claim_support_is_retained_at_zero_similarity(self):
        record = self.record("Bounded latency was the recorded reason.", "claim-1")
        ledger = EvidenceLedger(self.scope)
        ledger.register([item(record, "irrelevant", score=0.0)], "search_docs")
        context = InvestigationContext()
        views = context.observe([item(record, "irrelevant", score=0.0)])
        claims = [{"text": "Bounded latency was the reason.",
                   "refs": [{"evidence_id": record["evidence_id"],
                             "content_hash": record["content_hash"]}]}]
        _, usage = context.request(
            request_messages(context, views, "Why?"), ledger, [], {}, {},
            48000, claims=claims, question="Why?",
            context_budget=test_budget())
        retained = [decision for decision in usage["evidence_decisions"]
                    if decision["decision"] == "retained"]
        self.assertEqual([decision["reason"] for decision in retained],
                         ["verified_claim_support"])
        self.assertEqual(usage["selected_evidence_count"], 1)

    def test_duplicate_chunks_are_deduped_and_ledger_is_untouched(self):
        content = ("HEAD-FACT " + "filler text " * 120 + "\n"
                   + "TAIL-FACT " + "more filler " * 120)
        record = self.record(content, "long-1")
        first = item(record, "HEAD-FACT", score=0.9)
        second = item(record, "TAIL-FACT", score=0.8)
        self.assertNotEqual(first["excerpt"]["char_start"],
                            second["excerpt"]["char_start"])
        ledger = EvidenceLedger(self.scope)
        ledger.register([first, second], "search_docs")
        context = InvestigationContext()
        views = context.observe([first, second])
        before = [entry.model_dump() for entry in ledger.entries]
        request, usage = context.request(
            request_messages(context, views, "facts"),
            ledger, [], {}, {}, 48000, question="facts",
            context_budget=test_budget(max_per=1))
        after = [entry.model_dump() for entry in ledger.entries]
        self.assertEqual(before, after)
        self.assertEqual(usage["candidate_evidence_count"], 2)
        self.assertEqual(usage["unique_evidence_count"], 1)
        self.assertGreaterEqual(usage["deduped_excerpt_count"], 1)
        self.assertEqual(usage["selected_evidence_count"], 1)
        state = json.loads(request[2]["content"])
        self.assertEqual(len(state["evidence_index"]), 1)
        self.assertEqual(len(state["retained_excerpts"]), 1)
        self.assertIn("duplicate_chunk_same_evidence",
                      usage["pruning_reasons"])
        bodies = {view["content"] for view in state["retained_excerpts"]
                  if view.get("content")}
        for message in request[3:]:
            payload = json.loads(message["content"])
            bodies.update(view.get("content") for view in payload.get("evidence", [])
                          if view.get("content"))
        self.assertEqual(len(bodies), 1)
        self.assertEqual(json.dumps(request, ensure_ascii=False).count(
            bodies.pop()), 1)

    def test_reproducible_selection_for_fixed_inputs(self):
        record = self.record("A deterministic decision record.", "repro-1")
        ledger = EvidenceLedger(self.scope)
        ledger.register([item(record, "decision", score=0.4)], "search_docs")

        def build():
            context = InvestigationContext()
            views = context.observe([item(record, "decision", score=0.4)])
            return context.request(
                request_messages(context, views, "decision"), ledger, [], {},
                {}, 48000, question="decision",
                context_budget=test_budget())

        first_request, first_usage = build()
        second_request, second_usage = build()
        self.assertEqual(json.dumps(first_request, sort_keys=True),
                         json.dumps(second_request, sort_keys=True))
        self.assertEqual(first_usage["evidence_decisions"],
                         second_usage["evidence_decisions"])
        self.assertEqual(first_usage["estimated_input_tokens"],
                         second_usage["estimated_input_tokens"])

    def test_mandatory_overflow_is_structured_and_never_drops_rules(self):
        record = self.record("Evidence body.", "overflow-1")
        ledger = EvidenceLedger(self.scope)
        ledger.register([item(record, "Evidence")], "search_docs")
        context = InvestigationContext()
        views = context.observe([item(record, "Evidence")])
        messages = request_messages(context, views, "Keep this exact question")
        with self.assertRaises(ContextLimitError) as raised:
            context.request(messages, ledger, [], {}, {}, 48000,
                            question="Keep this exact question",
                            context_budget=test_budget(remaining=20))
        detail = raised.exception.detail
        self.assertEqual(detail["context_limit"]["code"], "context_limit")
        self.assertEqual(detail["context_limit"]["reason"],
                         "mandatory_state_exceeds_budget")
        self.assertLessEqual(detail["input_token_ceiling"], 20)
        self.assertGreater(detail["estimated_input_tokens"],
                           detail["input_token_ceiling"])
        self.assertEqual(detail["tokenizer_mode"], "estimated")
        self.assertEqual(detail["context_schema_version"],
                         CONTEXT_SCHEMA_VERSION)

    def test_session_history_is_bounded_non_factual_and_cannot_crowd_evidence(self):
        record = self.record("Current-run verified decision evidence.", "session-1")
        ledger = EvidenceLedger(self.scope)
        ledger.register([item(record, "verified decision", score=0.5)],
                        "search_docs")
        context = InvestigationContext()
        views = context.observe([item(record, "verified decision", score=0.5)])
        history = {
            "previous_investigations": [
                {"run_id": f"run-{index}", "question": "old question " * 30,
                 "status": "succeeded", "answer": "OLD-ANSWER " * 200,
                 "verified_evidence_refs": [{"evidence_id": "ev_old",
                                             "content_hash": "0" * 64}],
                 "unresolved_gaps": [], "conflicts": []}
                for index in range(12)
            ]
        }
        request, usage = context.request(
            request_messages(context, views, "那后来呢"),
            ledger, [], {}, {}, 48000, question="那后来呢",
            session_context=json.dumps(history, ensure_ascii=False),
            context_budget=test_budget(remaining=5000))
        state = json.loads(request[2]["content"])
        meta = state["session_context_meta"]
        self.assertFalse(meta["fact_source"])
        self.assertLess(meta["previous_investigations"], 12)
        self.assertIn("[会话上下文", request[1]["content"])
        self.assertIn("那后来呢", request[1]["content"])
        self.assertNotIn("OLD-ANSWER OLD-ANSWER OLD-ANSWER", request[1]["content"])
        self.assertLessEqual(usage["category_tokens"]["session_history"], 600)
        self.assertGreaterEqual(usage["selected_evidence_count"], 1)
        self.assertEqual(state["evidence_index"][0]["evidence_id"],
                         record["evidence_id"])
        self.assertEqual(state["gap_status"], "unresolved")


class AgentContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cma-phase3-agent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scope = EvidenceScope(project_id="phase3-agent", dataset_kind="live")
        self.db = self.root / "evidence.sqlite3"
        self.record = normalize_record(
            "Review ALPHA: the decision was bounded latency.",
            "benchmark://phase3/docs", "docs", "review.md",
            project_id=self.scope.project_id, locator={"path": "review.md"})
        self.other = normalize_record(
            "Review BETA: a later proposal changed the timeout.",
            "benchmark://phase3/docs", "docs", "beta.md",
            project_id=self.scope.project_id, locator={"path": "beta.md"})
        EvidenceStore(self.db).append([self.record, self.other])
        env = patch.dict(os.environ, {
            **OFFLINE_PROVIDER_ENV,
            "DECISION_DB_PATH": str(self.root / "absent.sqlite3")})
        env.start()
        self.addCleanup(env.stop)

    def agent(self, actions, budget=None):
        agent = ReActAgent(scope=self.scope, db_path=self.db, budget=budget)
        agent.api_key = "offline-script-only"
        agent._call_llm = Mock(side_effect=[json.dumps(action)
                                           for action in actions])
        return agent

    @staticmethod
    def claim(record):
        return {"text": record["content"],
                "refs": [{"evidence_id": record["evidence_id"],
                          "content_hash": record["content_hash"]}]}

    def test_react_request_contains_versioned_state_failures_and_empty_searches(self):
        original = ToolExecutor.execute

        def flaky(executor, tool, query, **kwargs):
            if query == "FAIL_QUERY":
                raise RuntimeError("provider detail must never be echoed")
            return original(executor, tool, query, **kwargs)

        actions = [
            {"action": "tool_call", "tool": "search_docs",
             "query": "Review ALPHA", "filters": {}},
            {"action": "tool_call", "tool": "search_docs",
             "query": "FAIL_QUERY", "filters": {}},
            {"action": "tool_call", "tool": "search_docs",
             "query": "no such record anywhere", "filters": {}},
            {"action": "finish", "claims": [self.claim(self.record)]},
        ]
        agent = self.agent(actions, Budget(max_model_rounds=6))
        with patch.object(ToolExecutor, "execute", flaky):
            result = agent.run_result("Why was the timeout chosen?")
        request = agent._call_llm.call_args_list[-1].args[0]
        state = json.loads(request[2]["content"])
        self.assertEqual(state["context_schema_version"],
                         CONTEXT_SCHEMA_VERSION)
        self.assertEqual(state["budget_policy_version"], BUDGET_POLICY_VERSION)
        self.assertEqual(state["task"]["goal"], "Why was the timeout chosen?")
        self.assertEqual(state["task"]["scope"], {
            "project_id": self.scope.project_id,
            "dataset_kind": self.scope.dataset_kind})
        self.assertTrue(state["evidence_index"])
        self.assertTrue(any(entry.get("evidence_id") == self.record["evidence_id"]
                            for entry in state["evidence_index"]))
        self.assertTrue(any(item["query"] == "FAIL_QUERY"
                            for item in state["tool_failures"]))
        self.assertNotIn("provider detail", json.dumps(state))
        self.assertTrue(any(item["query"] == "no such record anywhere"
                            for item in state["attempted_empty_searches"]))
        self.assertTrue(any(item["query"] == "FAIL_QUERY"
                            for item in state["tool_history"]))
        self.assertEqual(state["gap_status"], "unresolved")
        self.assertGreaterEqual(len(state.get("completed_steps", [])), 3)
        self.assertTrue(result.context_trace)
        self.assertEqual(result.context_trace[-1]["context_schema_version"],
                         CONTEXT_SCHEMA_VERSION)

    def test_model_window_tokens_not_bytes_drive_usage_and_trace(self):
        agent = self.agent([
            {"action": "tool_call", "tool": "search_docs",
             "query": "Review ALPHA", "filters": {}},
            {"action": "finish", "claims": [self.claim(self.record)]},
        ], Budget(context_window_tokens=32000))
        result = agent.run_result("Why was the timeout chosen?")
        request = agent._call_llm.call_args_list[-1].args[0]
        state = json.loads(request[2]["content"])
        self.assertTrue(state["evidence_index"])
        self.assertTrue(result.context_trace)
        round_trace = result.context_trace[0]
        self.assertEqual(round_trace["model_context_window"], 32000)
        self.assertGreater(round_trace["estimated_input_tokens"], 0)
        self.assertEqual(round_trace["tokenizer_mode"], "estimated")
        self.assertEqual(round_trace["tokenizer_name"], "heuristic_v1")
        self.assertTrue(round_trace["categories_estimated"])
        self.assertFalse(round_trace["context_limit"])
        self.assertLess(round_trace["estimated_input_tokens"],
                        round_trace["model_context_window"])

    def test_token_budget_overflow_stops_before_model_call_with_trace(self):
        agent = self.agent([{"action": "finish"}], Budget(max_tokens=50))
        result = agent.run_result("A question that must not be silently cut")
        self.assertEqual(agent._call_llm.call_count, 0)
        self.assertEqual((result.termination_reason, result.termination_detail),
                         ("budget", "context_token_budget"))
        self.assertTrue(result.context_trace)
        detail = result.context_trace[0]
        self.assertEqual(detail["context_limit"]["reason"],
                         "mandatory_state_exceeds_budget")
        self.assertTrue(any("上下文预算不足" in gap for gap in result.gaps))


class ContextTraceApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cma-phase3-api-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "runtime.sqlite3"
        self.scope = EvidenceScope(project_id="phase3-api", dataset_kind="live")

    def test_context_trace_is_persisted_and_queryable_per_run(self):
        def behaviour(agent, question, context):
            agent.observer.emit("context_selected", {
                "round": 1, "step_index": 1,
                "context_schema_version": CONTEXT_SCHEMA_VERSION,
                "budget_policy_version": BUDGET_POLICY_VERSION,
                "model_context_window": 64000,
                "reserved_output_tokens": 2048,
                "estimated_input_tokens": 1200,
                "tokenizer_mode": "estimated",
                "candidate_evidence_count": 2,
                "selected_evidence_count": 1,
                "deduped_evidence_count": 1,
                "pruned_count": 1,
                "pruning_reasons": {"duplicate_source_text": 1},
                "important_conflicts_retained": True,
                "gap_count": 1,
                "context_limit": None,
            })
            agent.observer.emit("model_requested", {"round": 1,
                                                    "model": agent.model})
            return make_result(agent.scope, answer="trace answer")

        service = RunService(
            self.db, agent_factory=agent_factory(behaviour),
            config=RuntimeConfig(db_path=self.db, max_workers=1))
        self.addCleanup(service.close)
        client = TestClient(api.create_app(runtime_service=service))
        self.addCleanup(client.close)
        session = client.post("/sessions", json={
            "project_id": self.scope.project_id,
            "dataset_kind": "live"}).json()["session"]
        submitted = client.post("/runs", json={
            "session_id": session["session_id"], "question": "trace"})
        self.assertEqual(submitted.status_code, 202, submitted.text)
        run_id = submitted.json()["run"]["run_id"]
        wait_status(service, run_id, {"succeeded"})
        response = client.get(f"/runs/{run_id}/context-trace",
                              params={"project_id": self.scope.project_id})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["run_id"], run_id)
        self.assertEqual(len(body["rounds"]), 1)
        round_trace = body["rounds"][0]
        self.assertEqual(round_trace["context_schema_version"],
                         CONTEXT_SCHEMA_VERSION)
        self.assertEqual(round_trace["budget_policy_version"],
                         BUDGET_POLICY_VERSION)
        self.assertEqual(round_trace["selected_evidence_count"], 1)
        self.assertTrue(round_trace["important_conflicts_retained"])
        self.assertEqual(round_trace["context_limit"], None)
        self.assertIn("estimates", body["unit_note"])
        # The endpoint requires the owning project; a foreign query sees nothing.
        foreign = client.get(f"/runs/{run_id}/context-trace",
                             params={"project_id": "foreign-project"})
        self.assertEqual(foreign.status_code, 404)

        events = client.get(f"/runs/{run_id}/events",
                            params={"project_id": self.scope.project_id}).json()
        self.assertIn("context_selected",
                      [event["event_type"] for event in events["events"]])


if __name__ == "__main__":
    unittest.main()
