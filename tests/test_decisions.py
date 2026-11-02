"""Local SQLite review lifecycle tests; no real provider requests or user data."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src.evidence import EvidenceScope, legacy_evidence
from src.evidence_store import EvidenceStore
from src.ingest import normalize_record
from src.investigation import Claim, Conflict, EvidenceRef, InvestigationResult, parse_action
from src.memory.decisions import DecisionStore, RevisionConflict, timestamp
from src.agents.react_agent import ReActAgent, ToolExecutor

from offline_env import OFFLINE_PROVIDER_ENV


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-decisions-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence_path = self.root / "evidence.sqlite3"
        self.decision_path = self.root / "decisions.sqlite3"
        self.scope = EvidenceScope(dataset_kind="live", project_id="decision-project")
        self.now = "2025-01-10T00:00:00+00:00"
        self.store = DecisionStore(self.decision_path, evidence_db_path=self.evidence_path,
            clock=lambda: datetime.fromisoformat(self.now))
        self.b = normalize_record("Team approved plan B because latency is bounded.", "file:///decisions/issue", "issues", "I-1",
            project_id=self.scope.project_id, source_type="issue", event_time="2025-01-01T00:00:00+00:00")
        self.c = normalize_record("Proposal: adopt plan C with measured capacity.", "file:///decisions/issue", "issues", "I-2",
            project_id=self.scope.project_id, source_type="issue", event_time="2025-02-01T00:00:00+00:00")
        EvidenceStore(self.evidence_path).append([self.b, self.c])
        self.topic = self.store.create("技术方案选择", self.scope)
        self.decision_id = self.topic["decision_id"]
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {**OFFLINE_PROVIDER_ENV,
            "EVIDENCE_DB_PATH": str(self.evidence_path), "DECISION_DB_PATH": str(self.decision_path)}).start()

    def result(self, record=None, **updates):
        record = record or self.b
        reference = EvidenceRef(evidence_id=record["evidence_id"], content_hash=record["content_hash"])
        return InvestigationResult(run_id="test-run", scope=self.scope, answer="test", termination_reason="sufficient",
            evidence_refs=[reference], claims=[Claim(text=record["content"], refs=[reference])], **updates)

    def propose(self, record=None, statement="Use B", **kwargs):
        return self.store.propose(self.decision_id, self.result(record), statement, **kwargs)

    def accept(self, proposal, revision=0):
        return self.store.review(proposal["proposal_id"], self.scope, operation="accept", reviewer_id="local-human",
            expected_revision=revision, note="Checked source and risks locally", acknowledge_risks=True)

    def test_create_and_pending_does_not_confirm(self):
        proposal = self.propose()
        snapshot = self.store.snapshot(self.decision_id, self.scope)
        self.assertEqual(proposal["review_status"], "pending")
        self.assertFalse(snapshot["current_confirmed"])
        self.assertFalse(snapshot["versions"])

    def test_invalid_or_unobserved_evidence_rejected_and_no_pointer_change(self):
        proposal = self.propose()
        self.accept(proposal)
        reference = {"evidence_id": self.c["evidence_id"], "content_hash": self.c["content_hash"]}
        with self.assertRaises(ValueError):
            self.store.propose(self.decision_id, self.result(), "Use C", refs=[reference])
        with self.assertRaises(ValueError):
            self.store.propose(self.decision_id, self.result(), "Use C", refs=[])
        with self.assertRaises(ValueError):
            self.store.propose(self.decision_id, self.result(), "Use C", refs=[{**reference, "content_hash": "0" * 64}])
        self.assertEqual(self.store.snapshot(self.decision_id, self.scope)["current_confirmed"]["statement"], "Use B")

    def test_idempotent_candidate_and_opposite_candidates_retained(self):
        first = self.propose()
        self.assertEqual(self.propose()["proposal_id"], first["proposal_id"])
        second = self.propose(self.c, "Use C")
        snapshot = self.store.snapshot(self.decision_id, self.scope)
        self.assertEqual(len(snapshot["proposals"]), 2)
        self.assertEqual(len(snapshot["conflicts"]), 1)
        self.assertNotEqual(first["proposal_id"], second["proposal_id"])

    def test_reject_does_not_change_confirmed_version(self):
        self.accept(self.propose())
        proposal = self.propose(self.c, "Use C")
        self.store.review(proposal["proposal_id"], self.scope, operation="reject", reviewer_id="local-human",
                          expected_revision=1, note="Not approved")
        snapshot = self.store.snapshot(self.decision_id, self.scope)
        self.assertEqual(snapshot["current_confirmed"]["statement"], "Use B")
        self.assertEqual(len(snapshot["versions"]), 1)
        self.assertEqual(snapshot["review_history"][-1]["operation"], "reject")

    def test_b_to_c_lifecycle_time_and_restart(self):
        self.accept(self.propose(event_time=self.b["event_time"]))
        self.now = "2025-02-10T00:00:00+00:00"
        candidate = self.propose(self.c, "Use C", event_time=self.c["event_time"])
        self.assertEqual(self.store.snapshot(self.decision_id, self.scope)["current_confirmed"]["statement"], "Use B")
        self.assertEqual(candidate["review_status"], "pending")
        self.now = "2025-03-10T00:00:00+00:00"
        self.accept(candidate, revision=1)
        reopened = DecisionStore(self.decision_path, evidence_db_path=self.evidence_path)
        snapshot = reopened.snapshot(self.decision_id, self.scope)
        self.assertEqual([v["statement"] for v in snapshot["versions"]], ["Use B", "Use C"])
        self.assertEqual([v["status"] for v in snapshot["versions"]], ["superseded", "confirmed"])
        self.assertEqual(snapshot["versions"][1]["supersedes_version_id"], snapshot["versions"][0]["version_id"])
        self.assertEqual(snapshot["current_confirmed"]["event_time"], timestamp(self.c["event_time"]))
        self.assertEqual(snapshot["current_confirmed"]["reviewed_at"], self.now)
        self.assertEqual(len(snapshot["review_history"]), 2)
        self.assertEqual(snapshot["world_state"], "unknown")
        print("DECISION_LIFECYCLE=" + json.dumps({"B": snapshot["versions"][0]["status"], "C": snapshot["versions"][1]["status"],
            "supersedes": snapshot["versions"][1]["supersedes_version_id"], "world_state": snapshot["world_state"]}))

    def test_immutable_sql_bodies_and_evidence_links(self):
        self.accept(self.propose())
        for sql in ("UPDATE decision_versions SET statement='overwrite'", "DELETE FROM decision_versions",
                    "UPDATE decision_evidence SET content_hash='overwrite'", "DELETE FROM decision_evidence"):
            with closing(sqlite3.connect(self.decision_path)) as db, self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)
        self.assertEqual(self.store.snapshot(self.decision_id, self.scope)["current_confirmed"]["statement"], "Use B")

    def test_concurrent_reviews_revision_conflict(self):
        first, second = self.propose(), self.propose(self.c, "Use C")
        def review(proposal):
            try:
                return self.accept(proposal)["revision"]
            except RevisionConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(review, [first, second]))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(len(self.store.snapshot(self.decision_id, self.scope)["versions"]), 1)

    def test_scope_isolation(self):
        proposal = self.propose()
        for scope in (EvidenceScope(dataset_kind="live", project_id="other"), EvidenceScope(dataset_kind="fixture", project_id=self.scope.project_id)):
            with self.assertRaises(KeyError): self.store.snapshot(self.decision_id, scope)
            with self.assertRaises(KeyError): self.store.proposal(proposal["proposal_id"], scope)
            self.assertEqual(self.store.search("技术", scope), [])

    def test_knowledge_time_no_future_backfill(self):
        self.accept(self.propose(event_time=self.b["event_time"]))
        self.now = "2025-03-10T00:00:00+00:00"
        candidate = self.propose(self.c, "Use C", event_time="2025-01-02T00:00:00+00:00")
        self.accept(candidate, revision=1)
        january = self.store.snapshot(self.decision_id, self.scope, known_at="2025-01-31T23:59:59+00:00")
        self.assertEqual(january["current_confirmed"]["statement"], "Use B")
        self.assertEqual(january["current_confirmed"]["status"], "confirmed")
        self.assertEqual(len(january["versions"]), 1)
        before_record = self.store.snapshot(self.decision_id, self.scope, known_at="2025-01-01T12:00:00+00:00")
        self.assertIsNone(before_record["current_confirmed"])

    def test_risk_ack_and_failure_roll_back_superseded_state(self):
        self.accept(self.propose())
        proposal = self.propose(self.c, "Use C")
        with self.assertRaises(ValueError):
            self.store.review(proposal["proposal_id"], self.scope, operation="accept", reviewer_id="human",
                expected_revision=1, note="no risk ack")
        with closing(sqlite3.connect(self.decision_path)) as db:
            db.execute("CREATE TRIGGER fail_version BEFORE INSERT ON decision_versions BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.DatabaseError): self.accept(proposal, revision=1)
        snapshot = self.store.snapshot(self.decision_id, self.scope)
        self.assertEqual(snapshot["current_confirmed"]["statement"], "Use B")
        self.assertEqual(snapshot["current_confirmed"]["status"], "confirmed")
        self.assertEqual(snapshot["decision"]["revision"], 1)
        self.assertEqual(self.store.proposal(proposal["proposal_id"], self.scope)["review_status"], "pending")

    def test_conflicted_result_risks_are_preserved(self):
        result = self.result(gaps=["Missing corroboration"])
        result.termination_reason = "insufficient"
        proposal = self.store.propose(self.decision_id, result, "Use B")
        self.assertIn("investigation_gaps", proposal["risk_flags"])
        self.assertIn("investigation_incomplete", proposal["risk_flags"])
        self.assertIsNone(self.store.snapshot(self.decision_id, self.scope)["current_confirmed"])

    def test_legacy_alone_cannot_confirm(self):
        legacy = legacy_evidence({"content": "legacy proposal"}, scope=self.scope)
        EvidenceStore(self.evidence_path).append([legacy])
        result = self.result(legacy.model_dump(mode="json"))
        proposal = self.store.propose(self.decision_id, result, "Use legacy")
        with self.assertRaises(ValueError): self.accept(proposal)

    def test_agent_readonly_memory_has_real_evidence_not_version_alias(self):
        self.accept(self.propose())
        self.propose(self.c, "Use C")
        executor = ToolExecutor(scope=self.scope, db_path=self.evidence_path)
        output = executor.execute("search_memory", "技术方案", filters={"decision_id": self.decision_id})
        self.assertEqual(output["decision_memories"][0]["statement"], "Use B")
        self.assertTrue(output["decision_memories"][0]["version_id"].startswith("dv_"))
        self.assertTrue(all(r["evidence_id"].startswith("ev_") for r in output["evidence"]))
        self.assertEqual(output["decision_memories"][0]["world_state"], "unknown")
        with self.assertRaises(ValueError): parse_action('{"action":"review","proposal_id":"x"}')
        self.assertIn("error", executor.execute("accept_decision", "approve"))

    def test_react_displays_system_record_without_world_confirmation(self):
        self.accept(self.propose())
        agent = ReActAgent(scope=self.scope, db_path=self.evidence_path)
        agent.api_key = "offline-script"
        replies = iter([json.dumps({"action": "tool_call", "tool": "search_memory", "query": "技术方案",
            "filters": {"decision_id": self.decision_id}}), '{"action":"finish"}'])
        agent._call_llm = lambda messages: next(replies)
        result = agent.run_result("现在系统记录的正式方案是什么？")
        self.assertIn("系统记录的最新已确认版本", result.answer)
        self.assertIn("现实状态未验证", result.answer)
        self.assertEqual(result.decision_memories[0]["statement"], "Use B")
        self.assertNotEqual(result.termination_reason, "sufficient")

    def test_local_cli_read_and_no_http_review(self):
        import api
        self.accept(self.propose())
        completed = subprocess.run([os.sys.executable, "-X", "utf8", "-m", "src.memory.decision_cli",
            "--project-id", self.scope.project_id, "--db", str(self.decision_path), "--evidence-db", str(self.evidence_path),
            "show", "--decision-id", self.decision_id], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(completed.stdout)["current_confirmed"]["statement"], "Use B")
        self.assertFalse(any("review" in route.path or "proposal" in route.path for route in api.app.routes))

    def test_supports_and_contradicts_links_preserved(self):
        result = self.result(self.c)
        result.evidence_refs.append(EvidenceRef(evidence_id=self.b["evidence_id"], content_hash=self.b["content_hash"]))
        refs = [{**r.model_dump(), "relation_type": "supports" if r.evidence_id == self.c["evidence_id"] else "contradicts"} for r in result.evidence_refs]
        proposal = self.store.propose(self.decision_id, result, "Use C", refs=refs)
        self.assertIn("contradicting_evidence", proposal["risk_flags"])
        self.accept(proposal)
        version = self.store.snapshot(self.decision_id, self.scope)["current_confirmed"]
        self.assertEqual({r["relation_type"] for r in version["evidence_refs"]}, {"supports", "contradicts"})

    def test_pending_and_historical_conflicts_dont_leak_future_review(self):
        self.accept(self.propose())
        self.now = "2025-02-10T00:00:00+00:00"
        candidate = self.propose(self.c, "Use C")
        self.now = "2025-03-10T00:00:00+00:00"
        self.accept(candidate, revision=1)
        february = self.store.snapshot(self.decision_id, self.scope, known_at="2025-02-20T00:00:00+00:00")
        self.assertEqual(february["proposals"][-1]["review_status"], "pending")
        self.assertIsNone(february["proposals"][-1]["reviewer_id"])
        self.assertEqual(february["conflicts"][0]["resolution_status"], "open")
        self.assertIsNone(february["conflicts"][0]["right_version_id"])

    def test_review_once_and_history_append_only(self):
        proposal = self.propose()
        self.accept(proposal)
        with self.assertRaises(RevisionConflict): self.accept(proposal, revision=1)
        with closing(sqlite3.connect(self.decision_path)) as db, self.assertRaises(sqlite3.DatabaseError):
            db.execute("DELETE FROM decision_review_history")
        self.assertEqual(len(self.store.snapshot(self.decision_id, self.scope)["review_history"]), 1)

    def test_cross_scope_review_rejected(self):
        proposal = self.propose()
        other = EvidenceScope(dataset_kind="fixture", project_id=self.scope.project_id)
        with self.assertRaises(KeyError):
            self.store.review(proposal["proposal_id"], other, operation="accept", reviewer_id="wrong-scope",
                              expected_revision=0, note="should fail", acknowledge_risks=True)
        self.assertFalse(self.store.snapshot(self.decision_id, self.scope)["versions"])

    def test_read_only_missing_memory_db_does_not_create_or_migrate(self):
        missing = self.root / "absent.sqlite3"
        store = DecisionStore(missing, evidence_db_path=self.evidence_path)
        self.assertEqual(store.search("anything", self.scope), [])
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError): DecisionStore(self.evidence_path, evidence_db_path=self.evidence_path)
        with closing(sqlite3.connect(self.evidence_path)) as db:
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='decisions'").fetchall())

    def test_memory_history_filters_and_fixture_live_isolation(self):
        self.accept(self.propose())
        self.now = "2025-03-10T00:00:00+00:00"
        self.accept(self.propose(self.c, "Use C"), revision=1)
        executor = ToolExecutor(scope=self.scope, db_path=self.evidence_path)
        result = executor.execute("search_memory", "技术方案", filters={"decision_id": self.decision_id, "memory_view": "history",
                                                                       "known_at": "2025-01-31T00:00:00+00:00"})
        self.assertEqual([m["statement"] for m in result["decision_memories"]], ["Use B"])
        fixture = ToolExecutor(scope=EvidenceScope(dataset_kind="fixture", project_id=self.scope.project_id), db_path=self.evidence_path)
        self.assertFalse(fixture.execute("search_memory", "技术方案", filters={"decision_id": self.decision_id})["decision_memories"])

    def test_candidate_difference_stays_open_until_other_candidate_reviewed(self):
        first, second = self.propose(), self.propose(self.c, "Use C")
        self.accept(first)
        links = self.store.snapshot(self.decision_id, self.scope)["conflicts"]
        self.assertEqual(links[0]["resolution_status"], "open")
        self.assertTrue(links[0]["left_version_id"])
        self.accept(second, revision=1)
        links = self.store.snapshot(self.decision_id, self.scope)["conflicts"]
        self.assertEqual(links[0]["resolution_status"], "reviewed_acceptance")
        self.assertTrue(links[0]["right_version_id"])

    def test_idempotent_candidate_retains_new_investigation_risks(self):
        first = self.propose()
        self.now = "2025-02-10T00:00:00+00:00"
        newer = self.result(gaps=["Later investigation found missing context"])
        newer.run_id = "later-run"
        second = self.store.propose(self.decision_id, newer, "Use B")
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertIn("investigation_gaps", second["risk_flags"])
        self.assertEqual(len(second["investigation_observations"]), 2)
        january = self.store.snapshot(self.decision_id, self.scope, known_at="2025-01-31T00:00:00+00:00")
        self.assertNotIn("investigation_gaps", january["proposals"][0]["risk_flags"])

    def test_conflict_context_is_proposal_risk_not_automatic_confirmation(self):
        result = self.result()
        result.evidence_refs.append(EvidenceRef(evidence_id=self.c["evidence_id"], content_hash=self.c["content_hash"]))
        result.conflicts = [Conflict(description="Two sources need review", refs=list(result.evidence_refs))]
        result.termination_reason = "conflict"
        proposal = self.store.propose(self.decision_id, result, "Needs explicit review")
        self.assertIn("investigation_conflicts", proposal["risk_flags"])
        self.assertEqual(proposal["review_status"], "pending")
        self.assertIsNone(self.store.snapshot(self.decision_id, self.scope)["current_confirmed"])


class SmokeAuthorizationTests(unittest.TestCase):
    def test_preparation_without_authorization_never_constructs_client(self):
        from src.llm_smoke import run_suite
        with patch("src.agents.react_agent.ReActAgent", side_effect=AssertionError("must not construct provider")):
            result = run_suite()
        self.assertEqual(result["status"], "pending_authorization")
        self.assertEqual(len(result["scenarios"]), 5)
        self.assertFalse(result["online_executed"])

    def test_mismatched_online_origin_is_rejected_before_client(self):
        from src.llm_smoke import run_suite
        with patch("src.agents.react_agent.ReActAgent", side_effect=AssertionError("must not construct provider")), self.assertRaises(ValueError):
            run_suite(online=True, approved_origin="https://not-the-configured-provider.invalid")


if __name__ == "__main__":
    unittest.main()
