"""Second-batch decision investigation tests; offline, synthetic, no provider calls."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.agents.react_agent import ReActAgent, ToolExecutor
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.ingest import ingest_csv, normalize_record
from src.investigation import EvidenceLedger, EvidenceRef, InvestigationResult, structured_assertion_conflicts
from src.investigation_context import task_coverage
from src.memory.decisions import DecisionStore

from offline_env import OFFLINE_PROVIDER_ENV


class DecisionInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cma-decision-investigation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence_path = self.root / "evidence.sqlite3"
        self.decision_path = self.root / "decisions.sqlite3"
        self.scope = EvidenceScope(dataset_kind="live", project_id="team-investigation")
        self.now = "2026-01-16T00:00:00+00:00"
        self.store = DecisionStore(self.decision_path, evidence_db_path=self.evidence_path,
            clock=lambda: datetime.fromisoformat(self.now))
        self.records = [
            self.record("chat", "chat://CHAT-001", "2026-01-09T09:30:00+08:00",
                        "讨论：如果只做 BM25，语义改写问题的召回可能不足。"),
            self.record("document", "requirements://REQ-003", "2026-01-09T09:10:00+08:00",
                        "范围约束：第一版优先验证 SQLite Evidence 与 BM25 基线，不直接引入新的向量数据库。"),
            self.record("meeting", "meeting://MTG-001", "2026-01-15T14:00:00+08:00",
                        "会议结论：v1 采用 SQLite Evidence + BM25 基线，暂不引入向量数据库。"),
            self.record("document", "review://REV-001", "2026-01-22T16:00:00+08:00",
                        "评审问题：BM25 对同义改写召回不足，需要记录为已知限制。"),
            self.record("chat", "chat://CHAT-004", "2026-02-10T11:00:00+08:00",
                        "旧计划：Q2 上线向量数据库。"),
            self.record("meeting", "meeting://MTG-003", "2026-03-12T15:00:00+08:00",
                        "决策变更：取消 Q2 向量数据库迁移；v1 保持 SQLite + BM25，待真实数据接入后重新评估。"),
            self.record("document", "decision://DEC-001", "2026-03-12T15:10:00+08:00",
                        "决策记录：取消 Q2 向量数据库迁移，保留 SQLite + BM25。"),
            self.record("chat", "chat://CHAT-003", None,
                        "旧提议：Q2 迁移到向量数据库。该消息没有发送时间，不能当作已排期决策。"),
            self.record("document", "deploy://DEPLOY-001", "2026-03-12T17:00:00+08:00",
                        "发布记录：staging 检索服务已部署，后端使用 Qdrant。",
                        assertions={"environment": "staging", "vector_backend": "qdrant", "deployment": "deployed"}),
            self.record("document", "deploy://DEPLOY-002", "2026-03-12T17:10:00+08:00",
                        "发布记录：staging 检索服务已部署，后端仍为 SQLite。",
                        assertions={"environment": "staging", "vector_backend": "sqlite", "deployment": "deployed"}),
        ]
        EvidenceStore(self.evidence_path).append(self.records)
        self.topic = self.store.create("检索后端方案", self.scope)
        self.decision_id = self.topic["decision_id"]
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {**OFFLINE_PROVIDER_ENV,
            "EVIDENCE_DB_PATH": str(self.evidence_path),
            "DECISION_DB_PATH": str(self.decision_path)}).start()

    def record(self, category, source, event_time, content, *, record_id=None, assertions=None):
        record_id = record_id or source.rsplit("/", 1)[-1]
        metadata = {"assertions": assertions} if assertions is not None else None
        return normalize_record(content, source, category, record_id, project_id=self.scope.project_id,
                                source_type=category, event_time=event_time, extra_meta=metadata)

    def by_source(self, suffix):
        return next(record for record in self.records if record["source_uri"].endswith(suffix))

    def result(self, records, *, run_id="decision-investigation-run"):
        refs = [EvidenceRef(evidence_id=record["evidence_id"], content_hash=record["content_hash"])
                for record in records]
        return InvestigationResult(run_id=run_id, scope=self.scope, answer="synthetic review input",
            termination_reason="sufficient", evidence_refs=refs, claims=[])

    def propose(self, decision_id, records, statement, *, event_time=None, refs=None):
        refs = refs or [{"evidence_id": record["evidence_id"], "content_hash": record["content_hash"],
                         "relation_type": "supports"} for record in records]
        return self.store.propose(decision_id, self.result(records), statement, refs=refs, event_time=event_time)

    def accept(self, proposal, revision):
        return self.store.review(proposal["proposal_id"], self.scope, operation="accept",
            reviewer_id="synthetic-local-reviewer", expected_revision=revision,
            note="Synthetic case reviewed against the named evidence", acknowledge_risks=True)

    def build_history(self):
        meeting_v1 = self.by_source("MTG-001")
        requirement = self.by_source("REQ-003")
        self.accept(self.propose(self.decision_id, [meeting_v1, requirement],
            "采用 SQLite Evidence + BM25 作为 v1 基线，暂不引入向量数据库。",
            event_time=meeting_v1["event_time"]), 0)
        self.now = "2026-03-13T00:00:00+00:00"
        meeting_v2 = self.by_source("MTG-003")
        decision_doc = self.by_source("DEC-001")
        old_plan = self.by_source("CHAT-004")
        refs = [
            {"evidence_id": meeting_v2["evidence_id"], "content_hash": meeting_v2["content_hash"],
             "relation_type": "supports"},
            {"evidence_id": decision_doc["evidence_id"], "content_hash": decision_doc["content_hash"],
             "relation_type": "supports"},
            {"evidence_id": old_plan["evidence_id"], "content_hash": old_plan["content_hash"],
             "relation_type": "contradicts"},
        ]
        self.accept(self.propose(self.decision_id, [meeting_v2, decision_doc, old_plan],
            "取消 Q2 向量数据库迁移，v1 保持 SQLite + BM25，待真实数据接入后重新评估。",
            event_time=meeting_v2["event_time"], refs=refs), 1)
        return meeting_v1, meeting_v2

    def test_body_search_time_bounds_and_known_at_snapshots(self):
        self.build_history()
        hits = self.store.search("重新评估 向量数据库 迁移", self.scope, view="history", top_k=5)
        self.assertEqual(hits[0]["statement"], "取消 Q2 向量数据库迁移，v1 保持 SQLite + BM25，待真实数据接入后重新评估。")
        self.assertEqual(hits[0]["title"], "检索后端方案")

        january = self.store.search("SQLite BM25", self.scope, view="history",
            after="2026-01-01T00:00:00+00:00", before="2026-02-01T00:00:00+00:00")
        self.assertEqual([hit["version_id"] for hit in january], [hits[0]["supersedes_version_id"]])
        march_current = self.store.search("SQLite BM25", self.scope, view="current",
            after="2026-03-01T00:00:00+00:00", before="2026-04-01T00:00:00+00:00")
        self.assertEqual([hit["statement"] for hit in march_current], [hits[0]["statement"]])

        snapshot = self.store.search("重新评估 向量数据库", self.scope, view="current",
                                     known_at="2026-02-01T00:00:00+00:00")
        self.assertEqual([hit["statement"] for hit in snapshot],
                         ["采用 SQLite Evidence + BM25 作为 v1 基线，暂不引入向量数据库。"])
        self.assertEqual(snapshot[0]["knowledge_cutoff"], "2026-02-01T00:00:00+00:00")
        self.assertTrue(snapshot[0]["system_current"])
        self.assertEqual(snapshot[0]["execution_status"], "unknown")
        self.assertEqual(snapshot[0]["world_state"], "unknown")
        before_first_review = self.store.search("重新评估 向量数据库", self.scope, view="current",
                                                known_at="2026-01-10T00:00:00+00:00")
        self.assertEqual(before_first_review, [])

    def test_unknown_event_time_is_not_backfilled_from_ingest_time(self):
        unknown_topic = self.store.create("无时间方案", self.scope)
        proposal = self.propose(unknown_topic["decision_id"], [self.by_source("CHAT-003")],
                                "暂不确认无时间提议。", event_time=None)
        self.accept(proposal, 0)
        hit = self.store.search("无时间方案", self.scope, view="current")[0]
        self.assertEqual(hit["event_time_status"], "unknown")
        self.assertIsNone(hit["event_time"])
        self.assertIsNotNone(hit["decision_known_at"])
        self.assertEqual(self.store.search("无时间方案", self.scope, view="current",
            after="2026-01-01T00:00:00+00:00", before="2027-01-01T00:00:00+00:00"), [])
        timeline = self.store.snapshot(unknown_topic["decision_id"], self.scope)["timeline"]
        self.assertEqual(timeline[0]["event_time_status"], "unknown")

    def test_decision_versions_keep_fetchable_evidence_identity_and_scope(self):
        self.build_history()
        hits = self.store.search("重新评估 向量数据库", self.scope, view="current")
        self.assertTrue(hits)
        for detail in hits[0]["evidence"]:
            self.assertRegex(detail["evidence_id"], r"^ev_[0-9a-f]{64}$")
            self.assertRegex(detail["content_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(detail["provenance_status"], "verified")
            self.assertIn(detail["relation_type"], {"supports", "contradicts"})

        empty = self.root / "empty-evidence.sqlite3"
        EvidenceStore(empty)  # opens an empty store without writing decision tables
        unresolved = DecisionStore(self.decision_path, evidence_db_path=empty).search(
            "重新评估 向量数据库", self.scope, view="current")
        self.assertTrue(unresolved)
        self.assertTrue(all(item["provenance_status"] == "unresolved" for item in unresolved[0]["evidence"]))
        output = ToolExecutor(scope=self.scope, db_path=empty).execute(
            "search_memory", "重新评估 向量数据库", filters={"decision_id": self.decision_id})
        self.assertEqual(output["decision_memories"], [])

    def test_coverage_separates_authority_and_does_not_double_count_evidence(self):
        self.build_history()
        ledger = EvidenceLedger(self.scope)
        records = [self.by_source(name) for name in
                   ("CHAT-001", "MTG-001", "REV-001", "MTG-003", "DEPLOY-001", "DEPLOY-002")]
        ledger.register(records, "search_memory")
        ledger.register([self.by_source("CHAT-001")], "search_docs")
        memories = self.store.search("检索后端方案", self.scope, view="history", top_k=5)
        coverage = task_coverage(
            "请完整说明检索后端方案的初始原因、决策形成、评审意见、后续调整和当前状态。",
            ledger, memories, conflicts=ledger.conflicts, gaps=())
        self.assertEqual(coverage["method"], "deterministic_goal_and_literal_marker_check")
        self.assertEqual(coverage["semantic_completeness"], "unverified")
        self.assertEqual(coverage["missing_targets"], [])
        statuses = {item["target"]: item["status"] for item in coverage["targets"]}
        self.assertEqual(statuses["decision_formation"], "observed")
        self.assertEqual(statuses["current_status"], "observed_system_record")
        self.assertTrue(coverage["decision_binding"]["bound"])
        self.assertTrue(all(item["basis"] in {"explicit_id", "distinctive_topic_terms"}
                            for item in coverage["decision_binding"]["bound"]))
        authority = {item["level"]: item for item in coverage["authority_levels"]}
        self.assertTrue(authority["discussion"]["observed"])
        self.assertTrue(authority["meeting_record"]["observed"])
        self.assertTrue(authority["reviewed_decision"]["observed"])
        self.assertTrue(authority["execution"]["observed"])
        self.assertEqual(authority["execution"]["basis"], "literal_word_only_not_execution_proof")
        self.assertEqual(coverage["distinct_evidence_count"], len(records))
        self.assertEqual(coverage["independent_record_count"], len(records))
        self.assertEqual(coverage["duplicate_evidence_observations"], 1)
        self.assertEqual(coverage["completion_basis"], "record_presence_only_not_tool_or_evidence_count")

    def test_chat_assertion_is_unverified_until_a_human_reviewed_version_exists(self):
        chat_claim = self.record("chat", "chat://CHAT-999", "2026-03-20T10:00:00+08:00",
                                 "讨论：我们决定采用方案 C，已部署到生产。")
        ledger = EvidenceLedger(self.scope)
        ledger.register([chat_claim], "search_memory")
        coverage = task_coverage("为什么决定采用方案 C？现在状态如何？", ledger, (), gaps=())
        statuses = {item["target"]: item["status"] for item in coverage["targets"]}
        self.assertEqual(statuses["decision_formation"], "observed_unverified")
        self.assertEqual(statuses["current_status"], "observed_unverified")
        authority = {item["level"]: item for item in coverage["authority_levels"]}
        self.assertTrue(authority["discussion"]["observed"])
        self.assertFalse(authority["meeting_record"]["observed"])
        self.assertFalse(authority["reviewed_decision"]["observed"])
        self.assertTrue(authority["execution"]["observed"])

    def test_structured_conflicts_same_day_only_and_never_judged(self):
        deploy_one = self.by_source("DEPLOY-001")
        deploy_two = self.by_source("DEPLOY-002")
        conflicts = structured_assertion_conflicts([deploy_one, deploy_two])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(conflicts[0].refs), 2)
        self.assertIn("vector_backend", conflicts[0].description)

        undated = self.record("document", "deploy://DEPLOY-UNDATED", None,
                              "发布记录：staging 检索服务已部署。",
                              assertions={"environment": "staging", "vector_backend": "qdrant",
                                          "deployment": "deployed"})
        self.assertEqual(structured_assertion_conflicts([undated, deploy_two]), [])
        other_env = {**deploy_two, "metadata": {**deploy_two["metadata"],
                     "assertions": {"environment": "prod", "vector_backend": "sqlite", "deployment": "deployed"}}}
        self.assertEqual(structured_assertion_conflicts([deploy_one, other_env]), [])

        ledger = EvidenceLedger(self.scope)
        ledger.register([deploy_one, deploy_two], "search_docs")
        self.assertEqual(len(ledger.conflicts), 1)
        self.assertIn("vector_backend", ledger.conflicts[0].description)

    def test_missing_required_stage_blocks_sufficient_with_bounded_follow_up(self):
        agent = ReActAgent(scope=self.scope, db_path=self.evidence_path)
        agent.api_key = "offline-script"
        replies = iter([
            json.dumps({"action": "tool_call", "tool": "search_docs",
                        "query": "会议结论 基线 暂不引入"}),
            '{"action":"finish","claims":[],"gaps":["当前状态资料不足"]}',
        ])
        agent._call_llm = lambda messages: next(replies)
        question = "请完整说明初始原因、决策形成、评审意见、后续调整和当前状态。"
        result = agent.run_result(question)
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertIn("任务目标缺少可核验记录", result.answer)
        statuses = {item["target"]: item["status"] for item in result.task_coverage["targets"]}
        self.assertEqual(statuses["decision_formation"], "observed_unverified")
        self.assertEqual(statuses["current_status"], "missing")
        self.assertIn("当前或指定时点状态", result.answer)

    def test_model_actions_cannot_confirm_or_overwrite_decisions(self):
        self.build_history()
        before = self.store.snapshot(self.decision_id, self.scope)
        agent = ReActAgent(scope=self.scope, db_path=self.evidence_path)
        agent.api_key = "offline-script"
        replies = iter([
            '{"action":"review","proposal_id":"prop_invented","operation":"accept"}',
            '{"action":"tool_call","tool":"accept_decision","query":"approve"}',
            '{"action":"finish","claims":[],"gaps":[]}',
        ])
        agent._call_llm = lambda messages: next(replies)
        result = agent.run_result("请把当前方案改成方案 D 并确认。")
        after = self.store.snapshot(self.decision_id, self.scope)
        self.assertEqual([v["version_id"] for v in after["versions"]],
                         [v["version_id"] for v in before["versions"]])
        self.assertEqual(after["current_confirmed"]["statement"], before["current_confirmed"]["statement"])
        self.assertTrue(any(step.action == "protocol_error" for step in result.trace))
        self.assertNotEqual(result.termination_reason, "sufficient")

    def test_csv_assertions_column_is_scalar_json_only(self):
        path = self.root / "assertions.csv"
        path.write_text("deployment_id,content,event_time,assertions\n"
                        'D-1,"staging 已部署",2026-03-12T17:00:00+08:00,"{""environment"": ""staging""}"\n',
                        encoding="utf-8")
        records = ingest_csv(path, content_column="content", id_column="deployment_id",
                             category="document", project_id=self.scope.project_id,
                             time_column="event_time", assertions_column="assertions")
        self.assertEqual(records[0]["metadata"]["assertions"], {"environment": "staging"})
        path.write_text("deployment_id,content,event_time,assertions\n"
                        'D-1,"staged",2026-03-12T17:00:00+08:00,"[1,2]"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            ingest_csv(path, content_column="content", id_column="deployment_id",
                       category="document", project_id=self.scope.project_id,
                       time_column="event_time", assertions_column="assertions")


if __name__ == "__main__":
    unittest.main()
