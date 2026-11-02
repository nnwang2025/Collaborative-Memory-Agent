"""Offline real-Git/Skill/Runtime tests; scripted responses never call a provider."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.agents.react_agent import ReActAgent
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.investigation import Budget, InvestigationResult, ModelReply, TokenUsage
from src.llm_smoke import check_cross_source_evidence, prepare, run_suite

from offline_env import OFFLINE_PROVIDER_ENV


class CrossSourceSmokeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cma-cross-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db, self.scope = prepare(self.root)
        self.store = EvidenceStore(self.db)
        records = self.store.list(self.scope)
        self.commit = next(r for r in records if r.source_type == "git_commit" and "bounded latency" in r.content)
        self.initial = next(r for r in records if r.source_type == "git_commit" and "initial design" in r.content)
        self.document = next(r for r in records if r.source_type == "document")
        env = patch.dict(os.environ, {**OFFLINE_PROVIDER_ENV, "EVIDENCE_DB_PATH": str(self.db),
                                     "DECISION_DB_PATH": str(self.root / "absent-memory.sqlite3")})
        env.start(); self.addCleanup(env.stop)

    def claim(self, record, text):
        return {"text": text, "kind": "historical", "refs": [
            {"evidence_id": record.evidence_id, "content_hash": record.content_hash}]}

    def finish(self, claims=None):
        return {"action": "finish", "claims": claims if claims is not None else [
            self.claim(self.commit, "Cedar chose B due to bounded latency"), self.claim(self.document, self.document.content)],
            "gaps": ["Production deployment is not evidenced."], "requires_current_status": True}

    def actions(self):
        return [{"action": "tool_call", "tool": "search_memory", "query": "Cedar decision reasons commits review documents",
                 "filters": {"source_types": ["git_commit", "document"]}}, self.finish()]

    def run_script(self, actions):
        agent = ReActAgent(scope=self.scope, db_path=self.db, budget=Budget(max_model_rounds=7))
        agent.api_key = "offline-unit-test-only"
        agent._call_llm = Mock(side_effect=[json.dumps(action) for action in actions])
        with patch("urllib.request.urlopen", side_effect=AssertionError("No external model calls")):
            result = agent.run_result("Investigate Cedar decision reasons in commits and its later review in documents; cite both.")
        return result, agent

    def test_one_tool_two_sources_with_cited_roles_passes_even_if_insufficient(self):
        result, agent = self.run_script(self.actions())
        check = check_cross_source_evidence(result, self.store, self.scope)
        self.assertEqual(agent._call_llm.call_count, 2)
        self.assertEqual([s.tool_name for s in result.trace if s.action == "tool_attempt"], ["search_memory"])
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertTrue(check["passed"])
        self.assertEqual(check["role_coverage"], {"decision_reason": True, "later_review": True})
        self.assertEqual(check["cited_source_types"], ["document", "git_commit"])
        self.assertEqual(len({e["source_uri"] for e in check["cited_evidence"]}), 1)  # source URI alone is not the type.

    def test_retrieved_document_without_citing_it_does_not_pass(self):
        actions = self.actions()
        actions[-1] = self.finish([self.claim(self.commit, "Cedar chose B due to bounded latency")])
        result, _ = self.run_script(actions)
        check = check_cross_source_evidence(result, self.store, self.scope)
        self.assertIn("document", check["retrieved_source_types"])
        self.assertEqual(check["missing_roles"], ["later_review"])
        self.assertFalse(check["passed"])

    def test_short_verbatim_excerpts_can_cover_both_roles(self):
        actions = self.actions()
        actions[-1] = self.finish([self.claim(self.commit, "bounded latency"), self.claim(self.document, "adjusted timeout")])
        result, _ = self.run_script(actions)
        self.assertTrue(check_cross_source_evidence(result, self.store, self.scope)["passed"])

    def test_two_sources_but_missing_decision_reason_does_not_pass(self):
        actions = self.actions()
        actions[-1] = self.finish([self.claim(self.initial, "Cedar initial design"), self.claim(self.document, self.document.content)])
        result, _ = self.run_script(actions)
        check = check_cross_source_evidence(result, self.store, self.scope)
        self.assertEqual(check["cited_source_types"], ["document", "git_commit"])
        self.assertEqual(check["missing_roles"], ["decision_reason"])
        self.assertFalse(check["passed"])

    def test_multiple_tools_only_git_evidence_does_not_pass(self):
        actions = [{"action": "tool_call", "tool": name, "query": "Cedar", "filters": {"source_types": ["git_commit"]}}
                   for name in ["search_memory", "search_issues"]]
        actions.append(self.finish([self.claim(self.commit, "Cedar chose B due to bounded latency"),
                                    self.claim(self.initial, "Cedar initial design")]))
        result, _ = self.run_script(actions)
        self.assertEqual(len({s.tool_name for s in result.trace if s.action == "tool_attempt"}), 2)
        check = check_cross_source_evidence(result, self.store, self.scope)
        self.assertFalse(check["passed"])
        self.assertEqual(check["cited_source_types"], ["git_commit"])

    def test_bad_hash_scope_or_unobserved_reference_cannot_count_as_coverage(self):
        original, _ = self.run_script(self.actions())
        for variant in ("hash", "scope", "dataset", "ledger", "trace"):
            with self.subTest(variant=variant):
                result = original.model_copy(deep=True)
                if variant == "hash": result.claims[1].refs[0].content_hash = "0" * 64
                elif variant == "scope": result.scope = EvidenceScope(project_id="foreign", dataset_kind="live")
                elif variant == "dataset": result.scope = EvidenceScope(project_id=self.scope.project_id, dataset_kind="fixture")
                elif variant == "ledger": result.evidence_refs = [r for r in result.evidence_refs if r.evidence_id != self.document.evidence_id]
                else:
                    for step in result.trace:
                        step.evidence_ids = [i for i in step.evidence_ids if i != self.document.evidence_id]
                self.assertFalse(check_cross_source_evidence(result, self.store, self.scope)["passed"])

    def test_zero_evidence_and_unsupported_claim_do_not_pass(self):
        result = InvestigationResult(run_id="empty", scope=self.scope, answer="", termination_reason="insufficient")
        self.assertFalse(check_cross_source_evidence(result, self.store, self.scope)["passed"])
        result, _ = self.run_script(self.actions())
        result.claims[0].text = "Unsupported fabricated claim"
        self.assertFalse(check_cross_source_evidence(result, self.store, self.scope)["passed"])

    def test_smoke_runner_uses_real_evidence_coverage_not_tool_count(self):
        original = ReActAgent
        def factory(**kwargs):
            agent = original(**kwargs)
            agent.api_key = "offline-unit-test-key"
            agent._call_llm = Mock(side_effect=[ModelReply(text=json.dumps(action), finish_reason="stop",
                usage=TokenUsage(input_tokens=10, output_tokens=10, estimated=False)) for action in self.actions()])
            return agent
        target = self.root / "fixture-report.json"
        with patch("src.llm_smoke.prepare", return_value=(self.db, self.scope)), \
             patch("src.agents.react_agent.ReActAgent", side_effect=factory), \
             patch.dict(os.environ, {"SILICONFLOW_BASE_URL": "https://synthetic.invalid/v1",
                                     "DEEPSEEK_BASE_URL": ""}), \
             patch("urllib.request.urlopen", side_effect=AssertionError("No external model calls")):
            report = run_suite(online=True, approved_origin="https://synthetic.invalid", output=target)
        row = next(r for r in report["results"] if r["scenario"] == "cross_source")
        self.assertEqual(row["status"], "passed")
        self.assertEqual(row["tool_order"], ["search_memory"])
        self.assertEqual(row["model_calls"], 2)
        self.assertEqual(row["termination_reason"], "insufficient")
        self.assertEqual(row["action_protocol_errors"], 0)
        self.assertTrue(row["cross_source_check"]["passed"])
        self.assertIsNone(row["task_success"])
        self.assertEqual(row["task_grading"], "pending_human_review")
        self.assertNotIn("offline-unit-test-key", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
