"""End-to-end V2 acceptance: real sources -> sync -> index -> retrieval -> ReAct.

Everything here is offline.  The test uses a deterministic embedding stub for
the index; production builds use the locally cached bge-small model.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from benchmark.v2.dataset import (
    add_release_commit, apply_deletion, apply_update, create_dataset,
)
from src.agents.react_agent import ReActAgent
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.index.builder import IndexBuilder
from src.index.retriever import LiveRetriever
from src.retrieval import retrieve_evidence
from src.sync.models import SourceSpec
from src.sync.service import SyncService

PROJECT = "v2-e2e"
OTHER = "v2-e2e-other"
SCOPE = EvidenceScope(project_id=PROJECT, dataset_kind="live")


class HashEmbedder:
    model_name = "hash-e2e-embedder"
    dimension = 32

    def encode(self, texts, normalize_embeddings=True):
        rows = []
        for text in texts:
            vector = np.zeros(self.dimension, dtype=np.float32)
            for char in text:
                vector[ord(char) % self.dimension] += 1.0
            norm = float(np.linalg.norm(vector)) or 1.0
            rows.append(vector / norm if normalize_embeddings else vector)
        return np.vstack(rows)


class V2EndToEndAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-v2-e2e-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = create_dataset(self.root / "corpus")
        self.db = self.root / "evidence.sqlite3"
        self.service = SyncService(self.db)
        self.embedder = HashEmbedder()
        self.builder = IndexBuilder(self.db, embedder=self.embedder)

    def _sources(self):
        definitions = {
            "directory": SourceSpec(
                project_id=PROJECT, dataset_kind="live", connector_type="directory",
                source_uri=self.data["docs"].as_uri(),
                config={"root_path": str(self.data["docs"])}),
            "csv": SourceSpec(
                project_id=PROJECT, dataset_kind="live", connector_type="csv",
                source_uri=self.data["issues_csv"].as_uri(),
                config={"path": str(self.data["issues_csv"]), "id_column": "id",
                        "content_column": "description", "category": "issue",
                        "time_column": "updated_at"}),
            "json": SourceSpec(
                project_id=PROJECT, dataset_kind="live", connector_type="json",
                source_uri=self.data["events_json"].as_uri(),
                config={"path": str(self.data["events_json"]), "record_path": "records",
                        "id_field": "id", "content_fields": ["title", "body"],
                        "time_field": "event_time", "category": "document"}),
            "jsonl": SourceSpec(
                project_id=PROJECT, dataset_kind="live", connector_type="json",
                source_uri=self.data["stream_jsonl"].as_uri(),
                config={"path": str(self.data["stream_jsonl"]), "id_field": "id",
                        "content_fields": ["message"], "time_field": "ts",
                        "category": "logs"}),
            "git": SourceSpec(
                project_id=PROJECT, dataset_kind="live", connector_type="git",
                source_uri=self.data["git_repo"].as_uri(),
                config={"repo_path": str(self.data["git_repo"]), "branch": "main"}),
        }
        return {name: self.service.registry.register(spec)
                for name, spec in definitions.items()}

    def test_all_sources_sync_index_retrieval_and_scripted_agent(self):
        sources = self._sources()
        reports = {name: self.service.sync(source.source_id)
                   for name, source in sources.items()}
        self.assertEqual(
            {name for name, report in reports.items() if report.status == "succeeded"},
            {"directory", "csv", "json", "jsonl", "git"})
        self.assertEqual(sum(report.added for report in reports.values()), 13)

        records = {record.original_record_id: record
                   for record in self.service.store.list(SCOPE)}
        for identifier in ("architecture.md", "ISSUE-101", "ISSUE-102",
                           "JSON-1", "JSON-2", "LOG-1", "LOG-2"):
            self.assertIn(identifier, records)
        for sha in self.data["commits"]:
            self.assertIn(sha, records)
        architecture = records["architecture.md"]

        # Incremental CSV update keeps one identity and two versions.
        apply_update(self.data)
        updated = self.service.sync(sources["csv"].source_id)
        self.assertEqual((updated.updated, updated.unchanged), (1, 1))
        issue = next(record for record in self.service.store.list(SCOPE)
                     if record.original_record_id == "ISSUE-102")
        versions = self.service.store.list_versions(issue.evidence_id, SCOPE)
        self.assertEqual(len(versions), 2)
        self.assertEqual({version.evidence_id for version in versions}, {issue.evidence_id})

        # Git cursor is incremental and picks up exactly the new commit.
        sha = add_release_commit(self.data)
        incremental = self.service.sync(sources["git"].source_id)
        self.assertEqual(incremental.mode, "incremental")
        self.assertEqual((incremental.added, incremental.updated), (1, 0))
        self.assertTrue(self.service.store.get(
            next(record for record in self.service.store.list(SCOPE)
                 if record.original_record_id == sha).evidence_id, SCOPE))

        built = self.builder.build(SCOPE)
        self.assertEqual(built["status"], "built")
        self.assertTrue(self.builder.verify(SCOPE)["ok"])

        other_docs = self.root / "other"
        other_docs.mkdir()
        (other_docs / "notes.md").write_text(
            "Hybrid retrieval tombstones rollback from the other project.",
            encoding="utf-8")
        other_service = SyncService(self.db)
        other_source = other_service.registry.register(SourceSpec(
            project_id=OTHER, dataset_kind="live", connector_type="directory",
            source_uri=other_docs.as_uri(), config={"root_path": str(other_docs)}))
        other_service.sync(other_source.source_id)

        with patch.dict(os.environ, {"LIVE_RETRIEVAL_MODE": "hybrid"}), patch(
                "src.index.retriever._get_embedder", return_value=self.embedder):
            hits = retrieve_evidence(
                "ARCH-42 tombstone lifecycle evidence", scope=SCOPE, db_path=self.db,
                mode="hybrid", top_k=5)
            self.assertTrue(hits)
            self.assertEqual({hit["retrieval_mode"] for hit in hits}, {"hybrid"})
            self.assertTrue(any("dense" in hit["channels"] for hit in hits))
            self.assertTrue(any("bm25" in hit["channels"] for hit in hits))
            self.assertEqual({hit["project_id"] for hit in hits}, {PROJECT})
            top = hits[0]
            self.assertTrue(top["content"])
            for chunk in top["matched_chunks"]:
                if chunk["locator_status"] != "exact":
                    # The legacy markdown chunker normalizes heading blanks;
                    # such chunks are explicitly marked unavailable and never
                    # become a validated excerpt.
                    self.assertIsNone(chunk["char_start"])
                    continue
                parent = self.service.store.get(top["evidence_id"], SCOPE, top["content_hash"])
                self.assertEqual(parent.content[chunk["char_start"]:chunk["char_end"]],
                                 chunk["text"])

            obsolete = next(record for record in self.service.store.list(SCOPE)
                            if record.original_record_id == "obsolete.md")
            apply_deletion(self.data)
            deleted = self.service.sync(sources["directory"].source_id)
            self.assertEqual(deleted.deleted, 1)
            # The stale index forces an explicit BM25 fallback.  The deleted
            # record must not reappear; the deleted-only query may now be empty.
            stale = retrieve_evidence(
                "obsolete purge evidence seven days", scope=SCOPE, db_path=self.db,
                mode="hybrid", top_k=5)
            self.assertEqual(stale.diagnostics["effective_mode"], "bm25_fallback")
            self.assertIn("stale_index_source_revision",
                          stale.diagnostics["fallback_reason"])
            self.assertNotIn("obsolete.md", {hit["original_record_id"] for hit in stale})
            fallback_hit = retrieve_evidence(
                "tombstone lifecycle evidence", scope=SCOPE, db_path=self.db,
                mode="hybrid", top_k=5)
            self.assertEqual(fallback_hit[0]["retrieval_mode"], "bm25_fallback")
            self.assertNotIn("obsolete.md",
                             {hit["original_record_id"] for hit in fallback_hit})
            historical = self.service.store.get(
                obsolete.evidence_id, SCOPE, obsolete.content_hash)
            self.assertIsNotNone(historical)
            self.assertEqual(historical.content, obsolete.content)

            self.builder.build(SCOPE, force=True)
            self.assertTrue(self.builder.verify(SCOPE)["ok"])
            fresh = retrieve_evidence(
                "obsolete purge evidence seven days", scope=SCOPE, db_path=self.db,
                mode="hybrid", top_k=5)
            self.assertNotIn("obsolete.md", {hit["original_record_id"] for hit in fresh})

            # Scripted ReAct run: tool action first, then a validated finish.
            with patch("src.agents.react_agent._resolve_api_key", return_value="test-key"), patch(
                    "src.agents.answer_agent.resolve_api_key", return_value=""):
                agent = ReActAgent(max_iterations=4, scope=SCOPE, db_path=self.db)
            agent.api_key = "test-key"
            state = {"calls": 0}

            def scripted(messages):
                state["calls"] += 1
                if state["calls"] == 1:
                    return json.dumps({
                        "action": "tool_call", "tool": "search_docs",
                        "query": "ARCH-42 SQLite tombstones deleted evidence record",
                    })
                for message in reversed(messages):
                    if message.get("role") != "user":
                        continue
                    try:
                        observation = json.loads(message["content"])
                    except (TypeError, ValueError):
                        continue
                    for item in observation.get("evidence", []):
                        if (item.get("evidence_id"), item.get("content_hash")) != (
                                architecture.evidence_id, architecture.content_hash):
                            continue
                        parent = agent.executor.get_evidence(
                            item["evidence_id"], item["content_hash"])
                        line = next(line for line in parent.content.splitlines()
                                    if "ARCH-42" in line)
                        return json.dumps({
                            "action": "finish",
                            "claims": [{
                                "text": line, "kind": "historical",
                                "refs": [{"evidence_id": parent.evidence_id,
                                          "content_hash": parent.content_hash}],
                            }],
                            "gaps": [], "conflicts": [],
                        })
                return json.dumps({"action": "finish", "claims": [],
                                   "gaps": ["architecture evidence was not retrieved"]})

            agent._call_llm = scripted
            result = agent.run_result(
                "What does ARCH-42 say about SQLite tombstones for deleted evidence?")
            self.assertEqual(result.mode, "react")
            self.assertTrue(result.evidence_refs)
            self.assertTrue(result.claims, result.answer + "\nGAPS:" + "\n".join(result.gaps))
            self.assertEqual(result.claims[0].verification_status, "supported_extract")
            self.assertIn("ARCH-42", result.claims[0].text)
            self.assertIn("ARCH-42", result.answer)
            self.assertNotIn("obsolete", result.answer)
            for ref in result.evidence_refs:
                self.assertIsNotNone(self.service.store.get(
                    ref.evidence_id, SCOPE, ref.content_hash))

            # Explicit fallback when the active index disappears.
            active = self.builder.store.active_version(SCOPE)
            hidden = Path(active["path"]).with_name(Path(active["path"]).name + ".hidden")
            os.replace(active["path"], hidden)
            try:
                fallback = LiveRetriever(
                    self.db, embedder=self.embedder, mode="hybrid").retrieve(
                    "tombstones", scope=SCOPE, top_k=3)
            finally:
                os.replace(hidden, active["path"])
            self.assertEqual(fallback[0]["retrieval_mode"], "bm25_fallback")
            self.assertEqual(fallback[0]["index_state"], "unusable")
            self.assertIn("index_manifest_missing", fallback[0]["fallback_reason"])

            # Empty-candidate path through ReAct: the tool must return no
            # evidence, disclose why, and the run must finish as insufficient.
            empty_state = {"calls": 0, "observation": None}

            def empty_scripted(messages):
                empty_state["calls"] += 1
                if empty_state["calls"] == 1:
                    return json.dumps({
                        "action": "tool_call", "tool": "search_docs",
                        "query": "zzzz-no-match-qqq",
                    })
                for message in reversed(messages):
                    if message.get("role") != "user":
                        continue
                    try:
                        observation = json.loads(message["content"])
                    except (TypeError, ValueError):
                        continue
                    if "evidence" in observation:
                        empty_state["observation"] = observation
                        break
                return json.dumps({
                    "action": "finish", "claims": [],
                    "gaps": ["检索未返回可引用证据，证据不足。"],
                })

            agent._call_llm = empty_scripted
            with patch.dict(os.environ, {"LIVE_RETRIEVAL_MODE": "hybrid"}), patch(
                    "src.index.retriever._get_embedder", return_value=self.embedder):
                empty_result = agent.run_result("Find zzzz-no-match-qqq")
            self.assertEqual(empty_result.mode, "react")
            self.assertEqual(empty_result.termination_reason, "insufficient")
            self.assertFalse(empty_result.claims)
            self.assertTrue(empty_result.gaps)
            self.assertIn("证据不足", empty_result.answer)
            self.assertIsNotNone(empty_state["observation"])
            self.assertEqual(empty_state["observation"]["evidence"], [])
            diagnostics = empty_state["observation"]["retrieval_diagnostics"]
            self.assertEqual(diagnostics["effective_mode"], "hybrid")
            self.assertEqual(diagnostics["index_state"], "active")
            self.assertGreater(diagnostics["candidate_count"], 0)
            self.assertEqual(diagnostics["accepted_count"], 0)
            self.assertTrue(diagnostics["empty_after_filtering"])
            self.assertEqual(diagnostics["empty_reason"], "all_candidates_filtered")
            self.assertTrue(any("相关性阈值" in gap for gap in empty_result.gaps))


if __name__ == "__main__":
    unittest.main()
