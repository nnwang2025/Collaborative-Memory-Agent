"""Real cached-model acceptance tests for live retrieval empty-result handling.

The corpus is deliberately separate from benchmark/v2.  It exercises the
calibrated final-acceptance gate end to end over the real local bge-small
embedding model (no network, no provider call).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from src.evidence import EvidenceScope
from src.index.builder import IndexBuilder
from src.index.retriever import LiveRetriever
from src.retrieval import _get_embedder
from src.sync.models import SourceSpec
from src.sync.service import SyncService

PROJECT = "relevance-live"
OTHER = "relevance-other"
MODES = ("bm25", "dense", "hybrid")

DOCUMENTS = {
    "incident.md": (
        "INC-77 payment latency incident. The root cause was database connection "
        "pool exhaustion. The fix rotated database credentials and increased the pool."
    ),
    "deploy.md": (
        "Deployment rollback runbook. Use the rollback command and verify tombstones "
        "before restoring deleted evidence."
    ),
    "security.md": (
        "Payment service credentials rotate every 90 days. The rotation job stores "
        "new keys in the vault."
    ),
    "release.md": (
        "Release 2.4 was delayed because the mobile client crashed on startup."
    ),
    "storage.md": (
        "Object storage archives are encrypted at rest with customer managed keys."
    ),
    "oncall.md": (
        "On-call engineers page the database team for payment outages."
    ),
    "gateway.md": (
        "API gateway rate limiting uses a token bucket of 100 requests per second."
    ),
    "billing.md": (
        "Billing invoices are generated monthly and stored in the finance bucket."
    ),
    "support.md": (
        "Safari browser rendering issues were fixed in the mobile client."
    ),
    "obsolete.md": (
        "Obsolete purge retention policy removed seven day backups."
    ),
}


class LiveRelevanceAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            _get_embedder()
        except Exception as exc:  # pragma: no cover - environment guard
            raise unittest.SkipTest(f"cached embedding model unavailable: {exc}")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-live-relevance-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        for name, text in DOCUMENTS.items():
            (self.docs / name).write_text(text, encoding="utf-8")
        self.db = self.root / "evidence.sqlite3"
        self.scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")
        self.service = SyncService(self.db)
        self.source = self.service.registry.register(SourceSpec(
            project_id=PROJECT, dataset_kind="live", connector_type="directory",
            source_uri=self.docs.as_uri(), config={"root_path": str(self.docs)},
        ))
        self.service.sync(self.source.source_id)
        self.builder = IndexBuilder(self.db)
        built = self.builder.build(self.scope)
        self.assertEqual(built["status"], "built")

    def query(self, query, mode, top_k=5):
        return LiveRetriever(self.db, mode=mode).retrieve(
            query, scope=self.scope, top_k=top_k)

    def assert_diagnostics(self, hits, expected_mode):
        diagnostics = hits.diagnostics
        self.assertEqual(diagnostics["requested_mode"], expected_mode)
        self.assertEqual(diagnostics["effective_mode"], expected_mode)
        self.assertTrue(diagnostics["policy_version"])
        self.assertIn("dense_min_score", diagnostics["thresholds"])
        self.assertIn("lexical_min_coverage", diagnostics["thresholds"])
        self.assertGreaterEqual(diagnostics["candidate_count"],
                                diagnostics["accepted_count"])
        self.assertEqual(diagnostics["accepted_count"], len(hits))
        self.assertEqual(diagnostics["empty_after_filtering"], not hits)

    def test_exact_identifier_and_semantic_rewrite_all_modes(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                exact = self.query("INC-77", mode)
                self.assertEqual(exact[0]["original_record_id"], "incident.md")
                self.assertEqual(exact[0]["relevance"]["reason"], "exact_identifier")
                self.assert_diagnostics(exact, mode)

                # Rewrite: the record says "rotate every 90 days", not this query.
                semantic = self.query("payment credential rotation schedule", mode)
                self.assertIn("security.md",
                              {hit["original_record_id"] for hit in semantic})
                self.assert_diagnostics(semantic, mode)
                top = semantic[0]
                self.assertTrue(top["content"])
                parent = self.service.store.get(
                    top["evidence_id"], self.scope, top["content_hash"])
                self.assertEqual(parent.content, top["content"])
                self.assertEqual(top["relevance"]["policy_version"],
                                 self.query("INC-77", mode).diagnostics["policy_version"])
                self.assertIn("lexical_coverage", top["relevance"])
                self.assertIn("dense_score", top["relevance"])
                self.assertIn("thresholds", top["relevance"])

    def test_completely_unrelated_queries_return_empty_in_all_modes(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                hits = self.query("photosynthesis chlorophyll telescope", mode)
                self.assertEqual(list(hits), [])
                self.assert_diagnostics(hits, mode)
                self.assertTrue(hits.diagnostics["empty_after_filtering"])
                if mode == "bm25":
                    self.assertEqual(hits.diagnostics["candidate_count"], 0)
                    self.assertEqual(hits.diagnostics["empty_reason"], "no_candidates")
                else:
                    # Dense always returns nearest neighbours; every one must be
                    # filtered rather than returned as evidence.
                    self.assertGreater(hits.diagnostics["candidate_count"], 0)
                    self.assertEqual(hits.diagnostics["empty_reason"],
                                     "all_candidates_filtered")
                    self.assertTrue(hits.diagnostics["filter_reasons"])

    def test_lexically_similar_but_semantically_wrong_is_filtered(self):
        # The support document contains both "Safari" and "browser", so BM25
        # recalls it; the query is nevertheless about a cafeteria menu.
        for mode in MODES:
            with self.subTest(mode=mode):
                hits = self.query("Safari browser cafeteria menu", mode)
                self.assertEqual(list(hits), [])
                self.assert_diagnostics(hits, mode)
                self.assertGreaterEqual(hits.diagnostics["candidate_count"], 1)
                self.assertEqual(hits.diagnostics["empty_reason"],
                                 "all_candidates_filtered")
                reasons = set(hits.diagnostics["filter_reasons"])
                self.assertTrue(reasons)
                self.assertTrue(all("relevance" in reason or "threshold" in reason
                                    for reason in reasons), reasons)

    def test_deleted_record_absent_and_stale_index_falls_back_explicitly(self):
        obsolete = next(record for record in self.service.store.list(self.scope)
                        if record.original_record_id == "obsolete.md")
        (self.docs / "obsolete.md").unlink()
        deleted = self.service.sync(self.source.source_id)
        self.assertEqual(deleted.deleted, 1)

        # The index still contains the deleted chunk; hydration and the revision
        # check must prevent resurrection, and the stale index must be named.
        stale = self.query("obsolete purge retention policy", "hybrid")
        self.assertEqual(stale.diagnostics["effective_mode"], "bm25_fallback")
        self.assertIn("stale_index_source_revision",
                      stale.diagnostics["fallback_reason"])
        self.assertEqual(stale.diagnostics["index_state"], "unusable")
        self.assertNotIn("obsolete.md",
                         {hit["original_record_id"] for hit in stale})

        # A live current record still works through the explicit fallback.
        fallback = self.query("credential rotation schedule", "hybrid")
        self.assertTrue(fallback)
        self.assertEqual({hit["retrieval_mode"] for hit in fallback},
                         {"bm25_fallback"})
        self.assertNotIn("obsolete.md",
                         {hit["original_record_id"] for hit in fallback})

        self.builder.build(self.scope, force=True)
        fresh = self.query("obsolete purge retention policy", "hybrid")
        self.assertEqual(fresh.diagnostics["effective_mode"], "hybrid")
        self.assertEqual(list(fresh), [])
        self.assertEqual(fresh.diagnostics["empty_reason"],
                         "all_candidates_filtered")
        # Historical evidence remains refetchable by id and hash.
        historical = self.service.store.get(
            obsolete.evidence_id, self.scope, obsolete.content_hash)
        self.assertIsNotNone(historical)
        self.assertEqual(historical.content, obsolete.content)

    def test_cross_project_evidence_never_leaks_into_results(self):
        other_docs = self.root / "other"
        other_docs.mkdir()
        (other_docs / "security.md").write_text(
            "Payment credential rotation schedule for the other project.", encoding="utf-8")
        other_scope = EvidenceScope(project_id=OTHER, dataset_kind="live")
        other_service = SyncService(self.db)
        other_source = other_service.registry.register(SourceSpec(
            project_id=OTHER, dataset_kind="live", connector_type="directory",
            source_uri=other_docs.as_uri(), config={"root_path": str(other_docs)},
        ))
        other_service.sync(other_source.source_id)
        other_built = IndexBuilder(self.db).build(other_scope)
        self.assertEqual(other_built["status"], "built")

        for mode in MODES:
            with self.subTest(mode=mode):
                own = LiveRetriever(self.db, mode=mode).retrieve(
                    "payment credential rotation schedule", scope=self.scope, top_k=10)
                self.assertTrue(own)
                self.assertEqual({hit["project_id"] for hit in own}, {PROJECT})
                self.assertTrue(all(hit["dataset_kind"] == "live" for hit in own))
                other = LiveRetriever(self.db, mode=mode).retrieve(
                    "payment credential rotation schedule", scope=other_scope, top_k=10)
                self.assertTrue(other)
                self.assertEqual({hit["project_id"] for hit in other}, {OTHER})


if __name__ == "__main__":
    unittest.main()
