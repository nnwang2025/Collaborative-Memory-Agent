"""Task-level evaluation isolation, deterministic metrics and online authorization tests."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from benchmark.dataset import load as load_dataset
from benchmark.tasks import grade, run_online
from benchmark.tasks.run_offline import _case
from benchmark.tasks.run_online import _raw_result
from benchmark.tasks.scoring import (CAPABILITIES, aggregate, diagnose, load_tasks, score_task,
                                     validate_tasks)
from src.agents.answer_agent import AnswerAgent


class TaskEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()
        cls.tasks = load_tasks()
        cls.records = {record.evidence_id: record for record in cls.dataset["records"]}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-task-eval-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def task(self, task_id):
        return self.tasks["by_id"][task_id], self.tasks["labels"].labels[task_id]

    def raw(self, answer, task_id, **overrides):
        label = self.tasks["labels"].labels[task_id]
        evidence = label.acceptable_evidence[0]
        value = {
            "run_id": "test-run", "backend": "react", "mode": "react", "answer": answer,
            "runtime_error": None, "model_calls": 0, "provider_input_tokens": 0, "provider_output_tokens": 0,
            "estimated_tokens": 0, "termination_reason": "insufficient", "latency_ms": 1.0,
            "citation_refs": [{"evidence_id": evidence.evidence_id, "content_hash": evidence.content_hash}],
            "citation_count": 1, "valid_citations": 1, "valid_cited_ids": [evidence.evidence_id],
            "retrieved_ids": [evidence.evidence_id], "decision_memories": [], "events": [
                {"tool_name": "search_docs", "query": "q", "filters": {}, "evidence_ids": [evidence.evidence_id],
                 "error": None, "order": 1}],
            "invalid_actions": 0, "duplicate_calls": 0, "action_tool_requests": 1, "tool_attempts": 1,
            "failure_triggered": False, "raw_investigation": {"trace": [], "claims": [], "conflicts": []},
        }
        value.update(overrides)
        return value

    def test_task_set_structure_and_validation(self):
        checked = validate_tasks(self.tasks, self.dataset, self.root / "validation")
        self.assertEqual(checked["task_count"], 16)
        self.assertTrue(all(count == 2 for count in checked["category_counts"].values()))
        self.assertEqual(checked["corpus_snapshot_id"], self.dataset["manifest"]["snapshot_id"])
        self.assertEqual(checked["task_set_status"], "structurally_valid_pending_human_review")

    def test_runtime_case_contains_only_public_fields(self):
        task, _ = self.task("single-atlas")
        case = _case(task, self.dataset["manifest"]["snapshot_id"])
        self.assertEqual(set(case), {"case_id", "category", "question", "project_id", "dataset_kind",
                                     "corpus_snapshot_id", "split"})
        for hidden in ("gold_evidence_ids", "acceptable_evidence", "key_facts", "human_review_rules"):
            self.assertNotIn(hidden, case)

    def test_every_label_statement_satisfies_its_marker_groups(self):
        for task_id, label in self.tasks["labels"].labels.items():
            task = self.tasks["by_id"][task_id]
            answer = "\n".join(fact.statement for fact in label.key_facts)
            row = score_task(self.raw(answer, task_id), task, label, self.records,
                             execution_mode="offline_no_model", supported_capabilities=set())
            self.assertEqual(row["metrics"]["task_fact_coverage"], 1.0, task_id)

    def test_fact_coverage_does_not_create_task_success_or_citation_success(self):
        task, label = self.task("single-atlas")
        answer = "\n".join(fact.statement for fact in label.key_facts)
        row = score_task(self.raw(answer, "single-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        self.assertEqual(row["metrics"]["task_fact_coverage"], 1.0)
        self.assertEqual(row["metrics"]["citation_validity_rate"], 1.0)
        self.assertIsNone(row["task_success"])
        self.assertEqual(row["status"], "ungraded_offline_adapter")
        summary = aggregate([row])
        self.assertIsNone(summary["overall"]["TaskSuccessRate"]["value"])
        self.assertEqual(summary["overall"]["TaskSuccessRate"]["denominator"], 0)
        self.assertEqual(summary["grading"]["task_success_status"], "manual_only_no_claim_without_human_grades")

    def test_explicit_human_grade_is_required_for_task_success(self):
        task, label = self.task("single-atlas")
        answer = "\n".join(fact.statement for fact in label.key_facts)
        raw = self.raw(answer, "single-atlas")
        with self.assertRaises(ValueError):
            score_task(raw, task, label, self.records, execution_mode="online_model",
                       supported_capabilities=CAPABILITIES, manual_grade={"task_success": True})
        row = score_task(raw, task, label, self.records, execution_mode="online_model",
                         supported_capabilities=CAPABILITIES,
                         manual_grade={"reviewer": "reviewer-1", "task_success": True, "notes": "checked"})
        self.assertEqual(row["status"], "graded")
        self.assertTrue(row["task_success"])
        summary = aggregate([row])
        self.assertEqual(summary["overall"]["TaskSuccessRate"]["value"], 1.0)

    def test_offline_capability_task_is_unsupported_but_diagnostic_metrics_remain(self):
        task, label = self.task("decision-atlas")
        row = score_task(self.raw("C 是当前确认版本。", "decision-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        self.assertEqual(row["status"], "unsupported_offline")
        self.assertEqual(row["unsupported_capabilities"], ["time_filters"])
        self.assertFalse(row["metrics"]["decision_checks"]["known_at_applied"])
        self.assertIsNone(row["task_success"])

    def test_conflict_disclosure_requires_both_values_without_choosing(self):
        task, label = self.task("conflict-atlas")
        row = score_task(self.raw("ack_mode=exactly_once。", "conflict-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        self.assertFalse(row["metrics"]["conflict_checks"]["both_values_disclosed"])
        complete = score_task(self.raw("exactly_once 与 at_least_once 尚未对账。", "conflict-atlas"),
                              task, label, self.records, execution_mode="offline_no_model",
                              supported_capabilities=set())
        self.assertTrue(complete["metrics"]["conflict_checks"]["both_values_disclosed"])

    def test_abstention_rate_is_manual_only(self):
        task, label = self.task("missing-atlas")
        answer = "\n".join(fact.statement for fact in label.key_facts)
        row = score_task(self.raw(answer, "missing-atlas", termination_reason="insufficient"),
                         task, label, self.records, execution_mode="online_model",
                         supported_capabilities=CAPABILITIES)
        self.assertIsNone(row["human_appropriate_abstention"])
        self.assertIsNone(aggregate([row])["overall"]["AppropriateAbstentionRate"]["value"])
        graded = score_task(self.raw(answer, "missing-atlas", termination_reason="insufficient"),
                            task, label, self.records, execution_mode="online_model",
                            supported_capabilities=CAPABILITIES,
                            manual_grade={"reviewer": "reviewer-1", "task_success": True,
                                          "appropriate_abstention": True})
        self.assertEqual(aggregate([graded])["overall"]["AppropriateAbstentionRate"]["value"], 1.0)

    def test_online_runner_defaults_to_pending_without_output_or_model(self):
        result = run_online.run()
        self.assertEqual(result["status"], "pending_authorization")
        self.assertFalse(result["online_executed"])
        self.assertEqual(result["task_count"], 16)
        self.assertEqual(result["max_normal_model_calls"], 80)
        self.assertEqual(result["max_truncation_recovery_calls"], 16)
        self.assertEqual(result["max_model_calls"], 96)
        self.assertEqual(result["max_tool_calls"], 128)

    def test_online_runner_rejects_wrong_origin_before_any_request(self):
        output = self.root / "online"
        with patch.dict(os.environ, {"SILICONFLOW_BASE_URL": "https://api.example.test/v1"}, clear=False):
            with self.assertRaises(ValueError):
                run_online.run(online=True, approved_origin="https://evil.example", output=output)
        self.assertFalse(output.exists())

    def test_online_runner_rejects_existing_output_before_any_request(self):
        output = self.root / "existing"
        output.mkdir()
        with patch.dict(os.environ, {"SILICONFLOW_BASE_URL": "https://api.example.test/v1"}, clear=False):
            with self.assertRaises(FileExistsError):
                run_online.run(online=True, approved_origin="https://api.example.test", output=output)

    def test_online_runner_without_credential_environment_does_not_create_report(self):
        output = self.root / "no-credentials"
        with patch.dict(os.environ, {"SILICONFLOW_BASE_URL": "https://api.example.test/v1"}, clear=True):
            result = run_online.run(online=True, approved_origin="https://api.example.test", output=output)
        self.assertEqual(result["status"], "not_executed_no_credentials")
        self.assertFalse(result["online_executed"])
        self.assertFalse(output.exists())

    def test_grade_flow_requires_explicit_values_and_new_output(self):
        task, label = self.task("single-atlas")
        answer = "\n".join(fact.statement for fact in label.key_facts)
        row = score_task(self.raw(answer, "single-atlas"), task, label, self.records,
                         execution_mode="online_model", supported_capabilities=CAPABILITIES)
        report = self.root / "report"
        report.mkdir()
        (report / "results.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        (report / "summary.json").write_text(json.dumps({"config": {}, "warnings": [],
                                                         "metrics": aggregate([row])}), encoding="utf-8")
        grades = self.root / "grades.json"
        grades.write_text(json.dumps({"grades": [{"task_id": "single-atlas", "backend": "react",
                                                   "reviewer": "reviewer-1", "task_success": True}]}),
                          encoding="utf-8")
        with self.assertRaises(ValueError):
            grade.apply_grade(row, label, {"reviewer": "reviewer-1"})
        output, summary = grade.run(report, grades, self.root / "graded")
        self.assertEqual(summary["metrics"]["overall"]["TaskSuccessRate"]["value"], 1.0)
        self.assertEqual(summary["status"], "graded_complete")
        self.assertTrue((output / "results.jsonl").exists())
        with self.assertRaises(FileExistsError):
            grade.run(report, grades, output)

    def test_offline_fallback_in_online_run_cannot_be_graded_as_real_model(self):
        task, label = self.task("single-atlas")
        row = score_task(self.raw("离线结果", "single-atlas", mode="offline_fallback"),
                         task, label, self.records, execution_mode="online_model",
                         supported_capabilities=CAPABILITIES)
        self.assertEqual(row["status"], "offline_fallback_not_real_model")
        self.assertIsNone(row["task_success"])
        with self.assertRaises(ValueError):
            grade.apply_grade(row, label, {"reviewer": "reviewer-1", "task_success": True})

    def test_grade_rejects_unsupported_offline_rows(self):
        task, label = self.task("decision-atlas")
        row = score_task(self.raw("C 是当前确认版本。", "decision-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        with self.assertRaises(ValueError):
            grade.apply_grade(row, label, {"reviewer": "reviewer-1", "task_success": True})

    def test_history_check_only_applies_to_tasks_that_declare_it(self):
        history_task, history_label = self.task("decision-zephyr")
        self.assertTrue(history_label.expected_temporal.requires_history)
        row = score_task(self.raw("B 到 C 的确认版本关系见记录。", "decision-zephyr"),
                         history_task, history_label, self.records, execution_mode="offline_no_model",
                         supported_capabilities=set())
        self.assertIs(row["metrics"]["decision_checks"]["history_view_applied"], False)
        supersedes_task, supersedes_label = self.task("missing-atlas")
        self.assertFalse(supersedes_label.expected_temporal.requires_history)
        row = score_task(self.raw("当前确认版本不等同于生产执行。", "missing-atlas"),
                         supersedes_task, supersedes_label, self.records, execution_mode="offline_no_model",
                         supported_capabilities=set())
        self.assertIsNone(row["metrics"]["decision_checks"]["history_view_applied"])
        self.assertNotIn("history_view_not_requested", diagnose(row)["hypotheses"])

    def test_online_raw_result_counts_delivered_evidence_not_only_citations(self):
        cited = SimpleNamespace(evidence_id="ev_a", content_hash="a" * 64,
                                model_dump=lambda: {"evidence_id": "ev_a", "content_hash": "a" * 64})
        step = SimpleNamespace(action="tool_attempt", tool_name="search_docs", query="q", filters={},
                               evidence_ids=["ev_a", "ev_b"], errors=[], elapsed_ms=1.0,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))
        result = SimpleNamespace(run_id="r", mode="react", answer="answer", termination_reason="sufficient",
                                 termination_detail="finish_validated",
                                 usage=SimpleNamespace(input_tokens=10, output_tokens=5, estimated=False),
                                 trace=[step], evidence_refs=[cited], decision_memories=[],
                                 model_dump=lambda mode="json": {})
        raw = _raw_result(result, SimpleNamespace(), SimpleNamespace(get=lambda *args: object()),
                          "2026-11-01T00:00:00+00:00", 1.0)
        self.assertEqual(raw["retrieved_ids"], ["ev_a", "ev_b"])
        self.assertEqual(raw["citation_count"], 1)
        self.assertEqual(raw["termination_detail"], "finish_validated")

    @staticmethod
    def memory(version_id, statement, *, known_at, cutoff, current=True, decision_id="dec_test"):
        return {"version_id": version_id, "status": "confirmed", "statement": statement,
                "event_time": "2025-01-01T00:00:00+00:00", "decision_known_at": known_at,
                "recorded_at": known_at, "knowledge_cutoff": cutoff,
                "system_current": current, "execution_status": "unknown",
                "world_state": "unknown", "decision_id": decision_id}

    def decision_raw(self, task_id, label, memories, answer="截至 2025-02-20 的系统记录如下。"):
        cutoff = label.expected_temporal.known_at
        return self.raw(answer, task_id, decision_memories=memories, events=[{
            "tool_name": "search_memory", "query": "q",
            "filters": {"decision_id": label.decision_id, "known_at": cutoff,
                        "memory_view": "current", "before": None, "after": None},
            "evidence_ids": [], "error": None, "order": 1}])

    def test_temporal_snapshot_checks_use_only_the_matching_cutoff_view(self):
        task, label = self.task("decision-atlas")
        cutoff = label.expected_temporal.known_at
        expected = label.expected_temporal.expected_current_version_id
        historical = self.memory(expected, "历史方案 B 已确认", known_at="2023-01-10T00:00:00+00:00",
                                 cutoff=cutoff, decision_id=label.decision_id)
        current = self.memory("dv_later_current", "后续方案 C 已确认",
                              known_at="2025-03-20T00:00:00+00:00", cutoff=None,
                              decision_id=label.decision_id)
        row = score_task(self.decision_raw("decision-atlas", label, [current, historical]),
                         task, label, self.records, execution_mode="offline_no_model",
                         supported_capabilities=set())
        checks = row["metrics"]["decision_checks"]
        self.assertTrue(checks["known_at_applied"])
        self.assertTrue(checks["current_version_correct"])
        self.assertTrue(checks["snapshot_respects_cutoff"])
        self.assertTrue(checks["historical_versions_present"])
        self.assertNotIn("time_snapshot_inconsistent", diagnose(row)["hypotheses"])

    def test_cutoff_synthesis_reaches_fact_coverage_without_scanning_appendix(self):
        task, label = self.task("decision-atlas")
        cutoff = label.expected_temporal.known_at
        historical = self.memory(label.expected_temporal.expected_current_version_id,
                                 "采用固定分区归属 B", known_at="2023-01-10T00:00:00+00:00",
                                 cutoff=cutoff, decision_id=label.decision_id)
        current = self.memory("dv_later_current", "采用带 fencing token 的租约迁移 C",
                              known_at="2025-03-20T00:00:00+00:00", cutoff=None,
                              decision_id=label.decision_id)
        agent = AnswerAgent()
        synthesis = agent.build_synthesis(
            SimpleNamespace(entries=[]), [], [], [], "insufficient", task.question,
            decision_memories=[historical, current])
        answer = agent.render_synthesis(synthesis)
        row = score_task(self.decision_raw("decision-atlas", label, [historical, current], answer=answer),
                         task, label, self.records, execution_mode="offline_no_model",
                         supported_capabilities=set())
        self.assertEqual(row["metrics"]["task_fact_coverage"], 1.0)
        self.assertEqual(row["metrics"]["task_fact_coverage_status"], "rendered_synthesis")
        self.assertIsNone(row["task_success"])

    def test_missing_cutoff_view_is_not_hidden_by_current_view(self):
        task, label = self.task("decision-atlas")
        current = self.memory("dv_later_current", "后续方案 C 已确认",
                              known_at="2025-03-20T00:00:00+00:00", cutoff=None,
                              decision_id=label.decision_id)
        row = score_task(self.decision_raw("decision-atlas", label, [current]),
                         task, label, self.records, execution_mode="offline_no_model",
                         supported_capabilities=set())
        checks = row["metrics"]["decision_checks"]
        self.assertFalse(checks["current_version_correct"])
        self.assertFalse(checks["snapshot_respects_cutoff"])

    def test_synthesis_non_bullet_text_is_answer_body(self):
        from benchmark.tasks.scoring import _answer_body
        answer = ("【综合回答】\n本轮没有通过原文摘录与引用校验的结论。\n"
                  "未确认部分：截止时点的确认版本仍不清楚。\n"
                  "说明：这段说明不是结论。\n")
        body, basis = _answer_body(answer)
        self.assertEqual(basis, "rendered_synthesis")
        self.assertIn("截止时点的确认版本仍不清楚", body)
        self.assertNotIn("未确认部分", body)
        self.assertNotIn("说明：", body)
        self.assertNotIn("本轮没有通过", body)

    def test_unconfirmed_section_title_alone_does_not_match_fact_markers(self):
        task, label = self.task("decision-atlas")
        answer = ("【综合回答】\n"
                  "未确认部分：C 的租约迁移和 fencing token 已确认。\n")
        row = score_task(self.raw(answer, "decision-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        facts = {fact["fact_id"]: fact["matched"] for fact in row["metrics"]["facts"]}
        self.assertFalse(facts["c_pending_at_cutoff"])
        self.assertLess(row["metrics"]["task_fact_coverage"], 1.0)

    def test_unconfirmed_body_conclusion_is_retained_for_fact_matching(self):
        from benchmark.tasks.scoring import _answer_body
        task, label = self.task("decision-atlas")
        answer = ("【综合回答】\n"
                  "未确认部分：C 仍是候选方案，租约迁移尚未确认。"
                  "（完整见【未解决的缺口】，1 项）。\n")
        body, _ = _answer_body(answer)
        self.assertIn("C 仍是候选方案", body)
        self.assertNotIn("完整见", body)
        self.assertNotIn("未确认部分", body)
        row = score_task(self.raw(answer, "decision-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        facts = {fact["fact_id"]: fact["matched"] for fact in row["metrics"]["facts"]}
        self.assertTrue(facts["c_pending_at_cutoff"])

    def test_appendix_keywords_do_not_create_fact_coverage(self):
        task, label = self.task("decision-atlas")
        statement = "；".join(fact.statement for fact in label.key_facts)
        answer = "\n".join([
            "【综合回答】", "本轮没有通过原文摘录与引用校验的结论；不能确认该问题的答案。",
            "【证据】", f"- {statement}", "【未解决的缺口】", f"- {statement}",
        ])
        row = score_task(self.raw(answer, "decision-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        self.assertIsNone(row["metrics"]["task_fact_coverage"])
        self.assertEqual(row["metrics"]["task_fact_coverage_status"], "unverified_no_answer_body")

    def test_question_echo_and_caveat_keywords_do_not_create_fact_coverage(self):
        task, label = self.task("decision-atlas")
        statement = "；".join(fact.statement for fact in label.key_facts)
        answer = "\n".join([
            "【综合回答】", f"针对问题“{statement}”，本轮可确认的内容如下：",
            f"说明：{statement}",
        ])
        row = score_task(self.raw(answer, "decision-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        self.assertEqual(row["metrics"]["task_fact_coverage_status"], "unverified_no_answer_body")

    def test_keyword_hits_never_award_task_success(self):
        task, label = self.task("decision-atlas")
        answer = ("【综合回答】\n- 截至 2025-02-20，方案 C 已替代方案 B，"
                  "固定分区归属已确认。\n")
        row = score_task(self.raw(answer, "decision-atlas"), task, label, self.records,
                         execution_mode="offline_no_model", supported_capabilities=set())
        self.assertIsNone(row["task_success"])
        self.assertIn(row["status"], {"ungraded_offline_adapter", "unsupported_offline"})
        self.assertEqual(row["metrics"]["task_fact_coverage_status"], "rendered_synthesis")
        self.assertLess(row["metrics"]["task_fact_coverage"], 1.0)

    def test_termination_detail_is_persisted_and_old_rows_stay_compatible(self):
        task, label = self.task("single-atlas")
        row = score_task(self.raw("答案", "single-atlas", termination_detail="output_budget_retry"),
                         task, label, self.records, execution_mode="offline_no_model",
                         supported_capabilities=set())
        self.assertEqual(row["termination_detail"], "output_budget_retry")
        legacy = score_task(self.raw("答案", "single-atlas"), task, label, self.records,
                            execution_mode="offline_no_model", supported_capabilities=set())
        self.assertIsNone(legacy["termination_detail"])

    def test_task_set_version_and_hash_are_recorded(self):
        checked = validate_tasks(self.tasks, self.dataset, self.root / "validation")
        self.assertEqual(checked["task_set_version"], "cma-task-eval-v1")
        self.assertEqual(len(checked["task_set_sha256"]), 64)
        self.assertEqual(checked["scoring_isolation"], "labels_joined_after_run_only")


if __name__ == "__main__":
    unittest.main()
