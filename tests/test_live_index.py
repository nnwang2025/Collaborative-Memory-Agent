"""Live chunk, index lifecycle, hybrid retrieval, and ReAct integration tests."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

from src.agents.react_agent import ToolExecutor
from src.evidence import EvidenceScope
from src.index.builder import IndexBuilder
from src.index.chunks import build_chunks
from src.index.retriever import LiveRetriever
from src.index.store import IndexStore
from src.investigation import EvidenceLedger
from src.investigation_context import Excerpt
from src.sync.models import SourceSpec
from src.sync.service import SyncService


class HashEmbedder:
    """Offline deterministic embedder used only by these lifecycle tests."""

    model_name = "hash-test-embedder"

    def encode(self, texts, normalize_embeddings=True):
        rows = []
        for text in texts:
            vector = np.zeros(16, dtype=np.float32)
            for char in text:
                vector[ord(char) % 16] += 1.0
            norm = float(np.linalg.norm(vector)) or 1.0
            rows.append(vector / norm if normalize_embeddings else vector)
        return np.vstack(rows)


class FailingEmbedder(HashEmbedder):
    def encode(self, texts, normalize_embeddings=True):
        raise RuntimeError("injected embedding failure")


class LiveIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-v2-index-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.db = self.root / "evidence.sqlite3"
        self.scope = EvidenceScope(project_id="index-project", dataset_kind="live")
        self.service = SyncService(self.db)
        self.source = self.service.registry.register(SourceSpec(
            project_id=self.scope.project_id, dataset_kind="live",
            connector_type="directory", source_uri=self.docs.as_uri(),
            config={"root_path": str(self.docs)},
        ))
        self.write("deploy.md",
                   "# Deployment policy\n"
                   "The service uses SQLite tombstones for deleted evidence.\n\n"
                   "## Retrieval\n"
                   "Hybrid retrieval combines BM25 exact terms with dense vectors.\n")
        self.write("search.md",
                   "# Search notes\n"
                   "Semantic rewrites need dense retrieval while identifiers need exact matching.\n")
        self.service.sync(self.source.source_id)
        self.builder = IndexBuilder(self.db, embedder=HashEmbedder())

    def write(self, name, text):
        path = self.docs / name
        path.write_text(text, encoding="utf-8")

    def test_chunks_have_stable_identity_and_exact_parent_locators(self):
        evidence = {r.original_record_id: r for r in self.service.store.list(self.scope)}
        chunks = build_chunks(evidence["deploy.md"])
        self.assertGreaterEqual(len(chunks), 1)
        rebuilt = build_chunks(evidence["deploy.md"])
        self.assertEqual([c["chunk_id"] for c in chunks], [c["chunk_id"] for c in rebuilt])
        for chunk in chunks:
            self.assertEqual(chunk["evidence_id"], evidence["deploy.md"].evidence_id)
            self.assertEqual(chunk["content_hash"], evidence["deploy.md"].content_hash)
            self.assertEqual(chunk["locator_status"], "exact")
            self.assertEqual(evidence["deploy.md"].content[
                chunk["char_start"]:chunk["char_end"]], chunk["text"])
            self.assertEqual(chunk["line_start"],
                             evidence["deploy.md"].content.count(
                                 "\n", 0, chunk["char_start"]) + 1)

    def test_build_verify_rollback_and_stale_detection(self):
        first = self.builder.build(self.scope)
        self.assertEqual(first["status"], "built")
        old_version = first["version"]["index_version"]
        self.assertTrue(self.builder.verify(self.scope)["ok"])

        self.write("deploy.md", "# Deployment policy v2\nTombstones changed after review.\n")
        self.service.sync(self.source.source_id)
        self.assertTrue(self.builder.status(self.scope)["stale"])
        fallback = LiveRetriever(self.db, embedder=HashEmbedder(), mode="hybrid").retrieve(
            "tombstones", scope=self.scope, top_k=3)
        self.assertTrue(fallback)
        self.assertEqual(fallback[0]["retrieval_mode"], "bm25_fallback")
        self.assertIn("stale_index_source_revision", fallback[0]["fallback_reason"])

        second = self.builder.build(self.scope)
        new_version = second["version"]["index_version"]
        self.assertNotEqual(old_version, new_version)
        self.assertTrue(self.builder.verify(self.scope)["ok"])
        self.builder.store.activate(old_version)
        self.assertEqual(self.builder.store.active_version(self.scope)["index_version"],
                         old_version)
        self.builder.store.activate(new_version)
        self.assertEqual(self.builder.store.active_version(self.scope)["index_version"],
                         new_version)

    def test_hybrid_actually_runs_both_channels_and_hydrates_parent(self):
        self.builder.build(self.scope)
        retriever = LiveRetriever(self.db, embedder=HashEmbedder(), mode="hybrid")
        hits = retriever.retrieve("dense retrieval exact terms", scope=self.scope, top_k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["retrieval_mode"], "hybrid")
        self.assertIsNone(hits[0]["fallback_reason"])
        self.assertIn("dense", hits[0]["channels"])
        self.assertIn("bm25", hits[0]["channels"])
        self.assertGreater(hits[0]["rrf_score"], 0)
        self.assertEqual(hits[0]["score_kind"], "retrieval_relevance")
        self.assertTrue(hits[0]["content"])
        for chunk in hits[0]["matched_chunks"]:
            self.assertEqual(chunk["locator_status"], "exact")
            parent = self.service.store.get(hits[0]["evidence_id"], self.scope,
                                            hits[0]["content_hash"])
            self.assertEqual(parent.content[chunk["char_start"]:chunk["char_end"]],
                             chunk["text"])

    def test_deleted_evidence_is_absent_and_old_index_cannot_resurrect_it(self):
        self.builder.build(self.scope)
        (self.docs / "search.md").unlink()
        report = self.service.sync(self.source.source_id)
        self.assertEqual(report.deleted, 1)
        retriever = LiveRetriever(self.db, embedder=HashEmbedder(), mode="hybrid")
        hits = retriever.retrieve("semantic rewrites dense retrieval",
                                  scope=self.scope, top_k=10)
        self.assertNotIn("search.md", {h["original_record_id"] for h in hits})
        # Even after a rebuild, the deleted record cannot enter the index.
        self.builder.build(self.scope, force=True)
        hits = retriever.retrieve("semantic rewrites dense retrieval",
                                  scope=self.scope, top_k=10)
        self.assertNotIn("search.md", {h["original_record_id"] for h in hits})

    def test_checksum_failure_and_missing_index_fall_back_explicitly(self):
        self.builder.build(self.scope)
        version = self.builder.store.active_version(self.scope)
        with open(Path(version["path"]) / "faiss.index", "ab") as handle:
            handle.write(b"corruption")
        hits = LiveRetriever(self.db, embedder=HashEmbedder(), mode="hybrid").retrieve(
            "tombstones", scope=self.scope, top_k=3)
        self.assertEqual(hits[0]["retrieval_mode"], "bm25_fallback")
        self.assertIn("index_checksum_mismatch", hits[0]["fallback_reason"])
        self.assertEqual(hits[0]["index_state"], "unusable")

        other_db = self.root / "no-index.sqlite3"
        other_service = SyncService(other_db)
        source = other_service.registry.register(SourceSpec(
            project_id="other", dataset_kind="live", connector_type="directory",
            source_uri=self.docs.as_uri(), config={"root_path": str(self.docs)}))
        other_service.sync(source.source_id)
        fallback = LiveRetriever(other_db, embedder=HashEmbedder(), mode="dense").retrieve(
            "tombstones", scope=EvidenceScope(project_id="other", dataset_kind="live"),
            top_k=3)
        self.assertTrue(fallback)
        self.assertEqual(fallback[0]["retrieval_mode"], "bm25_fallback")
        self.assertIn("no_active_index", fallback[0]["fallback_reason"])

    def test_failed_build_does_not_damage_active_index(self):
        built = self.builder.build(self.scope)
        active = built["version"]["index_version"]
        failing = IndexBuilder(self.db, embedder=FailingEmbedder())
        with self.assertRaises(RuntimeError):
            failing.build(self.scope, force=True)
        self.assertEqual(self.builder.store.active_version(self.scope)["index_version"],
                         active)
        self.assertTrue(self.builder.verify(self.scope)["ok"])

    def test_interrupted_build_is_recovered(self):
        store = IndexStore(self.db)
        store.start_version(
            self.scope, version_id="idx_interrupted", embedding_model="hash-test-embedder",
            embedding_dim=16, chunker_version="v2.0", source_revision=0,
            evidence_count=0, chunk_count=0, path=str(self.root / "never"),
        )
        result = self.builder.recover(self.scope)
        self.assertIn("idx_interrupted", result["failed_versions"])
        self.assertEqual(store.get_version("idx_interrupted")["status"], "failed")
        self.assertIsNone(store.active_version(self.scope))

    def test_scope_isolation_across_projects(self):
        self.builder.build(self.scope)
        other_scope = EvidenceScope(project_id="other-project", dataset_kind="live")
        other_docs = self.root / "other"
        other_docs.mkdir()
        (other_docs / "other.md").write_text(
            "The other project also writes about tombstones and retrieval.",
            encoding="utf-8")
        other_db = self.root / "other.sqlite3"
        other = SyncService(other_db)
        source = other.registry.register(SourceSpec(
            project_id=other_scope.project_id, dataset_kind="live",
            connector_type="directory", source_uri=other_docs.as_uri(),
            config={"root_path": str(other_docs)}))
        other.sync(source.source_id)
        other_builder = IndexBuilder(other_db, embedder=HashEmbedder())
        other_builder.build(other_scope)
        hits = LiveRetriever(self.db, embedder=HashEmbedder(), mode="hybrid").retrieve(
            "tombstones retrieval", scope=self.scope, top_k=10)
        self.assertEqual({h["project_id"] for h in hits}, {self.scope.project_id})
        other_hits = LiveRetriever(other_db, embedder=HashEmbedder(), mode="hybrid").retrieve(
            "tombstones", scope=other_scope, top_k=10)
        self.assertEqual({h["project_id"] for h in other_hits}, {other_scope.project_id})

    def test_react_tool_executor_uses_chunk_excerpt_and_ledger_refetch(self):
        self.builder.build(self.scope)
        env = {"LIVE_RETRIEVAL_MODE": "hybrid"}
        with patch.dict(os.environ, env), patch(
                "src.index.retriever._get_embedder", return_value=HashEmbedder()):
            executor = ToolExecutor(scope=self.scope, db_path=self.db)
            result = executor.execute("search_docs", "tombstones deleted evidence")
            self.assertEqual(result["evidence"][0]["retrieval_mode"], "hybrid")
            item = result["evidence"][0]
            self.assertTrue(item["matched_chunks"])
            excerpt = Excerpt.model_validate(item["excerpt"])
            parent = executor.get_evidence(item["evidence_id"], item["content_hash"])
            excerpt.verify(parent)
            ledger = EvidenceLedger(self.scope)
            added = ledger.register(result["evidence"], "search_docs")
            self.assertTrue(added)
            self.assertEqual(ledger.validate_all(executor.get_evidence), [])

    def test_new_decision_cannot_use_tombstoned_evidence(self):
        from datetime import datetime
        from src.ingest import normalize_record
        from src.evidence_store import EvidenceStore
        from src.investigation import Claim, EvidenceRef, InvestigationResult
        from src.memory.decisions import DecisionStore

        evidence_path = self.root / "decision-evidence.sqlite3"
        decision_path = self.root / "decisions.sqlite3"
        scope = EvidenceScope(project_id="decision-live", dataset_kind="live")
        record = normalize_record("Team approved plan B.", "file:///decision", "issues", "I-1",
                                  project_id=scope.project_id, source_type="issue")
        EvidenceStore(evidence_path).append([record])
        decisions = DecisionStore(decision_path, evidence_db_path=evidence_path)
        topic = decisions.create("plan", scope)
        ref = EvidenceRef(evidence_id=record["evidence_id"],
                          content_hash=record["content_hash"])
        result = InvestigationResult(
            run_id="run", scope=scope, answer="a", termination_reason="sufficient",
            evidence_refs=[ref], claims=[Claim(text="plan B", refs=[ref])])
        decisions.propose(topic["decision_id"], result, "Use B")
        with closing(sqlite3.connect(evidence_path)) as connection, connection:
            connection.execute(
                "UPDATE evidence_current SET state='deleted' WHERE evidence_id=?",
                (record["evidence_id"],))
        with self.assertRaises(ValueError):
            decisions.propose(topic["decision_id"], result, "Use B again")
        snapshot = decisions.snapshot(topic["decision_id"], scope)
        self.assertEqual(len(snapshot["proposals"]), 1)
        self.assertFalse(snapshot["current_confirmed"])


if __name__ == "__main__":
    unittest.main()
