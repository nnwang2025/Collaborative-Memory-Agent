"""P0 regression: conflicts must be scoped to the claims they can affect.

The cross-atlas fixture below is a deterministic regression fixture built from
the historical trace's evidence references and conflict value pair.  It is not
a replay of the original model's Actions or hidden reasoning.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark.dataset import load as load_dataset
from benchmark.tasks.scoring import _answer_body, _group_matches, load_tasks, score_task
from src.agents.answer_agent import AnswerAgent
from src.agents.react_agent import ReActAgent, finish_termination_reason
from src.evidence import EvidenceScope
from src.ingest import normalize_record
from src.investigation import (Claim, Conflict, EvidenceLedger, EvidenceRef, FinishAction)

from offline_env import OFFLINE_PROVIDER_ENV

CROSS_ATLAS = {
    "chat": "ev_9ccc9fcf6f0f451cfc3a4ecc8d405ebe7d8f54e05f1bd49b7d03740f5124b7aa",
    "issue": "ev_040364740331b53779419de0bb315a9c06aa4ef378aee78b41b9a90adbd5fe0a",
    "proposal_c": "ev_e8a6134474085c010b751f866e005349c83c61801f71fca1fe0281d8c3273295",
    "ack_exactly_once": "ev_fec06e3f5d2acba37db2d8ab162b3506de32c66a3132ce8abb08ccc185ee8d06",
    "ack_at_least_once": "ev_6847f3e60f6f78f80ed8d9932655b2063ad75c4915429fd17a941ca7ac70650d",
}


def _ref(record):
    return EvidenceRef(evidence_id=record.evidence_id, content_hash=record.content_hash)


class ConflictScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()
        cls.records = {record.evidence_id: record for record in cls.dataset["records"]}

    def setUp(self):
        env = patch.dict(os.environ, OFFLINE_PROVIDER_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def ledger(self, *keys):
        records = [self.records[CROSS_ATLAS[key]] for key in keys]
        scope = EvidenceScope(project_id=records[0].project_id,
                              dataset_kind=records[0].dataset_kind)
        ledger = EvidenceLedger(scope)
        ledger.register([record.model_dump(mode="json") for record in records], "search_memory")
        return ledger

    def getter(self, evidence_id, content_hash):
        record = self.records.get(evidence_id)
        return record if record is not None and record.content_hash == content_hash else None

    def claim(self, key):
        record = self.records[CROSS_ATLAS[key]]
        return Claim(text=record.content, refs=[_ref(record)])

    def model_conflict(self):
        first = self.records[CROSS_ATLAS["issue"]]
        second = self.records[CROSS_ATLAS["proposal_c"]]
        return Conflict(description="历史批准 B 与后续提议 C 尚未裁决。",
                        refs=[_ref(first), _ref(second)])

    def test_unrelated_conflict_does_not_clear_independent_claim(self):
        ledger = self.ledger("ack_exactly_once", "ack_at_least_once", "chat")
        finish = FinishAction(claims=[self.claim("chat")])
        claims, gaps, conflicts, counts = AnswerAgent().compose_investigation(
            ledger, finish, self.getter)
        self.assertEqual(counts["claims_accepted"], 1)
        self.assertEqual(counts["claims_discarded_by_conflict"], 0)
        self.assertEqual(claims[0].verification_status, "supported_extract")
        structured = [conflict for conflict in conflicts
                      if conflict.origin == "structured_assertion"]
        self.assertEqual(len(structured), 1)
        self.assertEqual(structured[0].relation, "independent_of_submitted_claims")
        self.assertEqual(structured[0].fields, ["ack_mode"])
        self.assertFalse(any("冲突" in gap for gap in gaps))
        self.assertEqual(finish_termination_reason(conflicts, gaps, claims), "sufficient")
        body, basis = _answer_body(AnswerAgent().render_investigation(
            ledger, claims, gaps, conflicts, "sufficient"))
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn(self.records[CROSS_ATLAS["chat"]].content, body)

    def test_directly_related_conflict_withholds_affected_claim(self):
        ledger = self.ledger("issue", "proposal_c")
        finish = FinishAction(claims=[self.claim("issue")], conflicts=[self.model_conflict()])
        claims, gaps, conflicts, counts = AnswerAgent().compose_investigation(
            ledger, finish, self.getter)
        self.assertEqual(claims, [])
        self.assertEqual(counts["claims_submitted"], 1)
        self.assertEqual(counts["claims_discarded_by_conflict"], 1)
        self.assertEqual(conflicts[0].relation, "affects_submitted_claim")
        self.assertEqual(conflicts[0].affected_claim_indexes, [0])
        self.assertTrue(any("直接影响结论" in gap for gap in gaps))
        answer = AnswerAgent().render_investigation(ledger, claims, gaps, conflicts, "conflict")
        body, basis = _answer_body(answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn("检测到未裁决冲突", body)
        self.assertIn("冲突相关原文", body)
        self.assertNotIn("历史来源记载：" + self.records[CROSS_ATLAS["issue"]].content, body)

    def test_multiple_claims_only_affected_claim_is_withheld(self):
        ledger = self.ledger("ack_exactly_once", "ack_at_least_once",
                             "chat", "issue", "proposal_c")
        finish = FinishAction(
            claims=[self.claim("chat"), self.claim("issue")],
            conflicts=[self.model_conflict()])
        claims, gaps, conflicts, counts = AnswerAgent().compose_investigation(
            ledger, finish, self.getter)
        self.assertEqual(counts, {
            "claims_submitted": 2,
            "claims_accepted": 1,
            "claims_discarded_by_validation": 0,
            "claims_discarded_by_conflict": 1,
            "claims_retained_with_unresolved_conflict": 1,
        })
        self.assertEqual(claims[0].text, self.records[CROSS_ATLAS["chat"]].content)
        self.assertEqual(claims[0].verification_status, "pending_unattributable_conflict")
        related = [conflict for conflict in conflicts
                   if conflict.relation == "affects_submitted_claim"]
        self.assertEqual(related[0].affected_claim_indexes, [1])
        answer = AnswerAgent().render_investigation(ledger, claims, gaps, conflicts, "conflict")
        body, basis = _answer_body(answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn("检测到未裁决冲突", body)
        self.assertIn("待核实", answer)
        self.assertNotIn("历史来源记载：" + self.records[CROSS_ATLAS["issue"]].content, body)

    def test_unattributable_conflict_marks_claim_but_does_not_erase_it(self):
        ledger = self.ledger("chat", "issue", "proposal_c")
        finish = FinishAction(claims=[self.claim("chat")], conflicts=[self.model_conflict()])
        claims, gaps, conflicts, counts = AnswerAgent().compose_investigation(
            ledger, finish, self.getter)
        self.assertEqual(counts["claims_accepted"], 1)
        self.assertEqual(counts["claims_retained_with_unresolved_conflict"], 1)
        self.assertEqual(claims[0].verification_status, "pending_unattributable_conflict")
        self.assertEqual(conflicts[0].relation, "unattributable")
        self.assertTrue(any("无法可靠归属" in gap for gap in gaps))
        answer = AnswerAgent().render_investigation(ledger, claims, gaps, conflicts, "conflict")
        body, basis = _answer_body(answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn("检测到未裁决冲突", body)
        self.assertNotIn(self.records[CROSS_ATLAS["chat"]].content, body)
        self.assertIn("待核实", answer)

    def test_evidence_identity_validation_still_rejects_tampered_reference(self):
        ledger = self.ledger("issue", "proposal_c")
        tampered = self.claim("issue")
        tampered.refs[0] = EvidenceRef(
            evidence_id=tampered.refs[0].evidence_id, content_hash="0" * 64)
        with self.assertRaises(ValueError):
            AnswerAgent().compose_investigation(
                ledger, FinishAction(claims=[tampered]), self.getter)
        conflict = self.model_conflict()
        conflict.refs[0] = EvidenceRef(evidence_id=conflict.refs[0].evidence_id,
                                       content_hash="0" * 64)
        with self.assertRaises(ValueError):
            AnswerAgent().compose_investigation(
                ledger, FinishAction(conflicts=[conflict]), self.getter)

    def test_subject_and_field_are_required_together_for_attribution(self):
        project = "subject-scope-project"
        scope = EvidenceScope(project_id=project, dataset_kind="live")
        records = [
            normalize_record("Atlas mode 记录 A。", "file:///a", "doc", "A",
                             {"subject": "Atlas", "assertions": {"mode": "A"}},
                             project_id=project, event_time="2025-01-01T00:00:00+00:00"),
            normalize_record("Atlas mode 记录 B。", "file:///b", "doc", "B",
                             {"subject": "Atlas", "assertions": {"mode": "B"}},
                             project_id=project, event_time="2025-01-01T00:00:00+00:00"),
            normalize_record("Atlas mode 摘要 A。", "file:///c", "doc", "C", {},
                             project_id=project, event_time="2025-01-02T00:00:00+00:00"),
            normalize_record("Zephyr mode 摘要 A。", "file:///d", "doc", "D", {},
                             project_id=project, event_time="2025-01-02T00:00:00+00:00"),
        ]
        ledger = EvidenceLedger(scope)
        ledger.register(records, "search_docs")
        self.assertEqual(len(ledger.conflicts), 1)
        self.assertEqual(ledger.conflicts[0].fields, ["mode"])
        self.assertEqual(ledger.conflicts[0].subject, "Atlas")
        atlas, zephyr = records[2], records[3]
        finish = FinishAction(claims=[
            Claim(text=atlas["content"], refs=[EvidenceRef.model_validate(
                {"evidence_id": atlas["evidence_id"], "content_hash": atlas["content_hash"]})]),
            Claim(text=zephyr["content"], refs=[EvidenceRef.model_validate(
                {"evidence_id": zephyr["evidence_id"], "content_hash": zephyr["content_hash"]})]),
        ])
        by_id = {record["evidence_id"]: record for record in records}
        claims, gaps, conflicts, counts = AnswerAgent().compose_investigation(
            ledger, finish, lambda evidence_id, content_hash: by_id.get(evidence_id))
        self.assertEqual(counts["claims_discarded_by_conflict"], 1)
        self.assertEqual(counts["claims_accepted"], 1)
        self.assertEqual(conflicts[0].affected_claim_indexes, [0])
        self.assertEqual(claims[0].text, zephyr["content"])
        self.assertEqual(claims[0].verification_status, "supported_extract")

    def test_fact_coverage_never_comes_from_evidence_or_gaps_alone(self):
        tasks = load_tasks()
        task = tasks["by_id"]["single-atlas"]
        label = tasks["labels"].labels["single-atlas"]
        facts = [fact.statement for fact in label.key_facts]
        rendered_without_claim = "\n".join([
            "【回答】(ReAct)", "run_id: fixture", "termination_reason: insufficient",
            "证据不足以确认完整结论；以下仅为工具取得的来源片段。",
            "【冲突（未裁决）】", *[f"- {fact}" for fact in facts],
            "【证据】", *[f"- {fact}" for fact in facts],
            "【未解决的缺口】", *[f"- {fact}" for fact in facts],
        ])
        # The old whole-answer scan would have matched every fact here.
        for fact in label.key_facts:
            self.assertTrue(all(_group_matches(rendered_without_claim, fact.marker_groups)))
        raw = self.raw(rendered_without_claim)
        row = score_task(raw, task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        self.assertIsNone(row["metrics"]["task_fact_coverage"])
        self.assertEqual(row["metrics"]["task_fact_coverage_status"],
                         "unverified_no_answer_body")
        self.assertEqual(row["claim_counts"], {})
        rendered_claim = "\n".join([
            "【回答】(ReAct)", "run_id: fixture", "termination_reason: sufficient",
            "【有引用支持的原文摘录（非独立语义核验）】",
            *[f"- 历史来源记载：{fact} [{CROSS_ATLAS['chat']}@{'0' * 64}]" for fact in facts],
            "【证据】", *[f"- {fact}" for fact in facts],
        ])
        row = score_task(self.raw(rendered_claim), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        self.assertEqual(row["metrics"]["task_fact_coverage"], 1.0)
        self.assertEqual(row["metrics"]["task_fact_coverage_status"], "rendered_claim_section")

    def test_retained_claim_with_unresolved_conflict_is_not_sufficient(self):
        with tempfile.TemporaryDirectory(prefix="cma-conflict-scope-") as temporary:
            agent = ReActAgent(scope=EvidenceScope(project_id="bench-atlas", dataset_kind="live"),
                               db_path=Path(temporary) / "evidence.sqlite3")
            agent.api_key = "scripted-model-no-network"
            selected = [self.records[CROSS_ATLAS[key]]
                        for key in ("chat", "issue", "proposal_c")]
            agent.executor.execute = lambda *args, **kwargs: {
                "tool": "search_memory", "skill": "memory", "query": "q",
                "evidence": [record.model_dump(mode="json") for record in selected],
                "sources": [], "decision_memories": [],
            }
            agent.executor.get_evidence = lambda evidence_id, content_hash, timeout=None: (
                self.getter(evidence_id, content_hash))
            finish = FinishAction(claims=[self.claim("chat")],
                                  conflicts=[self.model_conflict()])
            replies = iter([
                json.dumps({"action": "tool_call", "tool": "search_memory",
                            "query": "Atlas 固定分区归属 理由 提交"}),
                finish.model_dump_json(),
            ])
            agent._call_llm = lambda messages: next(replies)
            result = agent.run_result("Atlas 为什么采用固定分区归属？讨论理由与哪个代码提交相互印证？")
        self.assertEqual(result.termination_reason, "conflict")
        self.assertNotEqual(result.termination_reason, "sufficient")
        self.assertEqual(result.claim_counts["claims_accepted"], 1)
        self.assertEqual(result.claim_counts["claims_retained_with_unresolved_conflict"], 1)
        self.assertEqual(result.claims[0].verification_status,
                         "pending_unattributable_conflict")
        body, basis = _answer_body(result.answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn("检测到未裁决冲突", body)
        self.assertNotIn(self.records[CROSS_ATLAS["chat"]].content, body)
        self.assertIn("待核实", result.answer)

    def run_scripted(self, selected_keys, finish, question):
        """Exercise ReActAgent.run_result with deterministic model/tool stand-ins."""
        with tempfile.TemporaryDirectory(prefix="cma-conflict-e2e-") as temporary:
            agent = ReActAgent(scope=EvidenceScope(project_id="bench-atlas", dataset_kind="live"),
                               db_path=Path(temporary) / "evidence.sqlite3")
            agent.api_key = "scripted-model-no-network"
            selected = [self.records[CROSS_ATLAS[key]] for key in selected_keys]
            agent.executor.execute = lambda *args, **kwargs: {
                "tool": "search_memory", "skill": "memory", "query": "q",
                "evidence": [record.model_dump(mode="json") for record in selected],
                "sources": [], "decision_memories": [],
            }
            agent.executor.get_evidence = lambda evidence_id, content_hash, timeout=None: (
                self.getter(evidence_id, content_hash))
            replies = iter([
                json.dumps({"action": "tool_call", "tool": "search_memory", "query": "q"}),
                finish.model_dump_json(),
            ])
            agent._call_llm = lambda messages: next(replies)
            return agent.run_result(question)

    def test_end_to_end_unrelated_conflict_keeps_independent_claim(self):
        result = self.run_scripted(
            ("ack_exactly_once", "ack_at_least_once", "chat"),
            FinishAction(claims=[self.claim("chat")]),
            "Atlas 固定分区归属 B 的讨论理由是什么？")
        self.assertEqual(result.claim_counts["claims_accepted"], 1)
        self.assertEqual(result.claim_counts["claims_discarded_by_conflict"], 0)
        self.assertEqual(result.claims[0].verification_status, "supported_extract")
        structured = [conflict for conflict in result.conflicts
                      if conflict.origin == "structured_assertion"]
        self.assertEqual(structured[0].relation, "independent_of_submitted_claims")
        self.assertNotEqual(result.termination_reason, "conflict")
        body, basis = _answer_body(result.answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn(self.records[CROSS_ATLAS["chat"]].content, body)

    def test_end_to_end_directly_related_conflict_withholds_claim(self):
        result = self.run_scripted(
            ("issue", "proposal_c"),
            FinishAction(claims=[self.claim("issue")], conflicts=[self.model_conflict()]),
            "Atlas 方案是否冲突？")
        self.assertEqual(result.claim_counts["claims_discarded_by_conflict"], 1)
        self.assertEqual(result.claims, [])
        self.assertEqual(result.termination_reason, "conflict")
        self.assertTrue(any(conflict.relation == "affects_submitted_claim"
                            for conflict in result.conflicts))
        body, basis = _answer_body(result.answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn("检测到未裁决冲突", body)
        self.assertIn("直接影响已提交结论", result.answer)

    def test_end_to_end_unattributable_conflict_discloses_pending(self):
        result = self.run_scripted(
            ("chat", "issue", "proposal_c"),
            FinishAction(claims=[self.claim("chat")], conflicts=[self.model_conflict()]),
            "Atlas 固定分区归属 B 的讨论理由是什么？")
        self.assertEqual(result.claim_counts["claims_retained_with_unresolved_conflict"], 1)
        self.assertEqual(result.claims[0].verification_status,
                         "pending_unattributable_conflict")
        self.assertEqual(result.termination_reason, "conflict")
        body, basis = _answer_body(result.answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn("检测到未裁决冲突", body)
        self.assertNotIn(self.records[CROSS_ATLAS["chat"]].content, body)
        self.assertIn("影响范围无法自动判定", result.answer)

    def test_end_to_end_conflict_atlas_retains_both_values(self):
        result = self.run_scripted(
            ("ack_exactly_once", "ack_at_least_once"),
            FinishAction(),
            "文档中的 ack_mode 两份同日记录是否冲突？")
        self.assertEqual(result.termination_reason, "conflict")
        structured = [conflict for conflict in result.conflicts
                      if conflict.origin == "structured_assertion"]
        self.assertEqual(len(structured), 1)
        self.assertEqual(structured[0].fields, ["ack_mode"])
        self.assertIn("ack_mode=exactly_once", result.answer)
        self.assertIn("ack_mode=at_least_once", result.answer)
        self.assertIn("尚未完成对账", result.answer)
        self.assertIn("未裁决冲突", result.answer)
        body, basis = _answer_body(result.answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn("结构化断言", body)
        self.assertIn("冲突相关原文", body)

    def raw(self, answer):
        return {
            "run_id": "fixture", "backend": "react", "mode": "react", "answer": answer,
            "runtime_error": None, "model_calls": 0, "provider_input_tokens": 0,
            "provider_output_tokens": 0, "estimated_tokens": 0,
            "termination_reason": "insufficient", "latency_ms": 1.0,
            "citation_refs": [], "citation_count": 0, "valid_citations": 0,
            "valid_cited_ids": [], "retrieved_ids": [], "decision_memories": [],
            "events": [], "invalid_actions": 0, "duplicate_calls": 0,
            "action_tool_requests": 0, "tool_attempts": 0, "failure_triggered": False,
            "raw_investigation": {"trace": [], "claims": [], "conflicts": [],
                                  "claim_counts": {}},
        }


if __name__ == "__main__":
    unittest.main()
