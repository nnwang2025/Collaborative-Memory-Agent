"""Independent dataset, metric and backend-adapter regression tests; no online models."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from benchmark.dataset import ROOT, load, materialize, validate
from benchmark.metrics import aggregate, retrieval_metrics
from benchmark.runner import PublicCase, run, run_backend, score
from benchmark.scripts.build import build
from benchmark.scripts.grade import apply_grades
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.memory.decisions import DecisionStore

from offline_env import OFFLINE_PROVIDER_ENV


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frozen = load()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-benchmark-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, OFFLINE_PROVIDER_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def cloned_snapshot(self):
        target = self.root / "snapshot"
        target.mkdir()
        shutil.copy2(ROOT / "manifest.json", target / "manifest.json")
        for name in ("corpus", "cases", "labels"):
            shutil.copytree(ROOT / name, target / name)
        return target

    def raw(self, backend="workflow", case_id="single-atlas"):
        case = next(c for c in self.frozen["cases"] if c["case_id"] == case_id)
        runtime = materialize(self.frozen, self.root / (backend + case_id))
        return run_backend(case, runtime, backend, self.frozen["labels"][case_id].get("injected_failure")), case

    def test_manifest_and_category_counts(self):
        checked = validate(self.frozen, self.root / "runtime")
        self.assertEqual(checked["test_cases"], 14)
        self.assertEqual(checked["dev_cases"], 2)
        self.assertEqual(checked["evidence_records"], 27)
        self.assertTrue(all(n == 2 for n in checked["category_counts"].values()))

    def test_fixed_seed_rebuild_is_identical_and_never_overwrites(self):
        first, second = self.root / "one", self.root / "two"
        self.assertEqual(build(first), build(second))
        self.assertEqual((first / "manifest.json").read_bytes(), (second / "manifest.json").read_bytes())
        self.assertEqual(load(first)["manifest"], self.frozen["manifest"])
        with self.assertRaises(FileExistsError): build(first)

    def test_manifest_detects_changed_content_and_extra_label(self):
        snapshot = self.cloned_snapshot()
        path = snapshot / "corpus/evidence.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaises(ValueError): load(snapshot)
        path.write_bytes(original)
        (snapshot / "labels/extra.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError): load(snapshot)

    def test_gold_missing_and_wrong_hash_fail(self):
        for mutation in ("id", "hash"):
            data = copy.deepcopy(self.frozen)
            label = data["labels"]["single-atlas"]
            if mutation == "id": label["gold_evidence_ids"] = ["ev_" + "0" * 64]
            else: label["gold_content_hashes"][label["gold_evidence_ids"][0]] = "0" * 64
            with self.assertRaises(ValueError): validate(data, self.root / mutation)

    def test_cross_project_gold_is_rejected(self):
        data = copy.deepcopy(self.frozen)
        data["labels"]["single-atlas"]["gold_evidence_ids"] = data["labels"]["single-zephyr"]["gold_evidence_ids"]
        with self.assertRaises(ValueError): validate(data, self.root / "invalid")

    def test_missing_decision_version_is_rejected(self):
        data = copy.deepcopy(self.frozen)
        data["labels"]["decision-atlas"]["gold_decision_version_ids"] = ["dv_missing"]
        with self.assertRaises(ValueError): validate(data, self.root / "invalid")

    def test_conflict_and_insufficiency_labels_have_corpus_constraints(self):
        for variant in ("conflict", "missing"):
            data = copy.deepcopy(self.frozen)
            if variant == "conflict":
                ids = data["labels"]["conflict-atlas"]["gold_evidence_ids"]
                for record in data["records"]:
                    if record.evidence_id in ids: record.metadata["assertions"]["ack_mode"] = "same"
            else:
                next(r for r in data["records"] if r.project_id == "bench-atlas").metadata["assertions"]["production_execution"] = True
            with self.assertRaises(ValueError): validate(data, self.root / variant)

    def test_runtime_copies_no_labels_and_scope_is_enforced(self):
        runtime = materialize(self.frozen, self.root / "runtime")
        self.assertEqual({p.name for p in runtime[0].parent.iterdir()}, {"evidence.sqlite3", "decisions.sqlite3"})
        store = EvidenceStore(runtime[0])
        self.assertFalse(store.list(EvidenceScope(project_id="outside", dataset_kind="live")))
        self.assertFalse(store.list(EvidenceScope(project_id="bench-atlas", dataset_kind="fixture")))
        self.assertTrue(all(r.project_id == "bench-atlas" for r in store.list(EvidenceScope(project_id="bench-atlas", dataset_kind="live"))))

    def test_labels_are_not_read_by_backends_or_accepted_as_public_input(self):
        case = next(c for c in self.frozen["cases"] if c["case_id"] == "single-atlas")
        with self.assertRaises(ValidationError): PublicCase.model_validate({**case, "gold_evidence_ids": ["leak"]})
        original = Path.read_text
        def guarded(path, *args, **kwargs):
            if "labels" in path.parts: raise AssertionError("Agent accessed hidden labels")
            return original(path, *args, **kwargs)
        for backend in ("workflow", "react"):
            runtime = materialize(self.frozen, self.root / backend)
            with patch.object(Path, "read_text", guarded):
                result = run_backend(case, runtime, backend)
            self.assertIsNone(result["runtime_error"])
            self.assertEqual(result["model_calls"], 0)

    def test_hit_recall_mrr_distinct_and_duplicates_do_not_inflate(self):
        metric = retrieval_metrics(["x", "a", "a", "b"], ["a", "b", "c"], k=2)
        self.assertEqual(metric, {"hit": 1, "recall": 1 / 3, "mrr": 1 / 2})
        self.assertEqual(retrieval_metrics(["x", "y", "z", "a"], ["a"], k=2), {"hit": 0, "recall": 0, "mrr": .25})
        self.assertIsNone(retrieval_metrics([], [])["recall"])

    def test_empty_gold_excluded_from_coverage_denominator(self):
        raw, case = self.raw()
        row = score(raw, case, self.frozen["labels"][case["case_id"]])
        empty = {**row, "gold_evidence_ids": []}
        result = aggregate([row, empty])
        self.assertEqual(result["EvidenceCoverage"]["query_count"], 1)
        self.assertEqual(result["Hit@5"]["query_count"], 1)

    def test_backend_adapters_share_result_schema_and_independent_run_ids(self):
        workflow, _ = self.raw("workflow")
        react, _ = self.raw("react")
        required = {"run_id", "answer", "retrieved_ids", "citation_refs", "events", "latency_ms", "tool_attempts", "model_calls", "termination_reason"}
        self.assertTrue(required.issubset(workflow) and required.issubset(react))
        self.assertNotEqual(workflow["run_id"], react["run_id"])
        self.assertEqual(workflow["valid_citations"], workflow["citation_count"])
        self.assertEqual(react["mode"], "offline_fallback")

    def test_ungraded_not_scored_from_sufficient_or_strings(self):
        raw, case = self.raw()
        raw["termination_reason"] = "sufficient"
        raw["answer"] = "All answers correct"
        label = self.frozen["labels"][case["case_id"]]
        row = score(raw, case, label)
        self.assertEqual(row["status"], "ungraded")
        self.assertIsNone(aggregate([row])["TaskSuccessRate"]["value"])
        graded = score(raw, case, label, {"reviewer": "test-only-reviewer", "task_success": True})
        self.assertEqual(aggregate([row, graded])["TaskSuccessRate"]["denominator"], 1)

    def test_unsupported_not_mistaken_for_correct_abstention(self):
        raw, case = self.raw("react", "decision-atlas")
        row = score(raw, case, self.frozen["labels"][case["case_id"]], {"reviewer": "test-only", "task_success": True})
        self.assertEqual(row["status"], "unsupported")
        self.assertIsNone(row["human_task_success"])
        self.assertIsNone(aggregate([row])["TaskSuccessRate"]["value"])

    def test_failure_is_injected_into_real_tools_without_corpus_mutation(self):
        raw, case = self.raw("react", "recovery-atlas")
        self.assertTrue(raw["failure_triggered"])
        self.assertEqual(raw["termination_reason"], "tool_failure")
        self.assertEqual(raw["tool_attempts"], 2)
        self.assertEqual(load()["manifest"], self.frozen["manifest"])
        row = score(raw, case, self.frozen["labels"][case["case_id"]])
        self.assertEqual(row["status"], "unsupported")

    def test_decision_labels_match_current_history_pending_and_supersedes(self):
        evidence, memory = materialize(self.frozen, self.root / "runtime")
        store = DecisionStore(memory, evidence_db_path=evidence)
        for case_id in ("decision-atlas", "decision-zephyr", "missing-zephyr"):
            case = next(c for c in self.frozen["cases"] if c["case_id"] == case_id)
            label = self.frozen["labels"][case_id]
            result = store.snapshot(label["decision_id"], EvidenceScope(project_id=case["project_id"], dataset_kind=case["dataset_kind"]), known_at=label.get("known_at"))
            self.assertEqual((result["current_confirmed"] or {}).get("version_id"), label["expected_current_version_id"])
            if case_id == "decision-atlas": self.assertTrue(any(p["review_status"] == "pending" for p in result["proposals"]))

    def test_reports_reproduce_non_timing_metrics_on_development_split(self):
        _, first = run(output=self.root / "first", split="dev")
        _, second = run(output=self.root / "second", split="dev")
        for summary in (first, second):
            for backend in summary["backends"].values():
                backend.pop("latency_mean_ms"); backend.pop("latency_p95_ms")
        self.assertEqual(first, second)
        rows = (self.root / "first/results.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 4)
        with self.assertRaises(FileExistsError): run(output=self.root / "first", split="dev")

    def test_synthetic_git_identity_and_no_test_dev_project_overlap(self):
        git = [r for r in self.frozen["records"] if r.source_type == "git_commit"]
        self.assertEqual(len(git), 6)
        self.assertTrue(all(len(r.original_record_id) == 40 and r.metadata["synthetic"] for r in git))
        cases = self.frozen["cases"]
        self.assertFalse({c["project_id"] for c in cases if c["split"] == "dev"} & {c["project_id"] for c in cases if c["split"] == "test"})

    def test_human_grading_is_explicit_post_run_and_never_replaces_report(self):
        original = self.root / "original"
        run(output=original, split="dev")
        original_bytes = (original / "results.jsonl").read_bytes()
        row = json.loads(original_bytes.splitlines()[0])
        grade = {k: row[k] for k in ("run_id", "case_id", "backend")}
        grade.update(reviewer="test-only-reviewer", task_success=False, notes="Unit test, no actual human annotation")
        path = self.root / "test-grades.json"
        path.write_text(json.dumps([grade]), encoding="utf-8")
        with patch("benchmark.runner.run_backend", side_effect=AssertionError("Grading must not rerun agents")):
            summary = apply_grades(original, path, self.root / "graded")
        self.assertEqual(summary["backends"][row["backend"]]["TaskSuccessRate"], {"numerator": 0, "denominator": 1, "value": 0})
        self.assertEqual((original / "results.jsonl").read_bytes(), original_bytes)
        with self.assertRaises(FileExistsError): apply_grades(original, path, self.root / "graded")
        for bad in ({**grade, "backend": "wrong"}, {**grade, "task_success": "yes"}, {**grade, "appropriate_abstention": "yes"}):
            path.write_text(json.dumps([bad]), encoding="utf-8")
            with self.assertRaises(ValueError): apply_grades(original, path, self.root / "invalid")

    def test_smoke_preparation_telemetry_and_invalid_action_counter(self):
        from src.llm_smoke import run_suite, _invalid_tool_action
        with patch("src.llm_smoke.prepare", return_value=(self.root / "missing.sqlite3", EvidenceScope(project_id="synthetic-smoke", dataset_kind="live"))), \
             patch("src.agents.react_agent.ReActAgent", side_effect=AssertionError("No provider without authorization")):
            result = run_suite()
        self.assertEqual(result["online_status"], "pending_authorization")
        self.assertFalse(result["online_executed"])
        self.assertEqual(_invalid_tool_action('{"action":"tool_call","tool":"invented"}'), 1)
        self.assertEqual(_invalid_tool_action('```json\n{"action":"tool_call","tool":"invented"}\n```'), 1)
        self.assertEqual(_invalid_tool_action('{"action":"tool_call","tool":"search_docs"}'), 0)
        self.assertEqual(_invalid_tool_action('not json'), 0)


if __name__ == "__main__":
    unittest.main()
