"""Offline context/completeness regressions with real scoped storage and tools."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.agents.react_agent import ReActAgent, ToolExecutor, REACT_SYSTEM_PROMPT
from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore
from src.ingest import normalize_record
from src.investigation import Budget, EvidenceLedger, Filters
from src.investigation_context import InvestigationContext, ContextLimitError, Excerpt
from src.llm_smoke import SCENARIOS, prepare, check_multi_round_evidence

from offline_env import OFFLINE_PROVIDER_ENV


def claim(record, text=None):
    if isinstance(record, Evidence): record = record.model_dump(mode="json")
    return {"text": text or record["content"], "refs": [{"evidence_id": record["evidence_id"], "content_hash": record["content_hash"]}]}


def tool(query, **filters):
    return {"action": "tool_call", "tool": "search_memory", "query": query, "filters": filters}


def multi_round_probe(directory):
    """No provider: reproduce the old action pattern with real Git/SQLite/Skills."""
    db, scope = prepare(directory)
    store = EvidenceStore(db)
    records = store.list(scope)
    decision = next(r for r in records if r.source_type == "git_commit" and "bounded latency" in r.content)
    later = next(r for r in records if r.source_type == "git_commit" and "later timeout proposal" in r.content)
    review = next(r for r in records if r.source_type == "document")
    question = dict(SCENARIOS)["multi_round"]
    actions = [tool("Cedar decision commit", source_types=["git_commit"]),
        {"action": "tool_call", "tool": "search_docs", "query": "Cedar review", "filters": {"after": decision.event_time.isoformat()}},
        {"action": "tool_call", "tool": "search_issues", "query": "Cedar adjustment", "filters": {
            "source_types": ["issue", "log"], "after": decision.event_time.isoformat()}},
        {"action": "finish", "claims": [claim(decision, "Cedar chose B due to bounded latency"),
            claim(later, "Cedar later timeout proposal"), claim(review)]}]
    with patch.dict(os.environ, {**OFFLINE_PROVIDER_ENV, "DECISION_DB_PATH": str(Path(directory) / "absent.sqlite3")}):
        agent = ReActAgent(scope=scope, db_path=db, budget=Budget(max_model_rounds=7))
        agent.api_key = "offline-script-only"
        agent._call_llm = Mock(side_effect=[json.dumps(a) for a in actions])
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            result = agent.run_result(question)
    return result, check_multi_round_evidence(result, store, scope)


class ContextAndCoverageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cma-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scope = EvidenceScope(project_id="context-test", dataset_kind="live")
        self.db = self.root / "evidence.sqlite3"
        self.commit = normalize_record("A recorded decision reason.", "benchmark://ctx/repo", "commits", "1" * 40,
            project_id=self.scope.project_id, source_type="git_commit", event_time="2025-02-01T00:00:00+00:00",
            locator={"commit_sha": "1" * 40, "repository_uri": "benchmark://ctx/repo"})
        self.undated = normalize_record("Review ALPHA explains a later proposal.", "benchmark://ctx/docs", "docs", "review.md",
            project_id=self.scope.project_id, locator={"path": "review.md"})
        self.old = normalize_record("Review ALPHA older dated record.", "benchmark://ctx/docs", "docs", "old.md",
            project_id=self.scope.project_id, event_time="2020-01-01T00:00:00+00:00")
        self.store = EvidenceStore(self.db)
        self.store.append([self.commit, self.undated, self.old])
        env = patch.dict(os.environ, {**OFFLINE_PROVIDER_ENV, "DECISION_DB_PATH": str(self.root / "absent.sqlite3")})
        env.start(); self.addCleanup(env.stop)

    def agent(self, actions, budget=None):
        agent = ReActAgent(scope=self.scope, db_path=self.db, budget=budget)
        agent.api_key = "offline-script-only"
        agent._call_llm = Mock(side_effect=[json.dumps(a) for a in actions])
        return agent

    def test_strict_dates_and_undated_only_preserve_scope_and_exclude_known_outside(self):
        executor = ToolExecutor(scope=self.scope, db_path=self.db)
        self.assertFalse(executor.execute("search_docs", "Review ALPHA", filters={"after": "2025-01-01T00:00:00Z"})["evidence"])
        hits = executor.execute("search_docs", "Review ALPHA", filters={"undated_only": True})["evidence"]
        self.assertEqual([h["evidence_id"] for h in hits], [self.undated["evidence_id"]])
        self.assertIsNone(hits[0]["event_time"])
        with self.assertRaises(ValueError): Filters(undated_only=True, after="2025-01-01T00:00:00Z")
        foreign = ToolExecutor(scope=EvidenceScope(project_id="foreign", dataset_kind="live"), db_path=self.db)
        self.assertFalse(foreign.execute("search_docs", "Review ALPHA", filters={"undated_only": True})["evidence"])

    def test_empty_date_search_has_one_explicit_undated_recovery(self):
        agent = self.agent([tool("Review ALPHA", source_types=["document"], after="2025-01-01T00:00:00Z"),
                            {"action": "finish", "claims": [claim(self.undated)]}])
        result = agent.run_result("Review documents")
        attempts = [s for s in result.trace if s.action == "tool_attempt"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].evidence_ids, [])
        self.assertTrue(attempts[1].filters["undated_only"])
        self.assertIsNone(attempts[1].filters["after"])
        self.assertEqual(attempts[1].recovery_kind, "undated_time_relaxation")
        self.assertEqual(attempts[1].evidence_ids, [self.undated["evidence_id"]])
        observation = json.loads(agent._call_llm.call_args_list[1].args[0][-1]["content"])
        self.assertEqual(observation["evidence"][0]["time_status"], "unknown")
        self.assertEqual(observation["time_relaxation"]["original_filters"]["after"], "2025-01-01T00:00:00Z")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertTrue(any("时间范围仍未知" in gap for gap in result.gaps))

    def test_relaxation_limit_and_tool_budget_are_enforced(self):
        actions = [tool("Review ALPHA", source_types=["document"], after="2025-01-01T00:00:00Z"),
            tool("Nonexistent source", source_types=["document"], after="2025-01-01T00:00:00Z"), {"action": "finish"}]
        result = self.agent(actions).run_result("Question")
        self.assertEqual(sum(s.recovery_kind is not None for s in result.trace), 1)
        self.assertEqual(sum(s.action == "tool_attempt" for s in result.trace), 3)
        limited = self.agent(actions, Budget(max_tool_calls=1)).run_result("Question")
        self.assertEqual((limited.termination_reason, limited.termination_detail), ("budget", "tool_call_budget"))
        self.assertFalse(limited.evidence_refs)
        self.assertTrue(any("补查" in gap for gap in limited.gaps))

    def test_tail_fact_is_in_verified_prompt_excerpt_parent_is_unchanged(self):
        text = "header irrelevant\n" * 200 + "TAIL_UNIQUE: bounded result from the final review.\n"
        record = normalize_record(text, "benchmark://ctx/docs", "docs", "long.md", project_id=self.scope.project_id,
                                  locator={"path": "long.md"})
        self.store.append([record])
        executor = ToolExecutor(scope=self.scope, db_path=self.db)
        hit = executor.execute("search_docs", "TAIL_UNIQUE")["evidence"][0]
        self.assertEqual(hit["content"], text)
        self.assertGreater(hit["excerpt"]["char_start"], 700)
        self.assertIn("TAIL_UNIQUE", hit["excerpt"]["text"])
        parent = self.store.get(record["evidence_id"], self.scope, record["content_hash"])
        Excerpt.model_validate(hit["excerpt"]).verify(parent)
        agent = self.agent([tool("TAIL_UNIQUE"), {"action": "finish", "claims": [claim(record, "TAIL_UNIQUE: bounded result from the final review.")]}])
        result = agent.run_result("Historical review")
        observation = json.loads(agent._call_llm.call_args_list[1].args[0][-1]["content"])
        self.assertIn("TAIL_UNIQUE", observation["evidence"][0]["content"])
        self.assertEqual(result.termination_reason, "sufficient")
        self.assertEqual(parent.content, text)

    def test_excerpt_tampering_is_rejected_atomically_by_ledger(self):
        hit = ToolExecutor(scope=self.scope, db_path=self.db).execute("search_docs", "Review ALPHA", filters={"undated_only": True})["evidence"][0]
        for field, value in (("text", "invented"), ("char_end", 99999), ("line_start", 99),
                             ("content_hash", "0" * 64), ("project_id", "foreign"), ("dataset_kind", "fixture")):
            with self.subTest(field=field):
                bad = copy.deepcopy(hit)
                bad["excerpt"][field] = value
                ledger = EvidenceLedger(self.scope)
                with self.assertRaises(ValueError): ledger.register([hit, bad], "search_docs")
                self.assertFalse(ledger.entries)

    def test_repeated_evidence_has_one_body_per_request_not_two(self):
        actions = [tool("Review ALPHA", undated_only=True), tool("Review ALPHA proposal", undated_only=True), {"action": "finish"}]
        agent = self.agent(actions)
        agent.run_result("Question")
        request = agent._call_llm.call_args_list[2].args[0]
        self.assertEqual(json.dumps(request).count(self.undated["content"]), 1)
        observation = json.loads(request[-1]["content"])
        self.assertEqual(observation["evidence"][0]["content"], "")
        state = json.loads(request[2]["content"])
        self.assertEqual(len(state["evidence_index"]), 1)
        self.assertTrue(state["evidence_index"][0]["excerpts"])
        self.assertGreaterEqual(len(state["tool_history"]), 2)

    def test_context_trimming_preserves_index_locator_question_and_error_state(self):
        text = "ALPHA preserved evidence body " * 80
        record = normalize_record(text, "benchmark://ctx/docs", "docs", "trim.md", project_id=self.scope.project_id)
        self.store.append([record])
        items = ToolExecutor(scope=self.scope, db_path=self.db).execute("search_docs", "preserved evidence")["evidence"]
        ledger = EvidenceLedger(self.scope); ledger.register(items, "search_docs")
        context = InvestigationContext(); views = context.observe(items)
        messages = [{"role": "system", "content": "Read-only"}, {"role": "user", "content": "Original user task"},
            {"role": "assistant", "content": "action"}, {"role": "user", "content": json.dumps({"evidence": views, "error": {"error_code": "tool_error"}})}]
        _, full = context.request(messages, ledger, [], {}, {}, 100000)
        request, usage = context.request(messages, ledger, [], {}, {}, full["input_bytes"] - 300)
        self.assertLessEqual(usage["input_bytes"], full["input_bytes"] - 300)
        self.assertGreater(usage["omitted_excerpt_bodies"], 0)
        self.assertEqual(request[1]["content"], "Original user task")
        state = json.loads(request[2]["content"])
        self.assertEqual(state["evidence_index"][0]["content_hash"], record["content_hash"])
        self.assertTrue(state["evidence_index"][0]["excerpts"])
        self.assertIn("tool_error", request[-1]["content"])
        self.assertEqual(usage["unit"], "utf8_bytes_not_model_tokens")
        with self.assertRaises(ContextLimitError): context.request(messages, ledger, [], {}, {}, 1)

    def test_context_budget_stops_before_model_call(self):
        agent = self.agent([{"action": "finish"}], Budget(max_context_bytes=10))
        result = agent.run_result("A user task which must never be cut")
        self.assertEqual(agent._call_llm.call_count, 0)
        self.assertEqual((result.termination_reason, result.termination_detail), ("budget", "context_byte_budget"))
        self.assertTrue(any("上下文" in gap for gap in result.gaps))

    def test_protocol_repair_keeps_conflicts_and_unresolved_time_state(self):
        ledger = EvidenceLedger(self.scope)
        revised = normalize_record("Review ALPHA revised.", "benchmark://ctx/docs", "docs", "review.md", project_id=self.scope.project_id)
        ledger.register([self.undated, revised], "search_docs")
        messages = [{"role": "system", "content": "Read only"}, {"role": "user", "content": "Original task"},
                    {"role": "user", "content": '{"error_code":"protocol_error"}'}]
        request, _ = InvestigationContext().request(messages, ledger, [], {}, {}, 48000, gaps=["原日期范围仍未知"])
        state = json.loads(request[2]["content"])
        self.assertTrue(state["conflicts"])
        self.assertIn("原日期范围仍未知", state["gaps"])
        self.assertTrue(all(e["time_status"] == "unknown" for e in state["evidence_index"]))

    def test_partial_source_coverage_and_budget_report_unfinished_task(self):
        agent = self.agent([tool("decision", source_types=["git_commit"]), {"action": "finish", "claims": [claim(self.commit)]}])
        result = agent.run_result("Investigate commits and review documents")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertEqual(result.task_coverage["missing_source_types"], ["document"])
        limited = self.agent([tool("decision", source_types=["git_commit"])], Budget(max_model_rounds=1)).run_result("Investigate commits and documents")
        self.assertEqual(limited.termination_reason, "budget")
        self.assertTrue(any("document" in gap for gap in limited.gaps))
        self.assertNotIn("Cedar", REACT_SYSTEM_PROMPT)
        self.assertNotIn("bounded latency", REACT_SYSTEM_PROMPT)

    def test_real_git_multi_round_gets_review_with_unknown_time_and_full_role_coverage(self):
        self.assertNotIn("至多2条", REACT_SYSTEM_PROMPT)
        result, check = multi_round_probe(self.root)
        self.assertTrue(check["passed"])
        self.assertTrue(check["chronology_verified"])
        self.assertTrue(check["decision_time_followup"])
        self.assertEqual(result.task_coverage["missing_source_types"], [])
        self.assertEqual(result.termination_reason, "insufficient")  # unknown document time remains unknown
        self.assertEqual(sum(s.recovery_kind is not None for s in result.trace), 1)
        print("OFFLINE_MULTI_CONTEXT=" + json.dumps({"verification_mode": "scripted_model_real_git_tools_offline",
            "termination": result.termination_reason, "role_coverage": check["role_coverage"],
            "tool_attempts": [{"tool": s.tool_name, "count": len(s.evidence_ids), "recovery": s.recovery_kind}
                              for s in result.trace if s.action == "tool_attempt"]}))

    def test_multi_round_with_many_commits_but_no_review_does_not_pass(self):
        db, scope = prepare(self.root)
        store = EvidenceStore(db)
        git = [r for r in store.list(scope) if r.source_type == "git_commit"]
        agent = ReActAgent(scope=scope, db_path=db)
        agent.api_key = "offline-script-only"
        agent._call_llm = Mock(side_effect=[json.dumps(tool("Cedar", source_types=["git_commit"])),
            json.dumps({"action": "tool_call", "tool": "search_issues", "query": "Cedar"}),
            json.dumps({"action": "finish", "claims": [claim(r) for r in git]})])
        result = agent.run_result(dict(SCENARIOS)["multi_round"])
        self.assertGreaterEqual(len(result.evidence_refs), 2)
        self.assertGreaterEqual(sum(s.action == "tool_attempt" for s in result.trace), 2)
        check = check_multi_round_evidence(result, store, scope)
        self.assertFalse(check["passed"])
        self.assertIn("review_document", check["missing_roles"])


if __name__ == "__main__":
    unittest.main()
