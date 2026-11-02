"""Scripted-model protocol/runtime tests. None measure real LLM effectiveness."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
from src.agents.react_agent import ReActAgent, ToolExecutor
from src.evidence import Evidence, EvidenceScope, legacy_evidence
from src.evidence_store import EvidenceStore
from src.ingest import normalize_record, ingest_all
from src.investigation import (Budget, Conflict, EvidenceLedger, EvidenceRef, FinishAction,
    ModelReply, TokenUsage, ToolAction, parse_action)
from src.skills.registry import SkillRegistry

from offline_env import OFFLINE_PROVIDER_ENV


def ref(record):
    return {"evidence_id": record["evidence_id"], "content_hash": record["content_hash"]}


def tool(name="search_memory", query="Decision", **extra):
    return {"action": "tool_call", "tool": name, "query": query, **extra}


def finish(record=None, **extra):
    claims = [] if record is None else [{"text": record["content"], "kind": "historical", "refs": [ref(record)]}]
    return {"action": "finish", "claims": claims, **extra}


class ActionTests(unittest.TestCase):
    def test_valid_legacy_and_new_protocol(self):
        self.assertIsInstance(parse_action(json.dumps(tool())), ToolAction)
        self.assertIsInstance(parse_action('{"action":"answer","summary":"old format"}'), FinishAction)
        self.assertIsInstance(parse_action('```json\n{"action":"finish"}\n```'), FinishAction)

    def test_invalid_json_tool_fields_types_lengths_and_scope(self):
        invalid = ["[]", "null", "not json", '{"action":"other"}',
                   json.dumps(tool("shell", "rm")), json.dumps(tool(query=" ")),
                   json.dumps(tool(query="x" * 2001)), json.dumps(tool(query=123)),
                   json.dumps(tool(filters={"project_id": "outside"})),
                   json.dumps(tool(filters={"db_path": "secret"})),
                   json.dumps(tool(filters={"dataset_kind": "fixture"})),
                   json.dumps(tool(user_id="other")), json.dumps(tool(filters=[])),
                   json.dumps(tool(filters={"after": "2026-01-01"})),
                   json.dumps(tool(filters={"evidence_ids": ["invented"]}))]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((ValueError, ValidationError)):
                parse_action(value)
        self.assertEqual(ReActAgent._parse_action("[]")["error_code"], "protocol_error")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-investigation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "evidence.sqlite3"
        self.scope = EvidenceScope(dataset_kind="live", project_id="investigation-project")
        self.a = normalize_record("Decision recorded due to latency.", "file:///project/issues", "issues", "I-1",
            project_id=self.scope.project_id, source_type="issue", event_time="2025-01-01T00:00:00+00:00")
        self.b = normalize_record("Later review changed timeout.", "file:///project/docs", "markdown", "review.md",
            project_id=self.scope.project_id, source_type="document", event_time="2025-03-01T00:00:00+00:00")
        self.store = EvidenceStore(self.db)
        self.store.append([self.a, self.b])
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {**OFFLINE_PROVIDER_ENV, "EVIDENCE_DB_PATH": str(self.db)}).start()

    def agent(self, actions, budget=None):
        agent = ReActAgent(scope=self.scope, db_path=self.db, budget=budget)
        agent.api_key = "scripted-model-no-network"
        iterator = iter(actions)
        agent._call_llm = lambda messages: json.dumps(next(iterator))
        return agent

    def test_ledger_dedup_versions_immutability_and_isolation(self):
        ledger = EvidenceLedger(self.scope)
        original = Evidence.model_validate(self.a)
        self.assertEqual(len(ledger.register([self.a], "search_issues")), 1)
        self.assertEqual(ledger.register([self.a], "search_memory"), [])
        modified = normalize_record("Decision revised.", original.source_uri, "issues", "I-1",
            project_id=self.scope.project_id, source_type="issue")
        self.assertEqual(len(ledger.register([modified], "search_issues")), 1)
        self.assertEqual(len(ledger.entries), 2)
        self.assertTrue(ledger.conflicts)
        ledger.entries[0].evidence.metadata["injected"] = True
        self.assertNotIn("injected", ledger.entries[0].evidence.metadata)
        self.assertEqual(EvidenceLedger(self.scope).entries, [])
        foreign = {**self.a, "project_id": "outside"}
        with self.assertRaises(ValueError):
            ledger.register([foreign], "search_memory")
        fixture = legacy_evidence({"content": "fixture"}).as_hit()
        with self.assertRaises(ValueError):
            ledger.register([fixture], "search_memory")

    def test_ledger_cannot_resolve_unobserved_or_changed_records(self):
        ledger = EvidenceLedger(self.scope)
        reference = EvidenceRef(**ref(self.a))
        with self.assertRaises(ValueError):
            ledger.resolve(reference, lambda *args: Evidence.model_validate(self.a))
        ledger.register([self.a], "search_issues")
        with self.assertRaises(ValueError):
            ledger.resolve(reference, lambda *args: None)

    def test_dynamic_tool_choice_from_previous_observation_and_sufficient(self):
        agent = self.agent([])
        messages_seen = []
        def script(messages):
            messages_seen.append(messages)
            if len(messages_seen) == 1:
                return json.dumps(tool("search_issues", "Decision"))
            observation = json.loads(messages[-1]["content"])
            if len(messages_seen) == 2:
                self.assertEqual(observation["new_evidence"][0]["evidence_id"], self.a["evidence_id"])
                return json.dumps(tool("search_docs", "review", filters={"after": self.a["event_time"]}))
            self.assertEqual(observation["new_evidence"][0]["evidence_id"], self.b["evidence_id"])
            return json.dumps(finish(self.a))
        agent._call_llm = script
        result = agent.run_result("Explain the recorded decision")
        self.assertEqual(result.termination_reason, "sufficient")
        self.assertEqual(len(result.evidence_refs), 2)
        self.assertEqual([s.tool_name for s in result.trace if s.action == "tool_attempt"], ["search_issues", "search_docs"])
        self.assertTrue(result.usage.estimated)
        self.assertNotIn("scripted-model-no-network", result.model_dump_json())

    def test_zero_evidence_and_summary_not_accepted(self):
        result = self.agent([{"action": "answer", "summary": "fabricated current fact"}]).run_result("question")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertFalse(result.claims)
        self.assertNotIn("fabricated current fact", result.answer)

    def test_current_validity_is_unknown_even_with_historical_claim(self):
        result = self.agent([tool(), finish(self.a)]).run_result("现在这个决策还有效吗？")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertIn("当前有效性未知", result.answer)

    def test_explicit_gap_prevents_sufficient_finish(self):
        result = self.agent([tool(), finish(self.a, gaps=["Missing later review evidence"])]).run_result("question")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertIn("Missing later review evidence", result.gaps)

    def test_unverified_legacy_cannot_become_confirmed_claim(self):
        from src.agents.answer_agent import AnswerAgent
        record = legacy_evidence({"content": "Unverified historical text"})
        ledger = EvidenceLedger(EvidenceScope())
        ledger.register([record.as_hit()], "search_memory")
        action = parse_action(json.dumps(finish(record.model_dump(mode="json"))))
        claims, gaps, conflicts, counts = AnswerAgent().compose_investigation(
            ledger, action, lambda *args: record)
        self.assertFalse(claims)
        self.assertTrue(any("legacy" in gap for gap in gaps))
        self.assertTrue(ledger.entries[0].evidence.metadata["legacy"])

    def test_fabricated_citation_and_nonverbatim_claim_blocked(self):
        bad = finish(self.a)
        bad["claims"][0]["refs"][0]["evidence_id"] = "ev_" + "0" * 64
        result = self.agent([tool(), bad]).run_result("question")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertNotIn("ev_" + "0" * 64, result.answer)
        self.assertTrue(any(e.error_code == "citation_error" for s in result.trace for e in s.errors))
        bad = finish(self.a)
        bad["claims"][0]["text"] = "Invented unstated cause"
        result = self.agent([tool(), bad]).run_result("question")
        self.assertFalse(result.claims)
        self.assertNotIn("Invented unstated cause", result.answer)

    def test_model_reported_conflict_discloses_both_references(self):
        conflict = {"description": "Two sources need reconciliation", "refs": [ref(self.a), ref(self.b)]}
        result = self.agent([tool(), tool("search_docs", "review"), finish(self.a, conflicts=[conflict])]).run_result("question")
        self.assertEqual(result.termination_reason, "conflict")
        self.assertFalse(result.claims)
        self.assertIn(self.a["evidence_id"], result.answer)
        self.assertIn(self.b["evidence_id"], result.answer)

    def test_protocol_error_recovers_without_changing_scope(self):
        result = self.agent([tool(filters={"project_id": "other"}), tool(), finish(self.a)]).run_result("question")
        self.assertEqual(result.termination_reason, "sufficient")
        self.assertEqual(result.scope, self.scope)
        self.assertEqual(result.trace[0].action, "protocol_error")

    def test_repeated_call_not_reexecuted_but_strategy_can_change(self):
        agent = self.agent([tool(), tool(), tool("search_docs", "review"), finish(self.a)])
        original = agent.executor.execute
        with patch.object(agent.executor, "execute", wraps=original) as execute:
            result = agent.run_result("question")
        self.assertEqual(execute.call_count, 2)
        self.assertTrue(any(e.error_code == "duplicate_call" for s in result.trace for e in s.errors))

    def test_retriable_failure_then_success(self):
        agent = self.agent([tool("search_issues"), finish(self.a)])
        underlying = agent.executor.registry.get("problem").run
        attempts = []
        def flaky(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise TimeoutError("secret must not leak")
            return underlying(*args, **kwargs)
        with patch.object(agent.executor.registry.get("problem"), "run", side_effect=flaky):
            result = agent.run_result("question")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(result.termination_reason, "sufficient")
        self.assertNotIn("secret must not leak", result.model_dump_json())

    def test_nonretryable_failure_switches_source(self):
        agent = self.agent([tool("search_issues"), tool("search_docs", "review"), finish(self.b)])
        with patch.object(agent.executor.registry.get("problem"), "run", side_effect=ValueError("private error")) as failed:
            result = agent.run_result("question")
        self.assertEqual(failed.call_count, 1)
        self.assertEqual(result.termination_reason, "sufficient")
        self.assertNotIn("private error", result.model_dump_json())

    def test_exhausted_retries_and_tool_failure_reason(self):
        agent = self.agent([tool("search_issues"), finish()])
        with patch.object(agent.executor.registry.get("problem"), "run", side_effect=TimeoutError()) as failed:
            result = agent.run_result("question")
        self.assertEqual(failed.call_count, 2)
        self.assertEqual(result.termination_reason, "tool_failure")
        self.assertFalse(result.evidence_refs)

    def test_tool_timeout_wait_limit_no_fake_cancellation(self):
        executor = ToolExecutor(scope=self.scope, db_path=self.db)
        release = threading.Event()
        def blocked(*args, **kwargs):
            release.wait(1)
            return {"evidence": [self.a], "sources": []}
        try:
            with patch.object(executor.registry.get("memory"), "run", side_effect=blocked):
                outcome = executor.execute("search_memory", "Decision", timeout=0.01)
                self.assertEqual(outcome["error"]["error_code"], "tool_timeout")
                self.assertEqual(executor.execute("search_docs", "review")["error"]["error_code"], "tool_busy")
                self.assertFalse(outcome["evidence"])
        finally:
            release.set()

    def test_model_round_tool_count_and_token_budgets(self):
        cases = [([tool()], Budget(max_model_rounds=1)),
                 ([tool(), tool("search_docs", "review")], Budget(max_tool_calls=1)),
                 ([tool()], Budget(max_tokens=1))]
        for actions, budget in cases:
            result = self.agent(actions, budget).run_result("question")
            self.assertEqual(result.termination_reason, "budget")

    def test_deadline_rejects_late_model_and_tool_results(self):
        agent = self.agent([], Budget(deadline_seconds=0.01))
        def slow(messages):
            time.sleep(0.02)
            return json.dumps(tool())
        agent._call_llm = slow
        result = agent.run_result("question")
        self.assertEqual(result.termination_reason, "budget")
        self.assertFalse(result.evidence_refs)

    def test_provider_timeout_is_capped_by_remaining_deadline_and_usage(self):
        agent = self.agent([], Budget(deadline_seconds=0.5, model_timeout=5.0))
        # Restore actual HTTP protocol method, intercept transport only; no network call.
        agent._call_llm = ReActAgent._call_llm.__get__(agent)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({"choices": [{"message": {"content": '{"action":"finish"}'}}],
                                   "usage": {"prompt_tokens": 17, "completion_tokens": 5}}).encode()
        with patch("urllib.request.urlopen", return_value=Response()) as transport:
            result = agent.run_result("question")
        self.assertGreater(transport.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(transport.call_args.kwargs["timeout"], 0.5)
        self.assertEqual(result.usage.input_tokens, 17)
        self.assertEqual(result.usage.output_tokens, 5)
        self.assertFalse(result.usage.estimated)

    def test_provider_usage_exhausts_budget(self):
        agent = self.agent([], Budget(max_tokens=10000))
        agent._call_llm = lambda messages: ModelReply(text=json.dumps(tool()), usage=TokenUsage(input_tokens=10000, estimated=False))
        result = agent.run_result("question")
        self.assertEqual(result.termination_reason, "budget")
        self.assertFalse(result.evidence_refs)

    def test_empty_optional_search_does_not_become_global_gap_after_evidence(self):
        evidence_result = {"evidence": [self.a], "retrieval_diagnostics": {
            "effective_mode": "hybrid", "candidate_count": 1, "accepted_count": 1}}
        empty_result = {"evidence": [], "retrieval_diagnostics": {
            "effective_mode": "hybrid", "candidate_count": 5, "accepted_count": 0,
            "empty_after_filtering": True, "empty_reason": "all_candidates_filtered"}}
        for order, responses in (
                ("evidence_then_empty", [evidence_result, empty_result]),
                ("empty_then_evidence", [empty_result, evidence_result])):
            with self.subTest(order=order):
                actions = [tool("search_issues", "Decision"),
                           tool("search_docs", "postmortem"), finish(self.a)]
                agent = self.agent(actions, Budget(
                    max_model_rounds=5, max_tokens=100000, max_output_tokens=8192))
                responses_iter = iter(responses)
                agent.executor.execute = lambda *args, **kwargs: next(responses_iter)
                result = agent.run_result("Why latency?")
                self.assertEqual(result.termination_reason, "sufficient")
                self.assertEqual(len(result.claims), 1)
                self.assertFalse(any("未取得可引用证据" in gap for gap in result.gaps))

    def test_multiple_runs_do_not_share_ledger_or_run_id(self):
        agent = self.agent([tool(), finish(self.a), finish(self.a)])
        first = agent.run_result("question")
        second = agent.run_result("question")
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertFalse(second.evidence_refs)
        self.assertEqual(second.termination_reason, "insufficient")

    def test_trace_export_redacts_private_body_query_and_model_summary(self):
        result = self.agent([tool(), finish(self.a)]).run_result("question")
        self.assertEqual([s.step_index for s in result.trace], list(range(1, len(result.trace) + 1)))
        self.assertEqual(result.trace[-1].termination_reason, result.termination_reason)
        for step in result.trace:
            self.assertTrue({"step_index", "action", "tool_name", "query", "evidence_ids", "errors", "elapsed_ms", "usage", "new_evidence", "termination_reason"}.issubset(step.model_dump()))
        retained = result.trace[-1].evidence_records
        self.assertEqual([item["evidence_id"] for item in retained],
                         [self.a["evidence_id"]])
        self.assertEqual(retained[0]["content"], self.a["content"])
        target = self.root / "opt-in-trace.jsonl"
        self.assertFalse(target.exists())
        result.export_jsonl(target)
        text = target.read_text(encoding="utf-8")
        self.assertNotIn(self.a["content"], text)
        self.assertNotIn('"query"', text)
        self.assertNotIn("scripted-model-no-network", text)
        self.assertEqual(len(text.splitlines()), len(result.trace))

    def test_api_structured_result_and_sse_single_execution(self):
        agent = self.agent([tool(), finish(self.a)])
        with patch("api._get_agent", return_value=agent), TestClient(api.app) as client:
            result = client.post("/chat", json={"backend": "react", "question": "question", "scope": self.scope.model_dump()}).json()
        self.assertEqual(result["termination_reason"], "sufficient")
        self.assertTrue(result["run_id"])
        agent = self.agent([tool(), finish(self.a)])
        with patch("api._get_agent", return_value=agent), patch.object(agent, "run_result", wraps=agent.run_result) as run, TestClient(api.app) as client:
            result = client.post("/chat/stream", json={"backend": "react", "question": "question", "scope": self.scope.model_dump()})
        self.assertEqual(run.call_count, 1)
        self.assertIn('"type": "investigation"', result.text)
        self.assertIn("[DONE]", result.text)

    def test_filters_apply_before_ranking_and_keep_scope(self):
        executor = ToolExecutor(scope=self.scope, db_path=self.db)
        result = executor.execute("search_memory", "Decision", filters={"evidence_ids": [self.b["evidence_id"]]})
        self.assertFalse(result["evidence"])
        result = executor.execute("search_memory", "review", filters={"source_types": ["document"], "after": "2025-02-01T00:00:00+00:00"})
        self.assertEqual(result["evidence"][0]["evidence_id"], self.b["evidence_id"])

    def test_real_git_three_source_steps_offline_trace(self):
        repo = self.root / "real-repo"
        repo.mkdir()
        def git(*args, env=None):
            return subprocess.check_output(["git", "-C", str(repo), *args], env=env, text=True,
                                           encoding="utf-8", stderr=subprocess.PIPE)
        git("init")
        git("config", "user.name", "Runtime Test")
        git("config", "user.email", "runtime@example.invalid")
        for timestamp, text, message in [
            ("2025-01-01T00:00:00+00:00", "Initial design", "Initial source design"),
            ("2025-02-01T00:00:00+00:00", "Choose bounded requests", "Decision documented"),
            ("2025-03-01T00:00:00+00:00", "Review recorded revised timeout.", "Review timeout adjustment")]:
            (repo / "design.md").write_text(text, encoding="utf-8")
            git("add", "design.md")
            git("commit", "-m", message, env={**os.environ, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp})
        ingest_all(repo_path=repo, docs_dir=repo, project_id=self.scope.project_id, db_path=self.db)
        agent = self.agent([])
        counter, acquired = [], []
        def script(messages):
            counter.append(1)
            if len(counter) == 1:
                return json.dumps(tool("search_memory", "Decision", filters={"source_types": ["git_commit"]}))
            obs = json.loads(messages[-1]["content"])
            acquired.extend(obs["evidence"])
            if len(counter) == 2:
                self.assertTrue(obs["new_evidence"])
                self.assertTrue(obs["evidence"][0]["locator"]["commit_sha"])
                return json.dumps(tool("search_issues", "adjustment", filters={"after": obs["evidence"][0]["event_time"]}))
            if len(counter) == 3:
                return json.dumps(tool("search_docs", "revised"))
            return json.dumps(finish(acquired[0]))
        agent._call_llm = script
        result = agent.run_result("Explain the recorded design changes")
        self.assertEqual(result.termination_reason, "sufficient")
        self.assertEqual([s.tool_name for s in result.trace if s.action == "tool_attempt"],
                         ["search_memory", "search_issues", "search_docs"])
        self.assertEqual(len(result.evidence_refs), 3)
        print("OFFLINE_GIT_TRACE=" + json.dumps({"run_id": result.run_id, "reason": result.termination_reason,
            "steps": [{"tool": s.tool_name, "ids": s.evidence_ids, "new": s.new_evidence}
                      for s in result.trace if s.action == "tool_attempt"]}))

    def test_final_refetch_failure_removes_unverifiable_citations(self):
        agent = self.agent([tool(), finish(self.a)])
        with patch.object(agent.executor.registry, "get_evidence", return_value=None):
            result = agent.run_result("question")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertFalse(result.evidence_refs)
        self.assertNotIn(self.a["evidence_id"], result.answer)

    def test_runtime_tool_timeout_returns_failure_without_late_evidence(self):
        agent = self.agent([tool(), finish()], Budget(tool_timeout=0.005))
        def delayed(*args, **kwargs):
            time.sleep(0.02)
            return {"evidence": [self.a]}
        with patch.object(agent.executor.registry.get("memory"), "run", side_effect=delayed):
            result = agent.run_result("question")
        self.assertEqual(result.termination_reason, "tool_failure")
        self.assertFalse(result.evidence_refs)


if __name__ == "__main__":
    unittest.main()
