"""Web workbench static assets and scoped Evidence detail endpoint.

Browser interaction is exercised separately against a real uvicorn process; this
module keeps the fast offline contract in the normal unittest suite.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import api
from src.api.runtime_routes import MAX_EVIDENCE_DETAIL_CHARS
from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore
from src.runtime.config import RuntimeConfig
from src.runtime.service import RunService

from tests.runtime_helpers import agent_factory, make_evidence, simple_behaviour


class WebStaticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-web-static-")
        self.addCleanup(self.temp.cleanup)
        db = Path(self.temp.name) / "runtime.sqlite3"
        service = RunService(
            db, agent_factory=agent_factory(simple_behaviour()),
            config=RuntimeConfig(db_path=db))
        self.addCleanup(service.close)
        self.client = TestClient(api.create_app(runtime_service=service))
        self.addCleanup(self.client.close)

    def test_root_redirects_to_workbench_and_assets_are_same_origin(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/demo/")

        page = self.client.get("/demo/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("研发调查工作台", page.text)
        self.assertIn("./styles.css", page.text)
        self.assertIn("./app.js", page.text)
        self.assertIn('id="conversation"', page.text)
        self.assertIn('id="evidence-list"', page.text)
        self.assertIn('id="tool-call-list"', page.text)
        self.assertIn('id="trace-events"', page.text)

        for path, content_type in (("/demo/app.js", "text/javascript"),
                                   ("/demo/styles.css", "text/css")):
            asset = self.client.get(path)
            self.assertEqual(asset.status_code, 200)
            self.assertIn(content_type, asset.headers["content-type"])
            self.assertTrue(asset.content)

    def test_workbench_script_uses_safe_dom_and_real_api_paths(self):
        script = self.client.get("/demo/app.js").text
        # No dynamic HTML parsing or inline code execution.
        self.assertIsNone(re.search(r"\.innerHTML\s*=", script))
        self.assertNotIn("insertAdjacentHTML", script)
        self.assertNotIn("eval(", script)
        self.assertNotIn("document.write", script)
        # Real runtime endpoints, not embedded fixture answers.
        for route in ("/sessions", "/runs", "/stream", "/evidence/",
                      "/tool-calls", "/context-trace", "/result"):
            self.assertIn(route, script)
        self.assertIn("textContent", script)
        self.assertIn("localStorage", script)
        self.assertNotIn("Authorization", script)
        self.assertNotIn("api_key", script.lower())

    def test_static_mount_does_not_shadow_runtime_api(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        # Runtime routes remain owned by the API router rather than StaticFiles.
        self.assertEqual(self.client.get("/runs/not-a-run",
                                         params={"project_id": "p"}).status_code, 404)


class EvidenceDetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-web-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        self.project = "demo-project"
        self.scope = EvidenceScope(project_id=self.project, dataset_kind="live")
        self.service = RunService(
            self.db, agent_factory=agent_factory(simple_behaviour()),
            config=RuntimeConfig(db_path=self.db))
        self.addCleanup(self.service.close)
        self.client = TestClient(api.create_app(runtime_service=self.service))
        self.addCleanup(self.client.close)

    def get_evidence(self, record, **overrides):
        params = {
            "project_id": self.project,
            "dataset_kind": "live",
            "content_hash": record.content_hash,
        }
        params.update(overrides)
        return self.client.get(f"/evidence/{record.evidence_id}", params=params)

    def test_scoped_read_only_detail_returns_citation_fields(self):
        record = make_evidence(self.db, self.scope, content="bounded evidence body")
        response = self.get_evidence(record)
        self.assertEqual(response.status_code, 200, response.text)
        evidence = response.json()["evidence"]
        self.assertEqual(evidence["evidence_id"], record.evidence_id)
        self.assertEqual(evidence["content"], record.content)
        self.assertEqual(evidence["content_hash"], record.content_hash)
        self.assertEqual(evidence["source_uri"], record.source_uri)
        self.assertEqual(evidence["verification"], "current_active")
        self.assertTrue(evidence["is_current"])
        self.assertTrue(evidence["content_hash_matches_current"])
        self.assertFalse(evidence["content_truncated"])
        self.assertEqual(evidence["content_limit"], MAX_EVIDENCE_DETAIL_CHARS)

    def test_scope_and_hash_mismatch_never_leak_other_partition(self):
        record = make_evidence(self.db, self.scope, content="private partition")
        self.assertEqual(self.get_evidence(record, project_id="other").status_code, 404)
        self.assertEqual(self.get_evidence(record, dataset_kind="fixture").status_code, 404)
        self.assertEqual(self.get_evidence(record, content_hash="0" * 64).status_code, 404)
        self.assertEqual(self.get_evidence(record, project_id=" p").status_code, 422)
        self.assertEqual(self.client.get(
            f"/evidence/{record.evidence_id}",
            params={"project_id": self.project}).status_code, 422)
        self.assertEqual(self.client.get(
            "/evidence/not-an-evidence-id",
            params={"project_id": self.project,
                    "dataset_kind": "live"}).status_code, 422)

    def test_historical_revision_and_content_limit(self):
        original = make_evidence(self.db, self.scope, content="old body")
        long_content = "新" * (MAX_EVIDENCE_DETAIL_CHARS + 137)
        revised = Evidence.create(
            project_id=self.project,
            dataset_kind="live",
            source_type=original.source_type,
            source_uri=original.source_uri,
            original_record_id=original.original_record_id,
            content=long_content,
            ingested_at=datetime.now(timezone.utc),
        )
        self.assertEqual(revised.evidence_id, original.evidence_id)
        EvidenceStore(self.db).append([revised])

        old_response = self.get_evidence(original)
        self.assertEqual(old_response.status_code, 200)
        old = old_response.json()["evidence"]
        self.assertEqual(old["content"], "old body")
        self.assertEqual(old["verification"], "historical")
        self.assertFalse(old["is_current"])
        self.assertFalse(old["content_hash_matches_current"])
        self.assertEqual(old["current_content_hash"], revised.content_hash)

        new_response = self.get_evidence(revised)
        self.assertEqual(new_response.status_code, 200)
        current = new_response.json()["evidence"]
        self.assertTrue(current["content_truncated"])
        self.assertEqual(current["content_chars"], len(long_content))
        self.assertEqual(len(current["content"]), MAX_EVIDENCE_DETAIL_CHARS)
        self.assertEqual(current["verification"], "current_active")
        self.assertTrue(current["is_current"])


if __name__ == "__main__":
    unittest.main()
