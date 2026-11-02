"""Unit tests for the retrieval final-acceptance policy.

These tests are deterministic and do not load an embedding model.  They lock
down the signal semantics: lexical coverage is not a cosine score, a missing
coverage value is not zero, and unrelated dense nearest neighbours are rejected
unless a calibrated independent signal accepts them.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.index.relevance import (
    RelevancePolicy,
    identifier_match,
    is_stopword,
    lexical_coverage,
    load_relevance_policy,
    meaningful_tokens,
)


class CoverageAndIdentifierTests(unittest.TestCase):
    def test_coverage_ignores_grammar_and_task_words(self):
        self.assertIsNone(lexical_coverage("decision", "decision B"))
        self.assertIsNone(lexical_coverage("please find the decision", "anything"))
        self.assertEqual(
            lexical_coverage("credential rotation schedule",
                             "Rotate signing credentials on a fixed schedule."),
            1.0,
        )
        self.assertEqual(
            lexical_coverage("tombstone lifecycle", "Tombstone lifecycle."), 1.0,
        )

    def test_coverage_normalizes_edge_punctuation_and_plurals(self):
        # A trailing sentence period must not turn a match into a miss.
        self.assertEqual(lexical_coverage("lifecycle.", "Tombstone lifecycle."), 1.0)
        self.assertEqual(lexical_coverage("credentials", "credential rotation"), 1.0)

    def test_stopwords_include_inflected_task_words(self):
        self.assertTrue(is_stopword("decision"))
        self.assertTrue(is_stopword("decisions"))
        self.assertTrue(is_stopword("documents"))
        self.assertFalse(is_stopword("credential"))

    def test_meaningful_tokens_are_distinct_and_ordered(self):
        tokens = meaningful_tokens("Gateway gateway RETRY retry tombstone")
        self.assertEqual(tokens, ["gateway", "retry", "tombstone"])

    def test_identifier_match_is_exact_and_token_bounded(self):
        self.assertTrue(identifier_match("INC-77", "Incident INC-77 was closed"))
        self.assertTrue(identifier_match("ARCH-42", "ARCH-42 design"))
        self.assertFalse(identifier_match("INC-999", "Incident INC-77 was closed"))
        # A commit SHA is not a prefix match inside a different SHA.
        self.assertFalse(identifier_match("abcdef1", "abcdef1234567890"))

    def test_chinese_option_label_is_bounded_and_case_sensitive(self):
        self.assertTrue(identifier_match("为什么选择B方案", "Decision choose B\nLatency"))
        self.assertFalse(identifier_match("为什么选择B方案", "Because latency mattered"))
        self.assertFalse(identifier_match("plain english query B", "choose B"))
        self.assertTrue(identifier_match("方案 B 的原因", "Decision choose B"))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.policy = RelevancePolicy(
            policy_version="test-policy", dense_min_score=0.75,
            lexical_min_coverage=0.55)

    def test_disabled_policy_accepts_every_candidate(self):
        decision = RelevancePolicy.disabled().decision(
            dense_score=0.1, lexical_coverage=0.0)
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["reason"], "policy_disabled")

    def test_identifier_bypasses_both_thresholds(self):
        decision = self.policy.decision(dense_score=0.1, lexical_coverage=0.0,
                                        identifier_match=True)
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["reason"], "exact_identifier")

    def test_each_signal_is_independent_and_not_averaged(self):
        dense = self.policy.decision(dense_score=0.9, lexical_coverage=0.0)
        lexical = self.policy.decision(dense_score=0.0, lexical_coverage=0.9)
        self.assertEqual(dense["reason"], "dense_threshold")
        self.assertEqual(lexical["reason"], "lexical_coverage_threshold")

    def test_dense_nearest_neighbour_below_floor_is_rejected(self):
        decision = self.policy.decision(dense_score=0.74, lexical_coverage=None)
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["filter_reason"], "below_relevance_thresholds")

    def test_missing_coverage_with_missing_dense_is_not_zero(self):
        # A BM25 candidate for a stopword-only query still shares the raw
        # query token; the gate records that it had no content signal.
        decision = self.policy.decision(dense_score=None, lexical_coverage=None)
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["reason"], "lexical_signal_unavailable")

    def test_both_signals_below_floor_are_rejected(self):
        decision = self.policy.decision(dense_score=0.2, lexical_coverage=0.1)
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["filter_reason"], "below_relevance_thresholds")


class PolicyLoadingTests(unittest.TestCase):
    def test_default_policy_is_valid_and_records_calibration(self):
        policy = load_relevance_policy()
        self.assertGreater(policy.dense_min_score, 0.0)
        self.assertGreater(policy.lexical_min_coverage, 0.0)
        self.assertFalse(policy.calibration.get("test_set_used_for_calibration", True))
        description = policy.describe()
        self.assertEqual(description["policy_version"], policy.policy_version)
        self.assertIn("independent OR", description["threshold_semantics"])

    def test_explicit_missing_policy_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                load_relevance_policy(Path(directory) / "missing.json")

    def test_invalid_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            for payload in (
                {"schema_version": 99, "policy_version": "x",
                 "dense_min_score": 0.5, "lexical_min_coverage": 0.5},
                {"schema_version": 1, "policy_version": "",
                 "dense_min_score": 0.5, "lexical_min_coverage": 0.5},
                {"schema_version": 1, "policy_version": "x",
                 "dense_min_score": 2.0, "lexical_min_coverage": 0.5},
                {"schema_version": 1, "policy_version": "x",
                 "dense_min_score": 0.5, "lexical_min_coverage": 1.5},
            ):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_relevance_policy(path)


if __name__ == "__main__":
    unittest.main()
