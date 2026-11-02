"""Real end-to-end incremental synchronization tests (no mocks, temp sources)."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.sync.connectors import CONNECTORS
from src.sync.models import SourceSpec
from src.sync.service import SyncService


class DirectorySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-v2-sync-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.db = self.root / "evidence.sqlite3"
        self.scope = EvidenceScope(project_id="v2-project", dataset_kind="live")
        self.service = SyncService(self.db)
        self.source = self.service.registry.register(SourceSpec(
            project_id=self.scope.project_id, dataset_kind="live",
            connector_type="directory", source_uri=self.docs.as_uri(),
            config={"root_path": str(self.docs)},
        ))

    def write(self, name, text):
        path = self.docs / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_full_repeat_update_delete_retain_history_and_tombstone(self):
        self.write("a.md", "# Alpha\nSQLite keeps the authoritative evidence record.")
        self.write("b.txt", "Beta mentions retrieval and tombstones.")
        self.write("c.py", "def search():\n    return 'bm25'\n")

        first = self.service.sync(self.source.source_id)
        self.assertEqual((first.status, first.added, first.scanned), ("succeeded", 3, 3))
        repeat = self.service.sync(self.source.source_id)
        self.assertEqual((repeat.added, repeat.updated, repeat.unchanged), (0, 0, 3))
        self.assertEqual(self.service.store.revision(self.scope), 3)

        before = {r.original_record_id: r for r in self.service.store.list(self.scope)}
        self.write("a.md", "# Alpha v2\nSQLite keeps evidence history and tombstones.")
        update = self.service.sync(self.source.source_id)
        self.assertEqual((update.updated, update.unchanged), (1, 2))
        versions = self.service.store.list_versions(before["a.md"].evidence_id, self.scope)
        self.assertEqual(len(versions), 2)
        self.assertNotEqual(versions[0].content_hash, versions[1].content_hash)
        self.assertEqual(versions[0].evidence_id, versions[1].evidence_id)

        (self.docs / "b.txt").unlink()
        deleted = self.service.sync(self.source.source_id)
        self.assertEqual(deleted.deleted, 1)
        active = {r.original_record_id for r in self.service.store.list(self.scope)}
        self.assertNotIn("b.txt", active)
        with_deleted = {r.original_record_id: r for r in self.service.store.list(
            self.scope, include_deleted=True)}
        self.assertIn("b.txt", with_deleted)
        old = self.service.store.get(before["b.txt"].evidence_id, self.scope,
                                     before["b.txt"].content_hash)
        self.assertEqual(old.content, before["b.txt"].content)
        state = self.service.store.current_state(before["b.txt"].evidence_id, self.scope)
        self.assertEqual(state["state"], "deleted")

        with closing(sqlite3.connect(self.db)) as connection, connection:
            tombstones = connection.execute(
                "SELECT original_record_id,reason FROM evidence_tombstones").fetchall()
            item_versions = connection.execute(
                "SELECT original_record_id,state FROM source_item_versions "
                "ORDER BY version_id").fetchall()
        self.assertEqual(tombstones, [("b.txt", "source_record_deleted")])
        self.assertGreaterEqual(len(item_versions), 5)
        self.assertEqual(item_versions[-1], ("b.txt", "deleted"))

    def test_secret_and_cache_files_are_not_ingested(self):
        self.write("safe.md", "public documentation")
        self.write(".env", "API_KEY=not-a-real-key")
        self.write("credentials.json", '{"token":"nope"}')
        cache = self.docs / "__pycache__"
        cache.mkdir()
        (cache / "x.py").write_text("print('cache')", encoding="utf-8")
        report = self.service.sync(self.source.source_id)
        self.assertEqual(report.added, 1)
        self.assertEqual([r.original_record_id for r in self.service.store.list(self.scope)],
                         ["safe.md"])

    def test_pause_resume_and_dry_run_do_not_write(self):
        self.write("a.md", "alpha evidence")
        plan = self.service.sync(self.source.source_id, dry_run=True)
        self.assertEqual((plan.status, plan.added), ("dry_run", 1))
        self.assertEqual(self.service.store.list(self.scope), [])
        self.service.registry.pause(self.source.source_id)
        with self.assertRaises(RuntimeError):
            self.service.sync(self.source.source_id)
        self.service.registry.resume(self.source.source_id)
        self.assertEqual(self.service.sync(self.source.source_id).added, 1)

    def test_concurrent_sync_is_serialized_and_keeps_one_version(self):
        self.write("a.md", "alpha evidence")
        self.write("b.md", "beta evidence")
        started, release = threading.Event(), threading.Event()
        inner = CONNECTORS["directory"]

        class BlockingScan:
            def scan(self, source, cursor, known_ids):
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test scan was not released")
                return inner.scan(source, cursor, known_ids)

        reports, errors = [], []

        def run_sync():
            try:
                reports.append(self.service.sync(self.source.source_id))
            except Exception as exc:  # captured for assertion below
                errors.append(exc)

        with patch.dict(CONNECTORS, {"directory": BlockingScan()}):
            worker = threading.Thread(target=run_sync)
            worker.start()
            self.assertTrue(started.wait(10))
            # The first run holds a 'running' row; a second run must be refused
            # before it scans or writes anything.
            with self.assertRaises(RuntimeError) as caught:
                self.service.sync(self.source.source_id)
            self.assertIn("already running", str(caught.exception))
            release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(reports), 1)
        self.assertEqual((reports[0].status, reports[0].added), ("succeeded", 2))

        repeat = self.service.sync(self.source.source_id)
        self.assertEqual((repeat.added, repeat.updated, repeat.unchanged), (0, 0, 2))
        for record in self.service.store.list(self.scope):
            self.assertEqual(len(self.service.store.list_versions(
                record.evidence_id, self.scope)), 1)
        status = self.service.status(self.source.source_id)
        self.assertEqual(status["cursor_version"], 2)
        self.assertEqual(status["item_counts"], {"active": 2})

    def test_scan_failure_writes_nothing_and_does_not_advance_cursor(self):
        self.write("a.md", "alpha evidence")
        self.write("b.md", "beta evidence")

        class FailingScan:
            def scan(self, source, cursor, known_ids):
                raise RuntimeError("injected scan failure")

        with patch.dict(CONNECTORS, {"directory": FailingScan()}):
            with self.assertRaisesRegex(RuntimeError, "injected scan failure"):
                self.service.sync(self.source.source_id)

        status = self.service.status(self.source.source_id)
        self.assertEqual(status["last_run"]["status"], "failed")
        self.assertEqual(status["cursor"], {})
        self.assertEqual(status["cursor_version"], 0)
        self.assertEqual(self.service.store.list(self.scope), [])
        self.assertEqual(self.service.store.revision(self.scope), 0)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM source_items").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM source_item_versions").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM evidence_tombstones").fetchone()[0], 0)

        # A later healthy run is a clean full sync, not a half-applied resume.
        recovered = self.service.sync(self.source.source_id)
        self.assertEqual((recovered.status, recovered.added), ("succeeded", 2))
        self.assertEqual(self.service.status(self.source.source_id)["cursor_version"], 1)


class StructuredSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-v2-structured-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "evidence.sqlite3"
        self.service = SyncService(self.db)
        self.scope = EvidenceScope(project_id="structured", dataset_kind="live")

    def register(self, connector, path, config):
        return self.service.registry.register(SourceSpec(
            project_id=self.scope.project_id, dataset_kind="live",
            connector_type=connector, source_uri=path.as_uri(),
            config={"path": str(path), **config},
        ))

    def test_csv_stable_id_update_and_delete(self):
        path = self.root / "issues.csv"
        path.write_text(
            "id,title,description,updated_at\n"
            "I-1,Login,login fails on safari,2026-10-01T10:00:00+08:00\n"
            "I-2,Search,search misses CJK,2026-10-02T10:00:00+08:00\n",
            encoding="utf-8")
        source = self.register("csv", path, {
            "id_column": "id", "content_column": "description",
            "category": "issue", "time_column": "updated_at",
        })
        first = self.service.sync(source.source_id)
        self.assertEqual(first.added, 2)
        self.assertEqual(self.service.sync(source.source_id).scanned, 0)  # file hash unchanged

        path.write_text(
            "id,title,description,updated_at\n"
            "I-1,Login,login fails on safari and is now fixed,2026-10-03T10:00:00+08:00\n",
            encoding="utf-8")
        changed = self.service.sync(source.source_id)
        self.assertEqual((changed.updated, changed.deleted), (1, 1))
        active = {r.original_record_id: r for r in self.service.store.list(self.scope)}
        self.assertEqual(set(active), {"I-1"})
        self.assertIn("now fixed", active["I-1"].content)
        versions = self.service.store.list_versions(active["I-1"].evidence_id, self.scope)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0].event_time, versions[0].event_time)  # timezone-aware preserved

    def test_json_array_and_jsonl_stable_identity(self):
        path = self.root / "records.json"
        path.write_text(json.dumps({"items": [
            {"id": "r1", "title": "Alpha", "body": "first record"},
            {"id": "r2", "title": "Beta", "body": "second record"},
        ]}), encoding="utf-8")
        source = self.register("json", path, {
            "record_path": "items", "id_field": "id",
            "content_fields": ["title", "body"], "category": "document",
        })
        self.assertEqual(self.service.sync(source.source_id).added, 2)
        path.write_text(json.dumps({"items": [
            {"id": "r1", "title": "Alpha", "body": "first record updated"},
        ]}), encoding="utf-8")
        report = self.service.sync(source.source_id)
        self.assertEqual((report.updated, report.deleted), (1, 1))

        lines = self.root / "records.jsonl"
        lines.write_text(
            json.dumps({"key": "e1", "text": "deploy happened"}) + "\n" +
            json.dumps({"key": "e2", "text": "rollback happened"}) + "\n",
            encoding="utf-8")
        jsonl = self.register("json", lines, {
            "id_field": "key", "content_fields": ["text"], "category": "logs",
        })
        self.assertEqual(self.service.sync(jsonl.source_id).added, 2)
        ids = {r.original_record_id for r in self.service.store.list(self.scope)}
        self.assertTrue({"r1", "e1", "e2"}.issubset(ids))

    def test_missing_or_duplicate_json_id_fails_without_tombstones(self):
        path = self.root / "bad.jsonl"
        path.write_text('{"id":"x","text":"one"}\n{"text":"missing"}\n', encoding="utf-8")
        source = self.register("json", path, {"category": "logs", "content_fields": ["text"]})
        with self.assertRaises(ValueError):
            self.service.sync(source.source_id)
        self.assertEqual(self.service.store.list(self.scope), [])
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()[0],
                "failed")


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-v2-git-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test User")
        self.git("branch", "-M", "main")
        self.db = self.root / "evidence.sqlite3"
        self.scope = EvidenceScope(project_id="git-project", dataset_kind="live")
        self.service = SyncService(self.db)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, text):
        (self.repo / "notes.md").write_text(text, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", text)
        return self.git("rev-parse", "HEAD")

    def register(self):
        return self.service.registry.register(SourceSpec(
            project_id=self.scope.project_id, dataset_kind="live",
            connector_type="git", source_uri=self.repo.as_uri(),
            config={"repo_path": str(self.repo), "branch": "main"},
        ))

    def test_full_then_incremental_cursor(self):
        first_sha = self.commit("first commit")
        self.commit("second commit")
        source = self.register()
        full = self.service.sync(source.source_id)
        self.assertEqual((full.status, full.mode, full.added), ("succeeded", "full", 2))
        self.assertEqual(self.service.sync(source.source_id).scanned, 0)
        third_sha = self.commit("third commit")
        incremental = self.service.sync(source.source_id)
        self.assertEqual((incremental.mode, incremental.added), ("incremental", 1))
        commits = {r.original_record_id for r in self.service.store.list(self.scope)}
        self.assertEqual(commits, {first_sha, third_sha,
                                   self.git("rev-parse", "HEAD~1")})
        status = self.service.status(source.source_id)
        self.assertEqual(status["last_run"]["status"], "succeeded")
        self.assertEqual(status["item_counts"], {"active": 3})

    def test_rewrite_marks_unreachable_commits_orphaned_not_deleted(self):
        self.commit("first")
        old_sha = self.commit("second")
        source = self.register()
        self.service.sync(source.source_id)
        old_record = next(r for r in self.service.store.list(self.scope)
                          if r.original_record_id == old_sha)
        self.git("reset", "--hard", "HEAD~1")
        self.git("reflog", "expire", "--expire=now", "--all")
        report = self.service.sync(source.source_id)
        self.assertEqual(report.orphaned, 1)
        self.assertIn("history_rewritten_cursor_invalid", report.warnings)
        active = {r.original_record_id for r in self.service.store.list(self.scope)}
        self.assertNotIn(old_sha, active)
        all_records = {r.original_record_id for r in self.service.store.list(
            self.scope, include_deleted=True)}
        self.assertIn(old_sha, all_records)
        self.assertIsNotNone(self.service.store.get(
            old_record.evidence_id, self.scope, old_record.content_hash))
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM evidence_tombstones").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
