"""Offline verification using a real disposable Git repository and SQLite files."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
from src.evidence import Evidence, EvidenceScope, legacy_evidence, sha256_text
from src.evidence_store import EvidenceStore, bind_legacy_export
from src.ingest import ingest_all, ingest_csv, ingest_directory, ingest_git_history, normalize_record
from src.retrieval import HybridRetriever, get_evidence, retrieve_evidence
from src.skills.registry import SkillRegistry
from src.skills.memory_skill import MemorySkill
from src.skills.problem_skill import ProblemSkill
from src.skills.document_skill import DocumentSkill
from src.agents.react_agent import ReActAgent, ToolExecutor
from src.agents.coordinator_agent import CoordinatorAgent
from src.agents.answer_agent import AnswerAgent

from offline_env import OFFLINE_PROVIDER_ENV

OFFLINE = {**OFFLINE_PROVIDER_ENV}


def record(content="decision B", project_id="project-a", record_id="decision.md", source="file:///test/repo"):
    return normalize_record(content, source, "markdown", record_id=record_id, project_id=project_id)


class EvidenceModelTests(unittest.TestCase):
    def test_stable_identity_and_content_hash(self):
        first, second = record(), record("decision C")
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        self.assertEqual(first["content_hash"], sha256_text(first["content"]))
        self.assertNotEqual(first["content_hash"], second["content_hash"])
        self.assertNotEqual(first["evidence_id"], record(project_id="project-b")["evidence_id"])
        self.assertNotEqual(first["evidence_id"], record(source="file:///other/repo")["evidence_id"])

    def test_validation_hash_identity_time_uri_extra(self):
        for change in ({"content": "tampered"}, {"project_id": "other"},
                       {"event_time": "2026-01-01T00:00:00"}, {"source_uri": "missing-scheme"},
                       {"dataset_kind": "mixed"}, {"unrecognized": True}, {"content": ""}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                Evidence.model_validate({**record(), **change})
        self.assertIsNone(record()["event_time"])
        self.assertIsNotNone(record()["ingested_at"])

    def test_live_scope_requires_project(self):
        for args in ({"dataset_kind": "live"}, {"dataset_kind": "live", "project_id": " "}):
            with self.assertRaises(ValidationError):
                EvidenceScope(**args)

    def test_identity_stable_across_process_hash_seeds(self):
        code = "from src.evidence import stable_evidence_id; print(stable_evidence_id('p','live','document','file:///r','a.md'))"
        values = [subprocess.check_output([os.sys.executable, "-c", code], env={**os.environ, "PYTHONHASHSEED": seed}, text=True).strip()
                  for seed in ("1", "2")]
        self.assertEqual(values[0], values[1])

    def test_legacy_missing_fields_are_explicit(self):
        evidence = legacy_evidence({"content": "old note", "source": "claimed.json", "record": 9})
        self.assertIsNone(evidence.source_uri)
        self.assertIsNone(evidence.event_time)
        self.assertIsNone(evidence.ingested_at)
        self.assertIsNone(evidence.original_record_id)
        self.assertEqual(evidence.source_type, "unknown")
        self.assertEqual(evidence.metadata["legacy_source_label"], "claimed.json")
        self.assertEqual(evidence.metadata["provenance_status"], "unverified_legacy")
        self.assertIn("source_uri", evidence.metadata["missing_fields"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = EvidenceStore(self.root / "evidence.sqlite3")
        self.scope = EvidenceScope(dataset_kind="live", project_id="project-a")

    def test_read_does_not_create_storage(self):
        self.assertEqual(self.store.list(self.scope), [])
        self.assertIsNone(self.store.get(record()["evidence_id"], self.scope))
        self.assertFalse(self.store.path.exists())

    def test_idempotent_and_append_only_revisions(self):
        first = record()
        second = record("decision C")
        self.assertEqual(self.store.append([first]), 1)
        self.assertEqual(self.store.append([record()]), 0)
        self.assertEqual(self.store.append([second]), 1)
        self.assertEqual(len(self.store.list(self.scope)), 1)
        self.assertEqual(self.store.get(first["evidence_id"], self.scope).content, "decision C")
        self.assertEqual(self.store.get(first["evidence_id"], self.scope, first["content_hash"]).content, "decision B")
        # Reverting to an older body is a new observation, not a lost update.
        self.assertEqual(self.store.append([first]), 1)
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence_observations").fetchone()[0], 3)

    def test_concurrent_imports_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            counts = list(pool.map(lambda _: self.store.append([record()]), range(4)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(self.store.list(self.scope)), 1)

    def test_invalid_batch_is_atomic(self):
        self.store.append([record()])
        with self.assertRaises(ValidationError):
            self.store.append([record(record_id="new.md"), {**record(), "content_hash": "0" * 64}])
        self.assertEqual(len(self.store.list(self.scope)), 1)

    def test_project_and_dataset_filtered_on_read(self):
        self.store.append([record(), record(project_id="project-b")])
        first = self.store.list(self.scope)[0]
        self.assertIsNone(self.store.get(first.evidence_id, EvidenceScope(project_id="project-b", dataset_kind="live")))
        self.assertEqual(self.store.list(EvidenceScope()), [])

    def test_existing_json_untouched_dry_run_and_explicit_binding(self):
        path = self.root / "ingested_records.json"
        original = json.dumps([{"id": "old", "source": "git:repo", "content": "old record"}])
        path.write_text(original, encoding="utf-8")
        self.assertEqual(retrieve_evidence("old", scope=self.scope, db_path=self.store.path), [])
        self.assertEqual(bind_legacy_export(path, project_id="project-a", db_path=self.store.path)["records"], 1)
        self.assertFalse(self.store.path.exists())
        bind_legacy_export(path, project_id="project-a", db_path=self.store.path, dry_run=False)
        hit = retrieve_evidence("old", scope=self.scope, db_path=self.store.path)[0]
        self.assertTrue(hit["metadata"]["legacy"])
        self.assertIsNone(hit["source_uri"])
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_scoped_live_json_read_without_db_or_migration(self):
        (self.root / "ingested_records.json").write_text(json.dumps([record()]), encoding="utf-8")
        result = retrieve_evidence("decision", scope=self.scope, db_path=self.store.path)
        self.assertEqual(len(result), 1)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(get_evidence(result[0]["evidence_id"], scope=self.scope, db_path=self.store.path).content, "decision B")

    def test_document_paths_and_updates(self):
        for name in ("a", "b"):
            (self.root / name).mkdir()
            (self.root / name / "design.md").write_text("Initial decision B", encoding="utf-8")
        first = ingest_directory(self.root, project_id="project-a")
        self.assertEqual(len({r["evidence_id"] for r in first}), 2)
        self.assertTrue(all(r["event_time"] is None for r in first))
        self.store.append(first)
        (self.root / "a" / "design.md").write_text("Later decision C", encoding="utf-8")
        second = ingest_directory(self.root, project_id="project-a")
        self.assertEqual({r["evidence_id"] for r in first}, {r["evidence_id"] for r in second})
        self.assertEqual(self.store.append(second), 1)

    def test_csv_retains_extra_columns(self):
        path = self.root / "issues.csv"
        path.write_text("id,description,status\nCMA-1,decision B,open\n", encoding="utf-8")
        result = ingest_csv(path, project_id="project-a", id_column="id", category="issues")[0]
        self.assertEqual(result["source_type"], "issue")
        self.assertEqual(result["metadata"]["columns"]["status"], "open")


class RealGitEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_temp = tempfile.TemporaryDirectory(prefix="cma-real-git-")
        cls.repo = Path(cls.repo_temp.name)
        cls.git("init")
        cls.git("config", "user.name", "Evidence Test")
        cls.git("config", "user.email", "evidence@example.invalid")
        cls.shas = []
        cls.times = ["2025-01-01T10:00:00+08:00", "2025-02-01T10:00:00+08:00", "2025-03-01T10:00:00+08:00"]
        changes = [("Initial technical options A and B", "Initial design document"),
                   ("Decision: choose B because latency matters", "Decision choose B\n\nLatency evidence approved."),
                   ("Modification: keep B with a revised timeout", "Modify B timeout after measurements")]
        for timestamp, (body, message) in zip(cls.times, changes):
            (cls.repo / "design.md").write_text(body, encoding="utf-8")
            cls.git("add", "design.md")
            cls.git("commit", "-m", message, env={**os.environ, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp})
            cls.shas.append(cls.git("rev-parse", "HEAD").strip())

    @classmethod
    def tearDownClass(cls):
        cls.repo_temp.cleanup()

    @classmethod
    def git(cls, *args, env=None):
        return subprocess.check_output(["git", "-C", str(cls.repo), *args], text=True, encoding="utf-8", env=env, stderr=subprocess.PIPE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-git-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "evidence.sqlite3"
        self.scope = EvidenceScope(dataset_kind="live", project_id="git-project")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {**OFFLINE, "EVIDENCE_DB_PATH": str(self.db)}).start()

    def ingest(self):
        return ingest_all(repo_path=self.repo, project_id=self.scope.project_id, db_path=self.db, output_dir=self.root)

    def test_git_metadata_stable_ids_and_idempotency(self):
        first = ingest_git_history(self.repo, project_id="git-project")
        second = ingest_git_history(self.repo, project_id="git-project")
        self.assertEqual(len(first), 3)
        self.assertEqual([r["evidence_id"] for r in first], [r["evidence_id"] for r in second])
        self.assertEqual({r["original_record_id"] for r in first}, set(self.shas))
        self.assertEqual({r["event_time"] for r in first}, set(self.times))
        self.assertTrue(all(r["locator"]["commit_sha"] == r["original_record_id"] for r in first))
        self.assertTrue(all(r["source_uri"] == self.repo.as_uri() for r in first))
        self.assertTrue(all("design.md" in r["locator"]["files"] for r in first))
        self.assertIn("Latency evidence approved.", next(r for r in first if r["original_record_id"] == self.shas[1])["content"])
        self.assertEqual(self.ingest()["appended_observations"], 3)
        self.assertEqual(self.ingest()["appended_observations"], 0)
        self.assertEqual(len(EvidenceStore(self.db).list(self.scope)), 3)

    def test_live_tool_chain_and_full_refetch(self):
        self.ingest()
        executor = ToolExecutor(scope=self.scope, db_path=self.db)
        result = executor.execute("search_issues", "choose B")
        self.assertTrue(result["evidence"])
        for hit in result["evidence"]:
            self.assertTrue(set(Evidence.model_fields).issubset(hit))
            self.assertEqual(hit["dataset_kind"], "live")
            self.assertEqual(hit["project_id"], "git-project")
            self.assertEqual(hit["score_kind"], "retrieval_relevance")
            refetched = executor.get_evidence(hit["evidence_id"], hit["content_hash"])
            self.assertEqual(refetched.content, hit["content"])
            fresh = ToolExecutor(scope=self.scope, db_path=self.db)
            self.assertEqual(fresh.get_evidence(hit["evidence_id"]).content, hit["content"])

    def test_project_isolation_across_tools_and_workflow(self):
        self.ingest()
        other = EvidenceScope(dataset_kind="live", project_id="unrelated-project")
        registry = SkillRegistry(scope=other, db_path=self.db)
        for name in registry.skills:
            self.assertEqual(registry.get(name).run("decision B")["evidence"], [])
        agent = CoordinatorAgent(scope=other, db_path=self.db, use_llm_routing=False)
        self.assertIn("证据不足", agent.ask("为什么选择B方案"))
        own = ToolExecutor(scope=self.scope, db_path=self.db).execute("search_memory", "decision")
        self.assertTrue(own["evidence"])
        self.assertIsNone(registry.get_evidence(own["evidence"][0]["evidence_id"]))

    def test_fixture_live_isolation(self):
        self.ingest()
        fixture = SkillRegistry()
        fixture_hits = fixture.get("problem").run("CMA-105")
        self.assertTrue(fixture_hits["evidence"])
        self.assertTrue(all(r["dataset_kind"] == "fixture" for r in fixture_hits["evidence"]))
        live = ToolExecutor(scope=self.scope, db_path=self.db).execute("search_memory", "CMA-105")
        self.assertEqual(live["evidence"], [])
        self.assertFalse(any(r["evidence_id"] in {x["evidence_id"] for x in fixture_hits["evidence"]}
                             for r in retrieve_evidence("decision", scope=self.scope, db_path=self.db)))

    def test_scoped_retriever_bypasses_fixture_faiss(self):
        self.ingest()
        with patch.object(HybridRetriever, "dense_search", side_effect=AssertionError("must not read fixture FAISS")):
            hits = HybridRetriever().retrieve("choose B", scope=self.scope, db_path=self.db)
        self.assertTrue(hits)

    def test_existing_export_preserved_and_dry_run_no_write(self):
        output = self.root / "ingested_records.json"
        output.write_text("[]", encoding="utf-8")
        result = ingest_all(repo_path=self.repo, project_id="git-project", db_path=self.db, output_dir=self.root, dry_run=True)
        self.assertEqual(result["total_records"], 3)
        self.assertFalse(self.db.exists())
        self.ingest()
        self.assertEqual(output.read_text(encoding="utf-8"), "[]")
        with self.assertRaises(ValueError):
            ingest_all(repo_path=self.repo, project_id="git-project", db_path=self.db, reindex=True)

    def test_workflow_linear_react_offline_and_api(self):
        self.ingest()
        for use_graph in (True, False):
            agent = CoordinatorAgent(scope=self.scope, db_path=self.db, use_langgraph=use_graph, use_llm_routing=False)
            answer = agent.ask("为什么选择B方案 decision")
            self.assertIn("ev_", answer)
            self.assertIn(self.repo.as_uri(), answer)
            self.assertNotIn("CMA-", answer)
        # Actual no-key fallback, not a simulated online model.
        react = ReActAgent(scope=self.scope, db_path=self.db)
        result = react.run_result("为什么选择B方案 decision")
        self.assertIn("【综合回答】", result.answer)
        self.assertTrue(result.evidence_refs)
        self.assertTrue(result.evidence_refs[0].evidence_id.startswith("ev_"))
        with TestClient(api.app) as client:
            for backend in ("linear", "langgraph", "react"):
                response = client.post("/chat", json={"question": "为什么选择B方案 decision", "backend": backend,
                                                       "scope": self.scope.model_dump()})
                self.assertEqual(response.status_code, 200)
                if backend == "react":
                    self.assertIn("【综合回答】", response.json()["answer"])
                else:
                    self.assertIn("ev_", response.json()["answer"])
            response = client.post("/chat/stream", json={"question": "为什么选择B方案 decision", "scope": self.scope.model_dump()})
            self.assertIn("[DONE]", response.text)
            self.assertNotIn('"type": "error"', response.text)
            self.assertEqual(client.post("/chat", json={"question": "x", "scope": {"dataset_kind": "live"}}).status_code, 422)

    def test_live_cli_and_ingestion_cli_dry_run(self):
        self.ingest()
        result = subprocess.run([os.sys.executable, "-X", "utf8", "main.py", "--linear", "--dataset-kind", "live", "--project-id", "git-project"],
                                input="为什么选择B方案 decision\nexit\n", text=True, encoding="utf-8", capture_output=True, check=True,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                     "PYTHON_DOTENV_DISABLED": "1"}, timeout=20)
        self.assertIn("ev_", result.stdout)
        untouched = self.root / "dry-run.sqlite3"
        subprocess.run([os.sys.executable, "-X", "utf8", "-m", "src.ingest", "--git", str(self.repo),
                        "--project-id", "git-project", "--db", str(untouched), "--dry-run"],
                       capture_output=True, check=True, timeout=20)
        self.assertFalse(untouched.exists())

    def test_scripted_react_observation_preserves_identity_offline_only(self):
        self.ingest()
        agent = ReActAgent(scope=self.scope, db_path=self.db)
        agent.api_key = "offline-script"
        replies = iter(['{"action":"tool_call","tool":"search_memory","query":"choose B"}',
                        '{"action":"answer","summary":"See retrieved decision evidence."}'])
        observed = []
        def scripted(messages):
            observed.append(json.loads(json.dumps(messages)))
            return next(replies)
        with patch.object(agent, "_call_llm", side_effect=scripted):
            answer = agent.run("why B")
        self.assertIn("ev_", observed[-1][-1]["content"])
        self.assertIn("【综合回答】", answer)
        self.assertNotIn("【证据】", answer.split("【综合回答】")[0])


class LegacySkillCompatibilityTests(unittest.TestCase):
    def test_direct_legacy_skills_return_canonical_fields(self):
        for skill, query in ((MemorySkill(), "历史上做过哪些技术决策"),
                             (ProblemSkill(), "CMA-105"), (DocumentSkill(), "架构")):
            result = skill.run(query)
            self.assertTrue(result["evidence"])
            for item in result["evidence"]:
                self.assertTrue(set(Evidence.model_fields).issubset(item))
                self.assertTrue(item["metadata"]["legacy"])

    def test_tool_does_not_truncate_original_content(self):
        with tempfile.TemporaryDirectory(prefix="cma-full-content-") as directory:
            store = EvidenceStore(Path(directory) / "e.sqlite3")
            full = record("decision " + "x" * 1500)
            store.append([full])
            executor = ToolExecutor(scope=EvidenceScope(dataset_kind="live", project_id="project-a"), db_path=store.path)
            hit = executor.execute("search_docs", "decision")["evidence"][0]
            self.assertEqual(hit["content"], full["content"])
            self.assertEqual(executor.get_evidence(hit["evidence_id"]).content, full["content"])

    def test_composition_rejects_mixed_projects(self):
        evidence = [Evidence.model_validate(record()).as_hit(), Evidence.model_validate(record(project_id="other")).as_hit()]
        with self.assertRaises(ValueError):
            AnswerAgent().compose("x", ["memory"], {"memory": {"evidence": evidence}})

    def test_legacy_status_visible_to_model_and_user(self):
        evidence = legacy_evidence({"content": "old decision note", "source": "claimed.json"}).as_hit()
        agent = AnswerAgent()
        agent.siliconflow_api_key = ""
        self.assertIn("legacy_unverified=True", agent._evidence_for_prompt([evidence]))
        result = agent.compose("decision", ["memory"], {"memory": {"evidence": [evidence]}})
        self.assertIn("provenance=unverified_legacy", result)


if __name__ == "__main__":
    unittest.main()
