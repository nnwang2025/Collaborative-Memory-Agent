"""Offline regression tests for structured team-collaboration ingestion.

These tests use synthetic CSV rows only. They do not call a model, a remote
platform, or any real chat/meeting export.
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.agents.react_agent import ReActAgent
from src.evidence import EvidenceScope, sha256_text, stable_evidence_id
from src.evidence_store import EvidenceStore
from src.ingest import ingest_all
from src.investigation import EvidenceLedger, Filters, ModelReply, TokenUsage
from src.investigation_context import source_coverage
from src.retrieval import retrieve_evidence

from offline_env import OFFLINE_PROVIDER_ENV


class StructuredIngestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cma-structured-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "evidence.sqlite3"
        self.scope = EvidenceScope(dataset_kind="live", project_id="team-demo")
        env = patch.dict(os.environ, OFFLINE_PROVIDER_ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def write(self, name, header, rows):
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def spec(self, path, category, id_column, **extra):
        return {"path": path, "category": category, "content_column": "content",
                "id_column": id_column, "time_column": "event_time",
                "sender_column": extra.pop("sender_column", None),
                "channel_column": extra.pop("channel_column", None),
                "subject_column": extra.pop("subject_column", None),
                "source_ref_column": "source_ref", **extra}

    def ingest(self, specs, project_id=None):
        return ingest_all(csv_files=specs, project_id=project_id or self.scope.project_id,
                          db_path=self.db)

    def records(self, project_id=None):
        scope = EvidenceScope(dataset_kind="live", project_id=project_id or self.scope.project_id)
        return {record.original_record_id: record for record in EvidenceStore(self.db).list(scope)}

    def test_categories_map_to_source_types_and_preserve_provenance(self):
        chat = self.write("chats.csv", ["message_id", "content", "event_time", "sender", "channel", "subject", "source_ref"],
                          [["C-1", "聊天讨论向量数据库。", "2026-01-02T09:00:00+08:00", "alice", "team", "检索", "chat://C-1"]])
        meeting = self.write("meetings.csv", ["meeting_id", "content", "event_time", "chair", "channel", "topic", "source_ref"],
                             [["M-1", "会议决定先保留 BM25。", "2026-01-03T14:00:00+08:00", "bob", "arch", "架构评审", "meeting://M-1"]])
        issue = self.write("issues.csv", ["issue_id", "content", "event_time", "reporter", "channel", "subject", "source_ref"],
                           [["I-1", "缺时间记录可能被误判。", "2026-01-04T10:00:00+08:00", "carol", "tracker", "时间语义", "issue://I-1"]])
        document = self.write("docs.csv", ["document_id", "content", "event_time", "author", "channel", "subject", "source_ref"],
                              [["D-1", "设计修订说明。", "2026-01-05T11:00:00+08:00", "dave", "docs", "设计", "doc://D-1"]])
        counts = self.ingest([
            self.spec(chat, "chat", "message_id", sender_column="sender", channel_column="channel", subject_column="subject"),
            self.spec(meeting, "meeting", "meeting_id", sender_column="chair", channel_column="channel", subject_column="topic"),
            self.spec(issue, "issue", "issue_id", sender_column="reporter", channel_column="channel", subject_column="subject"),
            self.spec(document, "document", "document_id", sender_column="author", channel_column="channel", subject_column="subject"),
        ])
        self.assertEqual(counts["total_records"], 4)
        records = self.records()
        self.assertEqual({r.source_type for r in records.values()}, {"chat", "meeting", "issue", "document"})
        self.assertEqual(records["C-1"].event_time.utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(records["M-1"].metadata["sender"], "bob")
        self.assertEqual(records["M-1"].metadata["subject"], "架构评审")
        self.assertEqual(records["M-1"].metadata["source_ref"], "meeting://M-1")
        self.assertEqual(records["M-1"].locator["source_ref"], "meeting://M-1")
        self.assertIn("sender: bob", records["M-1"].content)
        self.assertEqual(records["M-1"].content_hash, sha256_text(records["M-1"].content))
        self.assertEqual(records["M-1"].evidence_id, stable_evidence_id(
            self.scope.project_id, "live", "meeting", meeting.as_uri(), "M-1"))

    def test_duplicate_sync_is_idempotent_and_content_update_keeps_identity(self):
        path = self.write("chats.csv", ["message_id", "content", "event_time", "sender", "channel", "subject", "source_ref"],
                          [["C-1", "旧内容：Q2 上线向量数据库。", "2026-02-10T11:00:00+08:00", "alice", "team", "计划", "chat://C-1"]])
        spec = self.spec(path, "chat", "message_id", sender_column="sender",
                         channel_column="channel", subject_column="subject")
        first = self.ingest([spec])
        second = self.ingest([spec])
        self.assertEqual(first["appended_observations"], 1)
        self.assertEqual(second["appended_observations"], 0)
        original = self.records()["C-1"]
        old_hash, old_id = original.content_hash, original.evidence_id

        self.write("chats.csv", ["message_id", "content", "event_time", "sender", "channel", "subject", "source_ref"],
                   [["C-1", "修订内容：取消向量数据库迁移。", "2026-02-10T11:00:00+08:00", "alice", "team", "计划", "chat://C-1"]])
        third = self.ingest([spec])
        self.assertEqual(third["appended_observations"], 1)
        current = self.records()["C-1"]
        self.assertEqual(current.evidence_id, old_id)
        self.assertNotEqual(current.content_hash, old_hash)
        self.assertIn("修订内容", current.content)
        old = EvidenceStore(self.db).get(old_id, self.scope, old_hash)
        self.assertIsNotNone(old)
        self.assertIn("旧内容", old.content)

    def test_invalid_or_missing_time_and_duplicate_ids_are_not_guessed(self):
        bad = self.write("bad.csv", ["message_id", "content", "event_time"],
                         [["C-1", "没有时区的时间。", "2026-01-01 09:00:00"]])
        with self.assertRaises(ValueError):
            self.ingest([self.spec(bad, "chat", "message_id")])
        self.assertFalse(self.db.exists())  # validation precedes the append transaction

        missing = self.write("missing.csv", ["message_id", "content", "event_time"],
                             [["C-2", "没有时间的记录。", ""]])
        with self.assertRaises(ValueError):
            self.ingest([{"path": missing}])  # category is never silently assumed
        self.ingest([self.spec(missing, "chat", "message_id")])
        record = self.records()["C-2"]
        self.assertIsNone(record.event_time)
        self.assertEqual(record.metadata["event_time_status"], "unknown")

        duplicate = self.write("duplicate.csv", ["message_id", "content", "event_time"],
                               [["C-3", "第一行。", "2026-01-02T09:00:00+08:00"],
                                ["C-3", "第二行。", "2026-01-02T09:01:00+08:00"]])
        with self.assertRaises(ValueError):
            self.ingest([self.spec(duplicate, "chat", "message_id")])

        blank_id = self.write("blank_id.csv", ["message_id", "content", "event_time"],
                              [["", "缺少ID的记录。", "2026-01-02T09:00:00+08:00"]])
        with self.assertRaises(ValueError):
            self.ingest([self.spec(blank_id, "chat", "message_id")])

    def test_scope_isolation_and_live_filters(self):
        path = self.write("chats.csv", ["message_id", "content", "event_time", "sender", "channel", "subject", "source_ref"],
                          [["C-1", "Q2 迁移向量数据库。", "", "alice", "team", "历史", "chat://C-1"],
                           ["C-2", "Q2 取消向量数据库。", "2026-03-12T15:00:00+08:00", "bob", "team", "变更", "chat://C-2"]])
        spec = self.spec(path, "chat", "message_id", sender_column="sender",
                         channel_column="channel", subject_column="subject")
        self.ingest([spec], project_id="project-a")
        self.ingest([spec], project_id="project-b")
        records_a = self.records("project-a")
        records_b = self.records("project-b")
        self.assertEqual(set(records_a), {"C-1", "C-2"})
        self.assertEqual(set(records_b), {"C-1", "C-2"})
        self.assertFalse({r.evidence_id for r in records_a.values()} & {r.evidence_id for r in records_b.values()})

        scope_a = EvidenceScope(dataset_kind="live", project_id="project-a")
        hits = retrieve_evidence("Q2 向量数据库", scope=scope_a, db_path=self.db)
        self.assertTrue(hits)
        self.assertTrue(all(hit["project_id"] == "project-a" for hit in hits))
        undated = retrieve_evidence("Q2 迁移 向量数据库", scope=scope_a, db_path=self.db,
                                    filters=Filters(undated_only=True))
        self.assertEqual({hit["original_record_id"] for hit in undated}, {"C-1"})
        dated = retrieve_evidence("Q2 迁移 向量数据库", scope=scope_a, db_path=self.db,
                                  filters=Filters(after="2026-02-01T00:00:00+08:00"))
        self.assertNotIn("C-1", {hit["original_record_id"] for hit in dated})
        meetings = retrieve_evidence("Q2 向量数据库", scope=scope_a, db_path=self.db,
                                     filters=Filters(source_types=["meeting"]))
        self.assertEqual(meetings, [])

    def test_directory_category_override_preserves_whole_file_and_unknown_time(self):
        docs = self.root / "minutes"
        docs.mkdir()
        (docs / "2026-01-15-review.md").write_text("# 评审会\n\n结论：保留 BM25。", encoding="utf-8")
        counts = ingest_all(docs_dir=docs, project_id=self.scope.project_id, db_path=self.db,
                            docs_category="meeting")
        self.assertEqual(counts["total_records"], 1)
        record = next(iter(self.records().values()))
        self.assertEqual(record.source_type, "meeting")
        self.assertIsNone(record.event_time)
        self.assertEqual(record.metadata["event_time_status"], "unknown")
        self.assertIn("结论：保留 BM25。", record.content)
        self.assertEqual(record.locator["path"], "2026-01-15-review.md")

    def test_meeting_is_a_protocol_filter_and_coverage_source(self):
        self.assertEqual(Filters(source_types=["meeting"]).source_types, ["meeting"])
        meeting = self.write("meetings.csv", ["meeting_id", "content", "event_time", "chair", "channel", "topic", "source_ref"],
                             [["M-1", "会议结论：保留 BM25。", "2026-01-15T14:00:00+08:00", "bob", "arch", "架构评审", "meeting://M-1"]])
        self.ingest([self.spec(meeting, "meeting", "meeting_id", sender_column="chair",
                               channel_column="channel", subject_column="topic")])
        record = self.records()["M-1"]
        ledger = EvidenceLedger(self.scope)
        ledger.register([record.as_hit()], "search_memory")
        coverage = source_coverage("请结合 meeting 和 chat 说明", ledger)
        self.assertIn("meeting", coverage["observed_source_types"])
        self.assertEqual(coverage["missing_source_types"], ["chat"])

    def test_react_time_assertion_from_undated_evidence_is_insufficient(self):
        path = self.write("undated.csv", ["message_id", "content", "event_time"],
                          [["C-1", "旧提议：Q2 迁移到向量数据库。", ""]])
        self.ingest([self.spec(path, "chat", "message_id")])
        record = self.records()["C-1"]
        tool = {"action": "tool_call", "tool": "search_memory", "query": "Q2 迁移 向量数据库"}
        finish = {"action": "finish", "claims": [{"text": "旧提议：Q2 迁移到向量数据库。", "refs": [
            {"evidence_id": record.evidence_id, "content_hash": record.content_hash}]}]}
        agent = ReActAgent(scope=self.scope, db_path=self.db)
        agent.api_key = "offline-scripted-only"
        agent._call_llm = Mock(side_effect=[self.reply(tool), self.reply(finish)])
        result = agent.run_result("Q2 是否迁移向量数据库？")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertTrue(any("事件时间未知" in gap for gap in result.gaps))

    def test_declared_conflict_clears_supported_claims(self):
        path = self.write("conflict.csv", ["message_id", "content", "event_time", "sender", "channel", "subject", "source_ref"],
                          [["C-1", "Q2 上线向量数据库。", "2026-02-10T11:00:00+08:00", "alice", "team", "计划", "chat://C-1"],
                           ["C-2", "Q2 不迁移向量数据库。", "2026-02-10T11:00:00+08:00", "bob", "team", "计划", "chat://C-2"]])
        self.ingest([self.spec(path, "chat", "message_id", sender_column="sender",
                               channel_column="channel", subject_column="subject")])
        records = self.records()
        first, second = records["C-1"], records["C-2"]
        tool = {"action": "tool_call", "tool": "search_memory", "query": "Q2 向量数据库 计划"}
        finish = {"action": "finish",
                  "claims": [{"text": "Q2 上线向量数据库。", "refs": [
                      {"evidence_id": first.evidence_id, "content_hash": first.content_hash}]}],
                  "conflicts": [{"description": "两份记录对 Q2 计划说法相反。", "refs": [
                      {"evidence_id": first.evidence_id, "content_hash": first.content_hash},
                      {"evidence_id": second.evidence_id, "content_hash": second.content_hash}]}]}
        agent = ReActAgent(scope=self.scope, db_path=self.db)
        agent.api_key = "offline-scripted-only"
        agent._call_llm = Mock(side_effect=[self.reply(tool), self.reply(finish)])
        result = agent.run_result("Q2 向量数据库计划是什么？")
        self.assertEqual(result.termination_reason, "conflict")
        self.assertEqual(result.claims, [])
        self.assertEqual(len(result.conflicts), 1)
        self.assertIn("冲突", result.answer)

    def test_synthetic_team_story_imports_through_formal_chain(self):
        from examples.team_collaboration.seed_demo import csv_specs
        counts = ingest_all(csv_files=csv_specs(), project_id="story-project", db_path=self.db)
        self.assertEqual(counts["total_records"], 19)
        records = self.records("story-project")
        self.assertEqual(records["MTG-001"].source_type, "meeting")
        self.assertIsNone(records["CHAT-003"].event_time)
        self.assertEqual(records["MTG-003"].source_type, "meeting")
        self.assertEqual(records["ISS-002"].source_type, "issue")
        self.assertEqual(records["DEC-001"].source_type, "document")
        scope = EvidenceScope(dataset_kind="live", project_id="story-project")
        hits = retrieve_evidence("向量数据库", scope=scope, db_path=self.db, top_k=10)
        self.assertTrue({"meeting", "chat", "document"} <= {hit["source_type"] for hit in hits})
        undated = retrieve_evidence("Q2 迁移 向量数据库", scope=scope, db_path=self.db,
                                    filters=Filters(undated_only=True))
        self.assertEqual({hit["original_record_id"] for hit in undated}, {"CHAT-003"})

    @staticmethod
    def reply(value):
        return ModelReply(text=json.dumps(value, ensure_ascii=False), finish_reason="stop",
                          usage=TokenUsage(input_tokens=30, output_tokens=30, estimated=False))


if __name__ == "__main__":
    unittest.main()
