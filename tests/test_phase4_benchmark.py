"""Phase 4 benchmark contract: frozen snapshot, scoring, offline runtime and online safety."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from benchmark.v4 import (ablations, acceptance, build, dataset, grade, indexing,
                          run_offline, run_online)
from benchmark.v4 import scoring
from benchmark.v4.dataset import SnapshotError
from benchmark.v4.indexing import IndexPreparationError
from benchmark.v4.runtime import (ModelCallStop, ModelHttpGuard,
                                  _model_call_count, run_task)
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.index.retriever import LiveRetriever
from src.investigation import Budget, TraceStep


class _FakeEmbedder:
    """Deterministic local embedder for tests; no model download or network."""

    model_name = "test-hash-embedder"
    _model_name = "test-hash-embedder"

    @staticmethod
    def encode(texts, normalize_embeddings=True):
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = [(byte / 255.0) - 0.5 for byte in digest[:16]]
            norm = sum(value * value for value in vector) ** 0.5 or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


def _public_task(snapshot, category="single_source"):
    return next(task for task in snapshot.public_tasks if task["category"] == category)


def _base_label(project_id="v4-atlas-observability"):
    return {
        "task_id": "test-task", "category": "single_source",
        "task_set_version": "test-tasks", "project_id": project_id,
        "dataset_kind": "live", "known_at": None,
        "required_evidence_groups": [], "acceptable_alternative_evidence": [],
        "expected_facts": [], "expected_conflicts": [],
        "expected_decision_state": None,
        "expected_abstention": {"required": False, "expected_termination": ["sufficient"],
                                "must_disclose": []},
        "expected_limitations": [], "difficulty": "easy",
        "scoring_rubric": {"deterministic": [], "manual": ["task_success"]},
        "annotation_status": "synthetic_authored_pending_independent_human_review",
    }


def _base_raw(**overrides):
    raw = {
        "task_id": "test-task", "backend": "react", "answer": "",
        "retrieved_ids": [], "citation_refs": [], "claim_source_checks": [],
        "decision_memories": [], "events": [], "termination_reason": "sufficient",
        "termination_detail": None, "runtime_error": None, "model_calls": 0,
        "tool_calls": 0, "latency_ms": 1.0, "estimated_tokens": 0,
        "provider_input_tokens": 0, "provider_output_tokens": 0,
    }
    raw.update(overrides)
    return raw


def _task(task_id="test-task", project_id="v4-atlas-observability"):
    return {"task_id": task_id, "category": "single_source", "project_id": project_id,
            "dataset_kind": "live", "difficulty": "easy"}


def _score(label, raw, db):
    return scoring.score_run(raw, _task(label["task_id"], label["project_id"]), label,
                             evidence_db=db)


class SnapshotTests(unittest.TestCase):
    def test_build_is_deterministic_and_frozen(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one = build.build(Path(first) / "snapshot")
            two = build.build(Path(second) / "snapshot")
        self.assertEqual(one, two)
        self.assertEqual(one["snapshot_id"], two["snapshot_id"])
        self.assertGreaterEqual(one["record_count"], 300)
        self.assertLessEqual(one["record_count"], 500)
        self.assertGreaterEqual(len(one["files"]), 4)
        self.assertEqual(one["annotation_status"],
                         "synthetic_authored_pending_independent_human_review")

    def test_manifest_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "dataset"
            shutil.copytree(dataset.DEFAULT_DATASET, copy)
            records = copy / "records.json"
            records.write_bytes(records.read_bytes() + b" ")
            with self.assertRaises(SnapshotError):
                dataset.load(copy)

    def test_task_set_and_public_labels(self):
        snapshot = dataset.load()
        self.assertGreaterEqual(len(snapshot.public_tasks), 40)
        self.assertLessEqual(len(snapshot.public_tasks), 60)
        counts = {}
        for task in snapshot.public_tasks:
            self.assertLessEqual(set(task), dataset.PUBLIC_FIELDS)
            self.assertFalse(set(task) & dataset.FORBIDDEN_PUBLIC_FIELDS)
            self.assertEqual(task["dataset_kind"], "live")
            self.assertEqual(task["task_set_version"], snapshot.task_set_version)
            counts[task["category"]] = counts.get(task["category"], 0) + 1
        self.assertEqual(set(counts), set(dataset.CATEGORIES))
        for category, count in counts.items():
            self.assertGreaterEqual(count, 5, category)

    def test_gold_label_contract(self):
        snapshot = dataset.load()
        alternative_tasks = 0
        for task in snapshot.public_tasks:
            label = snapshot.label(task["task_id"])
            self.assertEqual(label["task_id"], task["task_id"])
            self.assertEqual(label["task_set_version"], snapshot.task_set_version)
            self.assertTrue(label["scoring_rubric"]["manual"])
            if task["category"] in {"decision_history", "conflict", "insufficient",
                                    "budget", "tool_failure"}:
                self.assertTrue(label["forbidden_inferences"])
            self.assertTrue(any(fact["required"] for fact in label["expected_facts"]))
            self.assertTrue(any(not fact["required"] for fact in label["expected_facts"]))
            self.assertIsInstance(label["acceptable_alternative_evidence"], list)
            for group in label["required_evidence_groups"]:
                if len(group["any_of"]) > 1:
                    alternative_tasks += 1
                    self.assertTrue(label["acceptable_alternative_evidence"])
            if label.get("known_at"):
                self.assertIsNotNone(datetime.fromisoformat(label["known_at"]).tzinfo)
            if task["category"] == "multi_turn":
                self.assertTrue(label.get("turns"))
                self.assertEqual(len(label["turns"]), 2)
                self.assertTrue(all(turn["evaluation_target"] for turn in label["turns"]))
            else:
                self.assertFalse(label.get("turns"))
            if task["category"] == "tool_failure":
                self.assertIn("skill", label["injected_failure"])
            if task["category"] == "budget":
                self.assertGreaterEqual(label["budget_override"]["max_tokens"], 1)
            if task["category"] == "insufficient":
                self.assertTrue(label["expected_abstention"]["required"])
                self.assertTrue(label["expected_abstention"]["must_disclose"])
            if task["category"] == "conflict":
                self.assertTrue(label["expected_conflicts"])
            if task["category"] == "decision_history":
                self.assertTrue(label["expected_decision_state"]["known_at"])
        self.assertGreaterEqual(alternative_tasks, 5)

    def test_gold_evidence_refs_are_current(self):
        snapshot = dataset.load()
        with tempfile.TemporaryDirectory() as temporary:
            evidence_db, _ = dataset.materialize_template(snapshot, temporary)
            for task in snapshot.public_tasks:
                label = snapshot.label(task["task_id"])
                scope = EvidenceScope(project_id=task["project_id"],
                                      dataset_kind=task["dataset_kind"])
                store = EvidenceStore(str(evidence_db))
                refs = [ref for group in label["required_evidence_groups"]
                        for ref in group["any_of"]]
                for alternative in label["acceptable_alternative_evidence"]:
                    refs.extend(alternative["any_of"])
                for conflict in label["expected_conflicts"]:
                    refs.extend(conflict.get("evidence_refs", []))
                for ref in refs:
                    record = store.get(ref["evidence_id"], scope, ref["content_hash"])
                    self.assertIsNotNone(record, f"{task['task_id']}: {ref['evidence_id']}")
                    state = store.current_state(ref["evidence_id"], scope, ref["content_hash"])
                    self.assertEqual(state["state"], "active")
                    self.assertTrue(state["matches"])


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "missing.db"

    def test_alternative_evidence_group_and_structured_facts(self):
        label = _base_label()
        label["required_evidence_groups"] = [
            {"group_id": "g", "any_of": [{"evidence_id": "ev_a", "content_hash": "a"},
                                         {"evidence_id": "ev_b", "content_hash": "b"}]}]
        label["expected_facts"] = [
            {"fact_id": "date", "fact_type": "date", "required": True,
             "values": ["2025-01-18"]},
            {"fact_id": "person", "fact_type": "person", "required": True,
             "values": ["alice"]},
            {"fact_id": "issue", "fact_type": "issue_id", "required": True,
             "values": ["ISS-42"]},
            {"fact_id": "root", "fact_type": "text", "required": True,
             "marker_groups": [["批次过小", "批次过小导致固定开销"]]},
            {"fact_id": "optional", "fact_type": "text", "required": False,
             "values": ["可选的补充说明"]},
        ]
        label["forbidden_inferences"] = [["已正式批准", "已经批准"]]
        raw = _base_raw(retrieved_ids=["ev_b"],
                        answer="2025-01-18 alice 确认 ISS-42：批次过小导致固定开销较高。")
        row = _score(label, raw, self.db)
        self.assertEqual(row["metrics"]["evidence_recall_at_k"], 1.0)
        self.assertEqual(row["metrics"]["fact_accuracy"], 1.0)
        self.assertIsNone(row["metrics"]["optional_fact_accuracy"])
        self.assertFalse(row["metrics"]["forbidden_inference_detected"])
        self.assertIn("root", [fact["fact_id"] for fact in row["details"]["facts"]])

        forbidden = _score(
            label,
            _base_raw(retrieved_ids=["ev_b"],
                      answer="批次过小导致固定开销较高，已正式批准。"), self.db)
        self.assertTrue(forbidden["metrics"]["forbidden_inference_detected"])
        self.assertIn("forbidden_inference", forbidden["failure_reasons"])

    def test_citation_validity_requires_current_hash_and_scope(self):
        snapshot = dataset.load()
        task = _public_task(snapshot)
        label = snapshot.label(task["task_id"])
        group = label["required_evidence_groups"][0]
        ref = group["any_of"][0]
        scope = EvidenceScope(project_id=task["project_id"], dataset_kind="live")
        with tempfile.TemporaryDirectory() as temporary:
            evidence_db, _ = dataset.materialize_template(snapshot, temporary)
            store = EvidenceStore(str(evidence_db))
            record = store.get(ref["evidence_id"], scope, ref["content_hash"])
            raw = _base_raw(
                retrieved_ids=[ref["evidence_id"]],
                citation_refs=[{"evidence_id": ref["evidence_id"],
                                "content_hash": ref["content_hash"]}],
                claim_source_checks=[{"all_refs_resolve": True,
                                      "source_text_exact_in_cited_evidence": True}])
            row = _score(label, raw, evidence_db)
            self.assertEqual(row["metrics"]["citation_validity"], 1.0)
            self.assertEqual(row["details"]["citations"]["claim_source_exactness"], 1.0)

            forged = dict(raw)
            forged["citation_refs"] = [{"evidence_id": ref["evidence_id"],
                                        "content_hash": "0" * 64}]
            bad = _score(label, forged, evidence_db)
            self.assertEqual(bad["metrics"]["citation_validity"], 0.0)
            self.assertIn("invalid_citations", bad["failure_reasons"])
            self.assertTrue(record.content)

    def test_abstention_false_refusal_and_unsupported_answer(self):
        required = _base_label()
        required["expected_abstention"] = {
            "required": True, "expected_termination": ["insufficient"],
            "must_disclose": [["未确认", "待审核"]]}
        raw = _base_raw(answer="当前未确认，只有待审核提案。", termination_reason="insufficient")
        row = _score(required, raw, self.db)
        self.assertTrue(row["metrics"]["appropriate_abstention_proxy"])

        confirmed = _base_raw(answer="已确认并正式批准。", termination_reason="insufficient")
        bad = _score(required, confirmed, self.db)
        self.assertFalse(bad["metrics"]["appropriate_abstention_proxy"])
        self.assertTrue(bad["metrics"]["unsupported_answer_signal"])

        normal = _base_label()
        normal["required_evidence_groups"] = [
            {"group_id": "g", "any_of": [{"evidence_id": "ev_a", "content_hash": "a"}]}]
        refusal = _base_raw(answer="证据不足，无法确认。", termination_reason="insufficient",
                            retrieved_ids=["ev_a"])
        refused = _score(normal, refusal, self.db)
        self.assertTrue(refused["metrics"]["false_refusal_signal"])

    def test_expected_termination_matches_all_supported_states(self):
        cases = [
            ("sufficient", "sufficient", True),
            ("insufficient", "insufficient", True),
            ("conflict", "conflict", True),
            ("budget", "budget", True),
            ("sufficient", "insufficient", False),
            ("insufficient", "sufficient", False),
            ("conflict", "budget", False),
            ("budget", "conflict", False),
        ]
        for expected, actual, should_match in cases:
            with self.subTest(expected=expected, actual=actual):
                label = _base_label()
                label["expected_abstention"]["expected_termination"] = [expected]
                row = _score(label, _base_raw(termination_reason=actual), self.db)
                self.assertEqual(row["metrics"]["termination_matches_expected"], should_match)

    def test_expected_termination_prefers_nested_and_supports_top_level_fallback(self):
        legacy = _base_label()
        legacy.pop("expected_abstention")
        legacy["expected_termination"] = ["budget"]
        fallback = _score(legacy, _base_raw(termination_reason="budget"), self.db)
        self.assertTrue(fallback["metrics"]["termination_matches_expected"])

        nested = _base_label()
        nested["expected_abstention"]["expected_termination"] = ["conflict"]
        nested["expected_termination"] = ["budget"]
        nested_match = _score(nested, _base_raw(termination_reason="conflict"), self.db)
        nested_mismatch = _score(nested, _base_raw(termination_reason="budget"), self.db)
        self.assertTrue(nested_match["metrics"]["termination_matches_expected"])
        self.assertFalse(nested_mismatch["metrics"]["termination_matches_expected"])

    def test_real_gold_termination_is_read_from_abstention_block(self):
        snapshot = dataset.load()
        label = snapshot.label("v4-single-source-03")
        self.assertEqual(label["expected_abstention"]["expected_termination"], ["sufficient"])
        self.assertIsNone(label.get("expected_termination"))
        row = scoring.score_run(
            _base_raw(termination_reason="insufficient"),
            _task(label["task_id"], label["project_id"]),
            label,
            evidence_db=self.db,
        )
        self.assertFalse(row["metrics"]["termination_matches_expected"])

    def test_conflict_detection_requires_both_values(self):
        label = _base_label()
        label["expected_conflicts"] = [{"field": "canary_percent", "values": ["10", "5"],
                                        "must_disclose_both": True}]
        both = _score(label, _base_raw(answer="讨论中 10 与 5 存在冲突，仍需保留双方记录。"),
                      self.db)
        self.assertTrue(both["metrics"]["conflict_detection"])
        one = _score(label, _base_raw(answer="当前值是 10。"), self.db)
        self.assertFalse(one["metrics"]["conflict_detection"])
        self.assertIn("expected_conflict_not_disclosed", one["failure_reasons"])

    def test_decision_state_uses_current_snapshot_only(self):
        label = _base_label()
        label["expected_decision_state"] = {
            "decision_id": "dec_1", "known_at": None,
            "expected_current_version_id": "dv_current", "expected_version_ids": ["dv_current"]}
        good = _base_raw(
            answer="当前确认版本是 dv_current。",
            decision_memories=[
                {"decision_id": "dec_1", "version_id": "dv_current", "system_current": True},
                {"decision_id": "dec_unrelated", "version_id": "dv_other",
                 "system_current": True}],
            events=[{"tool_name": "search_memory", "error": None}])
        row = _score(label, good, self.db)
        self.assertTrue(row["metrics"]["decision_state_correct"])

        bad = _base_raw(
            answer="提案已正式确认。",
            decision_memories=[{"decision_id": "dec_1", "version_id": "dv_old",
                                "system_current": True}],
            events=[{"tool_name": "search_memory", "error": None}])
        wrong = _score(label, bad, self.db)
        self.assertFalse(wrong["metrics"]["decision_state_correct"])
        self.assertIn("decision_state_mismatch", wrong["failure_reasons"])

    def test_tool_failure_recovery_uses_trace_order(self):
        label = _base_label()
        label["injected_failure"] = {"skill": "problem", "error": "timeout"}
        recovered_events = [
            {"order": 1, "error": "injected_timeout", "evidence_ids": []},
            {"order": 2, "error": None, "evidence_ids": ["ev_alt"]},
        ]
        label["required_evidence_groups"] = [
            {"group_id": "g", "any_of": [{"evidence_id": "ev_alt", "content_hash": "a"}]}]
        raw = _base_raw(events=recovered_events, retrieved_ids=["ev_alt"])
        row = _score(label, raw, self.db)
        self.assertTrue(row["metrics"]["tool_failure_recovered"])

        lost = _score(label, _base_raw(events=recovered_events[:1]), self.db)
        self.assertFalse(lost["metrics"]["tool_failure_recovered"])
        self.assertIn("tool_failure_not_recovered", lost["failure_reasons"])

    def test_task_success_stays_null_until_manual_grades(self):
        label = _base_label()
        row = _score(label, _base_raw(), self.db)
        self.assertIsNone(row["task_success"])
        self.assertTrue(row["needs_manual_review"])
        aggregate = scoring.aggregate([row])
        self.assertIsNone(aggregate["TaskSuccessRate"])
        self.assertEqual(aggregate["TaskSuccessRate_status"], "pending_human_review")

        graded = scoring.apply_manual_grades([row], [{
            "task_id": "test-task", "backend": "react", "reviewer": "reviewer-a",
            "task_success": True, "notes": "checked"}])
        complete = scoring.aggregate(graded)
        self.assertEqual(complete["TaskSuccessRate"], 1.0)
        self.assertEqual(complete["TaskSuccessRate_status"], "human_graded")

        with self.assertRaises(ValueError):
            scoring.apply_manual_grades([row], [{
                "task_id": "test-task", "backend": "react", "reviewer": "reviewer-a",
                "task_success": "yes"}])


class OfflineRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.snapshot = dataset.load()
        cls.output = Path(cls.temp.name) / "offline"
        with patch("urllib.request.urlopen",
                   side_effect=AssertionError("outer network must not be called")) as spy:
            _, cls.report = run_offline.run(
                cls.output, task_ids=["v4-single-source-01"],
                backends=["workflow", "react"])
        cls.spy = spy

    def test_real_chains_run_without_network_or_model(self):
        self.assertEqual(self.spy.call_count, 0)
        rows = [json.loads(line) for line in
                (self.output / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        by_backend = {row["backend"]: row for row in rows}
        self.assertEqual(by_backend["react"]["raw"]["mode"], "offline_fallback")
        self.assertEqual(by_backend["react"]["raw"]["model_calls"], 0)
        self.assertIsNone(by_backend["react"]["raw"]["runtime_error"])
        self.assertEqual(by_backend["workflow"]["raw"]["termination_reason"],
                         "not_reported_by_workflow")
        self.assertIsNone(self.report["aggregates"]["overall"]["TaskSuccessRate"])
        self.assertFalse(self.report["online_executed"])
        self.assertIsNone(self.report["config"]["model"])
        self.assertEqual(self.report["config"]["execution_mode"], "offline_no_model")
        self.assertEqual(self.report["pending_grading"]["pending"], 2)
        for name in ("summary.json", "results.jsonl", "config.json", "failure_cases.json",
                     "pending_grading.json", "report.md"):
            self.assertTrue((self.output / name).exists(), name)

    def test_existing_output_is_never_overwritten(self):
        with self.assertRaises(FileExistsError):
            run_offline.run(self.output, task_ids=["v4-single-source-01"])

    def test_plan_only_creates_no_report(self):
        target = Path(self.temp.name) / "plan-only"
        _, plan = run_offline.run(target, task_ids=["v4-single-source-01"], plan_only=True)
        self.assertFalse(target.exists())
        self.assertEqual(plan["execution_mode"], "plan_only")
        self.assertEqual(plan["snapshot"]["snapshot_id"], self.snapshot.snapshot_id)

    def test_context_ablation_uses_same_tasks(self):
        target = Path(self.temp.name) / "phase2-context"
        with patch("urllib.request.urlopen",
                   side_effect=AssertionError("no network in context ablation")):
            _, report = run_offline.run(
                target, task_ids=["v4-single-source-01"], backends=["react"],
                context_mode="phase2_simulated")
        self.assertEqual(report["config"]["context_mode"], "phase2_simulated")
        self.assertIn("simulated", report["config"]["context_strategy"])
        row = json.loads((target / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertFalse(row["raw"]["model_calls"])

    def test_budget_and_tool_failure_fixtures_are_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            databases = dataset.materialize_template(self.snapshot, temporary)
            budget_task = self.snapshot.task("v4-budget-01")
            budget_label = self.snapshot.label("v4-budget-01")
            result = run_task(self.snapshot, budget_task, "react", databases,
                              budget_override=budget_label["budget_override"])
            self.assertEqual(result["termination_reason"], "budget")
            self.assertLessEqual(result["tool_calls"], 1)
            self.assertIsNone(result["runtime_error"])
            self.assertIsInstance(Budget(**budget_label["budget_override"]), Budget)

            failure_task = self.snapshot.task("v4-tool-failure-02")
            failure_label = self.snapshot.label("v4-tool-failure-02")
            failed = run_task(self.snapshot, failure_task, "react", databases,
                              injected_failure=failure_label["injected_failure"])
            self.assertTrue(failed["failure_triggered"])
            self.assertIn("tool_failure", (failed["termination_reason"] or ""))

    def test_multi_turn_runs_each_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            databases = dataset.materialize_template(self.snapshot, temporary)
            task = self.snapshot.task("v4-multi-turn-03")
            result = run_task(self.snapshot, task, "react", databases)
        self.assertEqual(result["turn_count"], 2)
        self.assertEqual(len(result["answers"]), 2)
        self.assertEqual(result["model_calls"], 0)
        self.assertIsNotNone(result["run_id"])
        self.assertIsNone(result["runtime_error"])

    def test_model_call_count_is_not_undercounted_by_final_action_label(self):
        trace = [
            TraceStep(step_index=1, action="tool_call", requested_output_tokens=8192),
            TraceStep(step_index=2, action="tool_attempt"),
            TraceStep(step_index=3, action="tool_call", requested_output_tokens=8192),
            TraceStep(step_index=4, action="tool_attempt"),
            TraceStep(step_index=5, action="model_decision"),
        ]
        self.assertEqual(_model_call_count(trace), 2)

    def test_grading_requires_complete_explicit_human_judgment(self):
        template = grade.template(self.report, [json.loads(line) for line in
                                                (self.output / "results.jsonl")
                                                .read_text(encoding="utf-8").splitlines()])
        self.assertEqual(len(template["grades"]), 2)
        self.assertTrue(all(item["task_success"] is None for item in template["grades"]))

        one = Path(self.temp.name) / "grades-one.json"
        one.write_text(json.dumps({"grades": [{**template["grades"][0],
                                               "reviewer": "reviewer-a",
                                               "task_success": True}]}),
                       encoding="utf-8")
        graded, report = grade.run(self.output, one, Path(self.temp.name) / "graded-one")
        self.assertIsNone(report["aggregates"]["overall"]["TaskSuccessRate"])
        self.assertEqual(report["pending_grading"]["pending"], 1)

        both = Path(self.temp.name) / "grades-both.json"
        both.write_text(json.dumps({"grades": [
            {**item, "reviewer": "reviewer-a", "task_success": item["backend"] == "react"}
            for item in template["grades"]]}), encoding="utf-8")
        _, complete = grade.run(self.output, both, Path(self.temp.name) / "graded-both")
        self.assertEqual(complete["aggregates"]["overall"]["TaskSuccessRate"], 0.5)
        self.assertEqual(complete["aggregates"]["overall"]["TaskSuccessRate_status"],
                         "human_graded")

    def test_ablation_builds_hybrid_index_and_reports_requested_channel(self):
        target = Path(self.temp.name) / "ablation"
        with patch("src.retrieval._get_embedder", return_value=_FakeEmbedder()), \
                patch("src.index.retriever._get_embedder", return_value=_FakeEmbedder()):
            _, index = ablations.run(
                target, arms=["bm25_phase3", "hybrid_phase3", "bm25_phase2_simulated"],
                task_ids=["v4-single-source-01"], backends=["workflow"])
        self.assertEqual(index["matrix_completeness"], "partial")
        by_arm = {arm["arm"]: arm for arm in index["arms"]}
        self.assertEqual(by_arm["bm25_phase3"]["arm_status"],
                         "executed_requested_configuration")
        self.assertEqual(by_arm["hybrid_phase3"]["arm_status"],
                         "executed_requested_configuration")
        observed = by_arm["hybrid_phase3"]["retrieval_observed"]
        self.assertEqual(observed["effective_modes"], ["hybrid"])
        self.assertFalse(observed["fallback_used"])
        self.assertTrue(observed["index_versions"])
        self.assertEqual(by_arm["hybrid_phase3"]["index_preparation"]["status"], "ready")
        self.assertIn("simulated", by_arm["bm25_phase2_simulated"]["context_strategy"])
        self.assertTrue((target / "ablation.json").exists())


class IndexPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.snapshot = dataset.load()

    def test_preparation_writes_real_faiss_and_dense_uses_it(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("src.retrieval._get_embedder", return_value=_FakeEmbedder()):
            template = dataset.materialize_template(self.snapshot, temporary)
            report = indexing.prepare_indexes(template[0], self.snapshot)
            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["index_count"], 3)
            self.assertEqual(report["chunk_count"], 372)
            self.assertTrue(all(entry["verified"] for entry in report["indexes"]))
            vectors = list((Path(template[0]).parent / "live_index").rglob("faiss.index"))
            self.assertEqual(len(vectors), 3)
            self.assertTrue(all(path.stat().st_size > 0 for path in vectors))

            project = self.snapshot.records[0].project_id
            retriever = LiveRetriever(template[0], embedder=_FakeEmbedder())
            _, diagnostics = retriever.retrieve_with_diagnostics(
                "observability incident", scope=EvidenceScope(project_id=project,
                                                               dataset_kind="live"),
                mode="dense", top_k=3)
        self.assertEqual(diagnostics["requested_mode"], "dense")
        self.assertEqual(diagnostics["effective_mode"], "dense")
        self.assertIsNone(diagnostics["fallback_reason"])
        self.assertTrue(diagnostics["index_version"])

    def test_dense_run_aborts_when_preparation_fails(self):
        output = Path(self.temp.name) / "index-failure"
        with patch("benchmark.v4.run_offline.indexing.prepare_indexes",
                   side_effect=IndexPreparationError("synthetic build failure")):
            with self.assertRaises(IndexPreparationError):
                run_offline.run(output, task_ids=["v4-single-source-01"],
                                backends=["workflow"], retrieval_mode="dense")
        self.assertFalse(output.exists())

    def test_dense_run_aborts_on_runtime_bm25_fallback(self):
        output = Path(self.temp.name) / "fallback-abort"
        raw = _base_raw(backend="workflow", retrieval={
            "requested_modes": ["dense"], "effective_modes": ["bm25_fallback"],
            "fallback_reasons": ["synthetic"], "index_versions": [],
            "fallback_used": True})
        with patch("benchmark.v4.run_offline.indexing.prepare_indexes",
                   return_value={"status": "ready", "indexes": [], "index_count": 0,
                                 "chunk_count": 0, "embedding_model": "test"}):
            with patch("benchmark.v4.run_offline.run_task", return_value=raw):
                with self.assertRaises(run_offline.RetrievalChannelError):
                    run_offline.run(output, task_ids=["v4-single-source-01"],
                                    backends=["workflow"], retrieval_mode="dense")
        self.assertFalse(output.exists())


class OnlineRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.snapshot = dataset.load()
        cls.key = "sk-live-test-abc123"
        cls.single = next(task for task in cls.snapshot.public_tasks
                          if task["category"] == "single_source")
        cls.second = [task for task in cls.snapshot.public_tasks
                      if task["category"] == "single_source"][1]

    def config(self, *, output=None, resume=None, task_ids=None, max_calls=10,
               max_tokens=1000, base_url="https://api.deepseek.com",
               approved="https://api.deepseek.com", max_tasks=None, max_retries=0,
               backend="react", retrieval_mode="bm25"):
        return run_online.OnlineConfig(
            provider="deepseek", model="deepseek-chat", base_url=base_url,
            approved_origin=approved, api_key_env="DEEPSEEK_API_KEY", output=output,
            resume=resume, task_ids=tuple(task_ids or []), categories=(),
            max_tasks=max_tasks, max_model_calls=max_calls,
            max_budget_tokens=max_tokens, max_model_rounds=3, max_tool_calls=4,
            max_output_tokens=512, max_retries=max_retries, timeout_seconds=10.0,
            retrieval_mode=retrieval_mode, backend=backend)

    def fake_row(self, task, *, http_status=None, calls=1, tokens=10):
        label = self.snapshot.label(task["task_id"])
        ids = [ref["evidence_id"] for group in label["required_evidence_groups"]
               for ref in group["any_of"]]
        steps = []
        if http_status is not None:
            steps = [{"errors": [{"error_code": "model_error",
                                  "diagnostic": {"http_status": http_status}}]}]
        return {
            "task_id": task["task_id"], "backend": "react", "turn_count": 1,
            "answer": "测试回答", "answers": ["测试回答"], "claims": [],
            "citation_refs": [], "retrieved_ids": ids[:10], "decision_memories": [],
            "events": [], "gaps": [], "conflicts": [], "termination_reason": "sufficient",
            "termination_detail": None, "mode": "react", "runtime_error": None,
            "model_calls": calls, "provider_input_tokens": tokens,
            "provider_output_tokens": 0, "estimated_tokens": tokens,
            "tool_calls": 1, "latency_ms": 1.0, "claim_source_checks": [],
            "trace": [{"turn": 1, "steps": steps}],
        }

    def test_default_plan_makes_no_request_and_reads_no_key(self):
        output = Path(self.temp.name) / "never-created"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}):
            report = run_online.run(self.config(output=output, base_url="",
                                                approved=None))
        self.assertEqual(report["status"], "pending_authorization")
        self.assertFalse(report["online_executed"])
        self.assertFalse(output.exists())
        self.assertEqual(report["supported_online_backends"], ["react", "workflow"])
        self.assertEqual(report["workflow_online_status"],
                         "supported_with_disclosed_capability_differences")
        self.assertNotIn(self.key, json.dumps(report, ensure_ascii=False))

    def test_confirmation_and_positive_caps_are_mandatory(self):
        output = Path(self.temp.name) / "refused"
        refused = run_online.run(self.config(output=output), online=True, confirmed=False)
        self.assertEqual(refused["status"], "refused_missing_explicit_confirmation")
        self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            run_online.run(self.config(output=output, max_calls=0), online=True,
                           confirmed=True)
        with self.assertRaises(ValueError):
            run_online.run(self.config(output=output, base_url="https://api.deepseek.com",
                                       approved="https://evil.example"),
                           online=True, confirmed=True)

    def test_402_stops_immediately_and_report_contains_no_key(self):
        output = Path(self.temp.name) / "payment-required"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), \
                patch("benchmark.v4.run_online.run_task",
                      return_value=self.fake_row(self.single, http_status=402,
                                                 calls=1, tokens=7)):
            report = run_online.run(self.config(
                output=output, task_ids=[self.single["task_id"], self.second["task_id"]]),
                online=True, confirmed=True)
        self.assertEqual(report["status"], "stopped_insufficient_balance")
        self.assertEqual(report["stopped_reason"], "http_402")
        self.assertEqual(report["execution"]["runs_recorded"], 1)
        self.assertEqual(report["execution"]["model_calls"], 1)
        self.assertFalse(report["config"]["api_key_recorded"])
        for path in output.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.key, path.read_text(encoding="utf-8", errors="ignore"),
                                 str(path))

    def test_429_retries_within_limit_and_503_is_classified(self):
        output = Path(self.temp.name) / "retry"
        responses = [self.fake_row(self.single, http_status=429, calls=1, tokens=1),
                     self.fake_row(self.single, calls=1, tokens=1)]
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), \
                patch("benchmark.v4.run_online.run_task", side_effect=responses), \
                patch("benchmark.v4.run_online.time.sleep", return_value=None):
            report = run_online.run(self.config(
                output=output, task_ids=[self.single["task_id"]],
                max_retries=1), online=True, confirmed=True)
        row = json.loads((output / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["run_status"], "completed")
        self.assertEqual(report["execution"]["attempts"], 2)

        unavailable = Path(self.temp.name) / "unavailable"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), \
                patch("benchmark.v4.run_online.run_task",
                      return_value=self.fake_row(self.single, http_status=503)):
            report = run_online.run(self.config(
                output=unavailable, task_ids=[self.single["task_id"]],
                max_retries=0), online=True, confirmed=True)
        row = json.loads((unavailable / "results.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
        self.assertEqual(row["run_status"], "provider_unavailable")
        self.assertEqual(run_online._classify([503]), "provider_unavailable")
        self.assertEqual(
            run_online._classify([], {"termination_reason": "model_error"}),
            "model_error")

    def test_call_and_token_caps_stop_before_next_task(self):
        output = Path(self.temp.name) / "call-cap"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), \
                patch("benchmark.v4.run_online.run_task",
                      side_effect=lambda _snapshot, task, *a, **k: self.fake_row(task)):
            report = run_online.run(self.config(
                output=output, task_ids=[self.single["task_id"], self.second["task_id"]],
                max_calls=1), online=True, confirmed=True)
        self.assertEqual(report["status"], "stopped_model_call_budget")
        self.assertEqual(report["execution"]["runs_recorded"], 1)

        token_output = Path(self.temp.name) / "token-cap"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), \
                patch("benchmark.v4.run_online.run_task",
                      side_effect=lambda _snapshot, task, *a, **k: self.fake_row(task, tokens=9)):
            report = run_online.run(self.config(
                output=token_output,
                task_ids=[self.single["task_id"], self.second["task_id"]],
                max_calls=10, max_tokens=5), online=True, confirmed=True)
        self.assertEqual(report["status"], "stopped_token_budget")
        self.assertEqual(report["execution"]["runs_recorded"], 1)

    def test_workflow_plan_discloses_capability_differences(self):
        report = run_online.run(self.config(output=Path(self.temp.name) / "wf-plan",
                                            base_url="", approved=None,
                                            backend="workflow"))
        self.assertEqual(report["selected_backend"], "workflow")
        self.assertEqual(report["supported_online_backends"], ["react", "workflow"])
        self.assertFalse(report["backend_capabilities"]["native_budget_protocol"])
        self.assertIn("causal gain", report["backend_comparability"]["interpretation"])
        self.assertIn("must not", report["backend_comparability"]["interpretation"])
        self.assertIn("Workflow has no native round concept",
                      report["cap_applicability"]["max_model_rounds"])
        turns = sum(len(task.get("turns") or [task["question"]])
                    for task in self.snapshot.public_tasks)
        self.assertEqual(report["max_normal_model_calls_upper_bound"], turns * 3)

    def test_workflow_online_reuses_chain_and_stops_on_402(self):
        output = Path(self.temp.name) / "wf-402"
        captured = {}

        def fake(_snapshot, task, backend, *args, **kwargs):
            captured.update(backend=backend, kwargs=kwargs)
            row = self.fake_row(task)
            row.update(backend="workflow", mode="online_model",
                       model_error_http_statuses=[402], model_calls=3,
                       external_caps=kwargs.get("online_limits"))
            return row

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), \
                patch("benchmark.v4.run_online.run_task", side_effect=fake):
            report = run_online.run(self.config(
                output=output, task_ids=[self.single["task_id"]],
                backend="workflow"), online=True, confirmed=True)
        self.assertEqual(captured["backend"], "workflow")
        self.assertTrue(captured["kwargs"]["online"])
        self.assertGreater(captured["kwargs"]["online_limits"]["max_model_calls"], 0)
        self.assertEqual(report["status"], "stopped_insufficient_balance")
        row = json.loads((output / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["backend"], "workflow")
        self.assertEqual(row["run_status"], "insufficient_balance")

    def test_external_guard_counts_tokens_and_enforces_caps(self):
        class Response:
            def __init__(self):
                self.payload = b'{"usage":{"prompt_tokens":3,"completion_tokens":2}}'

            def read(self, amount=None):
                body, self.payload = self.payload, b""
                return body

            def close(self):
                return None

        class Opener:
            def open(self, request, timeout=None):
                return Response()

        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions", data=b"{}", method="POST")
        original = urllib.request.urlopen
        with patch("urllib.request.build_opener", return_value=Opener()):
            with ModelHttpGuard(max_calls=1, max_tokens=100) as guard:
                with urllib.request.urlopen(request) as response:
                    self.assertIn(b"usage", response.read())
                with self.assertRaises(ModelCallStop) as stopped:
                    urllib.request.urlopen(request)
        self.assertEqual(stopped.exception.reason, "max_model_calls")
        self.assertEqual(guard.calls, 1)
        self.assertEqual(guard.prompt_tokens, 3)
        self.assertEqual(guard.completion_tokens, 2)
        self.assertIs(urllib.request.urlopen, original)

    def test_external_guard_stops_immediately_on_402(self):
        class Opener:
            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(request.full_url, 402, "payment", {}, None)

        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions", data=b"{}", method="POST")
        with patch("urllib.request.build_opener", return_value=Opener()):
            with ModelHttpGuard(max_calls=2, max_tokens=100) as guard:
                with self.assertRaises(ModelCallStop) as stopped:
                    urllib.request.urlopen(request)
        self.assertEqual(stopped.exception.reason, "http_402")
        self.assertEqual(guard.statuses, [402])
        self.assertEqual(guard.stop_reason, "http_402")

    def test_budget_exhaustion_is_classified(self):
        self.assertEqual(run_online._classify([], {"termination_reason": "max_budget_tokens"}),
                         "budget_exhausted")
        self.assertEqual(run_online._classify([], {"termination_reason": "max_model_calls"}),
                         "budget_exhausted")

    def test_resume_preserves_completed_and_runs_only_pending(self):
        output = Path(self.temp.name) / "resume"
        config = self.config(resume=output,
                             task_ids=[self.single["task_id"], self.second["task_id"]])
        with tempfile.TemporaryDirectory() as temporary:
            template = dataset.materialize_template(self.snapshot, temporary)
            clone = dataset.clone_databases(template, Path(temporary) / "prior")
            prior = run_online._row(self.snapshot, self.single,
                                    self.fake_row(self.single), clone, 1)
        output.mkdir()
        (output / "run_state.json").write_text(json.dumps({
            "run_id": "prior-run",
            "config_fingerprint": run_online._fingerprint(
                run_online._config_payload(config, self.snapshot)),
        }), encoding="utf-8")
        (output / "results.jsonl").write_text(
            json.dumps(prior, ensure_ascii=False) + "\n", encoding="utf-8")
        calls = []

        def fake(_snapshot, task, *args, **kwargs):
            calls.append(task["task_id"])
            return self.fake_row(task)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), \
                patch("benchmark.v4.run_online.run_task", side_effect=fake):
            report = run_online.run(config, online=True, confirmed=True)
        self.assertEqual(calls, [self.second["task_id"]])
        self.assertEqual(report["execution"]["runs_recorded"], 2)
        self.assertEqual(report["execution"]["tasks_selected"], 2)


class AcceptancePreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = dataset.load()
        cls.plan = acceptance.build_plan(cls.snapshot)

    def test_selection_covers_required_categories_within_bounds(self):
        plan = self.plan
        self.assertEqual(plan["status"], "prepared_not_executed")
        self.assertEqual(plan["task_count"], 10)
        self.assertTrue(8 <= plan["task_count"] <= 12)
        self.assertEqual(len({item["task_id"] for item in plan["tasks"]}), 10)
        self.assertEqual(set(plan["coverage"]["categories"]), {
            "single_source", "cross_source", "multi_step", "decision_history",
            "conflict", "insufficient", "multi_turn", "tool_failure", "budget"})
        self.assertGreaterEqual(plan["coverage"]["tool_failure_tasks"], 1)
        self.assertGreaterEqual(plan["coverage"]["budget_tasks"], 1)
        self.assertGreaterEqual(plan["coverage"]["multi_turn"], 1)
        self.assertEqual(plan["experiment"]["snapshot"]["task_set_sha256"],
                         self.snapshot.manifest["task_set_sha256"])

    def test_caps_cover_derived_call_upper_bound(self):
        experiment = self.plan["experiment"]
        upper = experiment["call_upper_bound"]
        self.assertEqual(upper["turns"], 11)
        self.assertEqual(upper["react"], 77)
        self.assertEqual(upper["workflow"], 33)
        self.assertGreater(experiment["per_backend_caps"]["react"]["max_model_calls"],
                           upper["react"])
        self.assertGreater(experiment["per_backend_caps"]["workflow"]["max_model_calls"],
                           upper["workflow"])
        self.assertEqual(experiment["per_backend_caps"]["react"]["max_model_calls"],
                         experiment["per_backend_caps"]["workflow"]["max_model_calls"])
        self.assertEqual(experiment["per_backend_caps"]["react"]["max_budget_tokens"],
                         experiment["per_backend_caps"]["workflow"]["max_budget_tokens"])
        self.assertGreater(experiment["per_backend_caps"]["react"]["max_budget_tokens"], 0)
        self.assertGreater(experiment["per_backend_caps"]["workflow"]["max_budget_tokens"], 0)

    def test_gold_checklists_resolve_and_carry_manual_rules(self):
        for checklist in self.plan["checklists"]:
            groups = (checklist["required_evidence_groups"] +
                      checklist["acceptable_alternative_evidence"])
            self.assertTrue(groups, checklist["task_id"])
            for group in groups:
                for ref in group["any_of"]:
                    self.assertTrue(ref["resolves"], ref["evidence_id"])
                    self.assertEqual(len(ref["content_hash"]), 64)
            self.assertTrue(checklist["review_checks"])
            self.assertTrue(checklist["human_review_rules"])
        failures = {item["task_id"]: item["injected_failure"]
                    for item in self.plan["tasks"] if item["injected_failure"]}
        self.assertEqual(failures, {
            "v4-tool-failure-01": {"skill": "document", "error": "timeout"},
            "v4-tool-failure-02": {"skill": "problem", "error": "timeout"},
        })

    def test_plan_makes_no_network_call_and_requires_operator_supplied_model(self):
        with patch("urllib.request.urlopen",
                   side_effect=AssertionError("acceptance preparation used the network")):
            plan = acceptance.build_plan(self.snapshot)
        self.assertFalse(plan["experiment"]["this_round_made_model_calls"])
        self.assertEqual(plan["experiment"]["provider_model"]["status"],
                         "operator_must_supply")
        self.assertEqual(plan["experiment"]["token_and_cost_method"]["this_round_cost"], None)
        self.assertEqual(plan["experiment"]["comparability"]["incomparable"],
                         ["budget-protocol behaviour", "Claim Ledger validation",
                          "known_at filtering", "multi-round investigation depth"])
        rule = plan["experiment"]["comparability"]["interpretation_rule"].lower()
        self.assertIn("do not attribute", rule)
        self.assertIn("end-to-end", rule)

    def test_write_creates_review_artifacts_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "acceptance"
            plan = acceptance.write(output, snapshot=self.snapshot)
            self.assertEqual(sorted(plan["files"]), sorted([
                "README.md", "gold_review_checklist.json", "gold_review_checklist.md",
                "grading_template.csv", "plan.json", "result_summary_template.md",
                "termination_recovery.md"]))
            grading = (output / "grading_template.csv").read_text(encoding="utf-8")
            rows = [line for line in grading.splitlines() if line.strip()]
            self.assertEqual(len(rows), 1 + 2 * 10)
            self.assertIn("v4-budget-01,workflow", grading)
            self.assertIn("n/a", grading)
            stored = json.loads((output / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["status"], "prepared_not_executed")
            self.assertNotIn("sk-", json.dumps(stored, ensure_ascii=False))
            with self.assertRaises(FileExistsError):
                acceptance.write(output, snapshot=self.snapshot)


if __name__ == "__main__":
    unittest.main()
