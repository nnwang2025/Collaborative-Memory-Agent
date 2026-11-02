"""Offline regressions for answer synthesis and decision-binding reliability."""
import json
import tempfile
import unittest
from pathlib import Path

from src.agents.answer_agent import AnswerAgent
from src.agents.react_agent import ReActAgent
from src.evidence import EvidenceScope
from src.ingest import normalize_record
from src.investigation import (Budget, Claim, EvidenceLedger, EvidenceRef, FinishAction,
                               ModelReply, TokenUsage)
from src.investigation_context import bind_decisions, task_coverage

PROJECT = "synthesis-fixture"


def _decision(decision_id, version_id, title, statement, *, current=False):
    return {"decision_id": decision_id, "version_id": version_id, "title": title,
            "statement": statement, "status": "confirmed", "system_current": current,
            "decision_conflicts": []}


class SynthesisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")
        cls.chat = normalize_record(
            "讨论记录：为了避免同一账户的重复写入，决定采用固定分区归属 B。",
            "fixture://chat-1", "chat", "chat-1", {},
            project_id=PROJECT, source_type="chat",
            event_time="2025-01-01T00:00:00+00:00")
        sha = "0123456789abcdef0123456789abcdef01234567"
        cls.commit = normalize_record(
            f"commit {sha}\nmessage: 实现账号顺序使用分区归属 B。",
            "fixture://repository", "git_commit", sha, {},
            project_id=PROJECT, source_type="git_commit",
            event_time="2025-01-02T00:00:00+00:00",
            locator={"commit_sha": sha, "repository_uri": "fixture://repository"})
        cls.unrelated = normalize_record(
            "无关日志：TLS handshake failure on an unrelated host.",
            "fixture://log-1", "log", "log-1", {},
            project_id=PROJECT, source_type="log",
            event_time="2025-01-03T00:00:00+00:00")
        cls.records = {item["evidence_id"]: item
                       for item in (cls.chat, cls.commit, cls.unrelated)}

    def ledger(self):
        ledger = EvidenceLedger(self.scope)
        ledger.register([self.chat, self.commit, self.unrelated], "search_memory")
        return ledger

    def ref(self, record):
        return EvidenceRef(evidence_id=record["evidence_id"],
                           content_hash=record["content_hash"])

    def compose(self, ledger, records):
        finish = FinishAction(claims=[
            Claim(text=record["content"], refs=[self.ref(record)]) for record in records
        ])
        return AnswerAgent().compose_investigation(
            ledger, finish, lambda evidence_id, content_hash: self.records.get(evidence_id))

    def test_synthesis_links_validated_sources_without_asserting_implementation(self):
        ledger = self.ledger()
        claims, gaps, conflicts, _ = self.compose(ledger, [self.chat, self.commit])
        agent = AnswerAgent()
        synthesis = agent.build_synthesis(
            ledger, claims, gaps, conflicts, "sufficient", "为什么采用 B，讨论与哪个提交对应？")
        answer = agent.render_investigation(
            ledger, claims, gaps, conflicts, "sufficient", synthesis=synthesis)

        cross = [item for item in synthesis.statements
                 if item.basis == "validated_cross_source"]
        self.assertEqual(len(cross), 1)
        self.assertEqual(len(cross[0].evidence_refs), 2)
        self.assertEqual(len(set(cross[0].claim_indexes)), 2)
        self.assertEqual(set(cross[0].source_types), {"chat", "git_commit"})
        self.assertIn("提交 01234567 的提交信息为", answer)
        self.assertIn("仅提交信息；未核验代码 diff", answer)
        self.assertIn("不能据此确认代码实际实现", answer)
        self.assertIn("【综合回答】", answer)
        self.assertIn("为什么采用 B", answer)
        self.assertIn(self.chat["evidence_id"], answer)
        self.assertIn(self.commit["evidence_id"], answer)
        # An uncited ledger item is not dumped into the default user answer.
        self.assertNotIn(self.unrelated["content"], answer)
        self.assertIn(self.unrelated["content"],
                      "\n".join(entry.evidence.content for entry in ledger.entries))

    def test_pending_claim_is_not_presented_as_confirmed(self):
        ledger = self.ledger()
        claims, gaps, conflicts, _ = self.compose(ledger, [self.chat])
        claims[0].verification_status = "pending_unattributable_conflict"
        agent = AnswerAgent()
        synthesis = agent.build_synthesis(ledger, claims, gaps, conflicts, "conflict", "q")
        answer = agent.render_investigation(
            ledger, claims, gaps, conflicts, "conflict", synthesis=synthesis)
        self.assertEqual(synthesis.statements, [])
        self.assertIn("本轮没有通过原文摘录与引用校验的结论", answer)
        self.assertNotIn("历史来源记载：" + self.chat["content"], answer)

    @staticmethod
    def decision_memories():
        return [
            {"version_id": "dv_hist", "status": "confirmed", "statement": "采用方案 B",
             "event_time": "2025-01-01T00:00:00+00:00",
             "decision_known_at": "2023-01-10T00:00:00+00:00",
             "recorded_at": "2023-01-10T00:00:00+00:00",
             "knowledge_cutoff": "2025-02-20T23:59:59+00:00", "system_current": True,
             "execution_status": "unknown", "world_state": "unknown"},
            {"version_id": "dv_current", "status": "confirmed", "statement": "采用方案 C",
             "event_time": "2025-02-10T00:00:00+00:00",
             "decision_known_at": "2025-03-20T00:00:00+00:00",
             "recorded_at": "2025-03-20T00:00:00+00:00",
             "knowledge_cutoff": None, "system_current": True,
             "execution_status": "unknown", "world_state": "unknown"},
        ]

    def test_historical_and_current_decision_views_are_labelled_separately(self):
        agent = AnswerAgent()
        records = self.decision_memories()
        answer = agent.render_decision_memories(records)
        self.assertIn("历史截止视图", answer)
        self.assertIn("截至 2025-02-20T23:59:59+00:00", answer)
        self.assertIn("不属于历史截止视图", answer)
        self.assertIn("不表示同一时点存在两个当前版本", answer)
        # Original query results are preserved verbatim, not rewritten.
        for record in records:
            self.assertIn(record["version_id"], answer)
            self.assertIn(record["statement"], answer)

    def test_cutoff_synthesis_states_the_confirmed_version_and_later_candidate(self):
        agent = AnswerAgent()
        synthesis = agent.build_synthesis(
            EvidenceLedger(self.scope), [], [], [], "insufficient",
            "截至 2025-02-20 当时采用哪一版？C 是否已替代 B？",
            decision_memories=self.decision_memories())
        snapshot = [item for item in synthesis.statements
                    if item.basis == "validated_decision_snapshot"]
        self.assertEqual(len(snapshot), 3)
        body = "\n".join(item.text for item in snapshot)
        self.assertIn("截至 2025-02-20", body)
        self.assertIn("dv_hist", body)
        self.assertIn("采用方案 B", body)
        self.assertIn("status=confirmed", body)
        self.assertIn("dv_current", body)
        self.assertIn("晚于截止时点", body)
        self.assertIn("未确认", body)
        self.assertIn("不属于历史截止视图", body)
        self.assertIn("execution_status 与 world_state 均为 unknown", body)
        rendered = agent.render_synthesis(synthesis)
        self.assertIn("不是原始 Evidence 的逐字 Claim", rendered)
        self.assertIn("现实状态未验证", rendered)

    def test_no_verified_claim_yields_explicit_unconfirmed_answer(self):
        ledger = self.ledger()
        synthesis = AnswerAgent().build_synthesis(
            ledger, [], ["证据不足。"], [], "insufficient", "q")
        answer = AnswerAgent().render_synthesis(synthesis)
        self.assertIn("不能确认该问题的答案", answer)
        self.assertIn("未确认部分", answer)


class DecisionBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scope = EvidenceScope(project_id=PROJECT, dataset_kind="live")
        cls.chat = normalize_record(
            "讨论记录：为了避免同一账户的重复写入，决定采用固定分区归属 B。",
            "fixture://chat-1", "chat", "chat-1", {},
            project_id=PROJECT, source_type="chat",
            event_time="2025-01-01T00:00:00+00:00")
        cls.records = {cls.chat["evidence_id"]: cls.chat}
        cls.b = _decision("dec_" + "b" * 32, "dv_" + "b" * 32,
                          "Atlas 分区归属", "采用固定分区归属 B，原因是同一账户写入需要有序。",
                          current=True)
        cls.c = _decision("dec_" + "c" * 32, "dv_" + "c" * 32,
                          "Atlas 排序方案", "采用基于 fencing token 的租约迁移 C。",
                          current=True)

    def coverage(self, question, decisions):
        ledger = EvidenceLedger(self.scope)
        ledger.register([self.chat], "search_memory")
        return task_coverage(question, ledger, decisions, gaps=())

    def test_retrieved_unrelated_decision_does_not_mark_requested_target(self):
        question = "Atlas 为什么采用固定分区归属 B？同一账户写入有序的讨论与哪个提交相互印证？"
        bound, unbound = bind_decisions(question, [self.c], PROJECT)
        self.assertEqual(bound, [])
        self.assertEqual([item["version_id"] for item in unbound], [self.c["version_id"]])
        coverage = self.coverage(question, [self.c])
        statuses = {item["target"]: item["status"] for item in coverage["targets"]}
        self.assertEqual(statuses["decision_formation"], "observed_unverified")
        self.assertEqual(coverage["decision_binding"]["bound"], [])
        self.assertEqual(coverage["decision_binding"]["unbound_version_ids"],
                         [self.c["version_id"]])
        authority = {item["level"]: item for item in coverage["authority_levels"]}
        self.assertFalse(authority["reviewed_decision"]["observed"])

    def test_matching_decision_is_bound_and_observed(self):
        question = "Atlas 为什么采用固定分区归属 B？同一账户写入有序的讨论与哪个提交相互印证？"
        bound, unbound = bind_decisions(question, [self.b, self.c], PROJECT)
        self.assertEqual([item["version_id"] for item in bound], [self.b["version_id"]])
        self.assertEqual([item["version_id"] for item in unbound], [self.c["version_id"]])
        coverage = self.coverage(question, [self.b, self.c])
        statuses = {item["target"]: item["status"] for item in coverage["targets"]}
        self.assertEqual(statuses["decision_formation"], "observed")
        self.assertTrue(coverage["decision_binding"]["bound"])

    def test_explicit_version_id_binds_even_without_topic_overlap(self):
        question = f"请说明 {self.c['version_id']} 的当前状态。"
        bound, unbound = bind_decisions(question, [self.b, self.c], PROJECT)
        self.assertEqual([item["version_id"] for item in bound], [self.c["version_id"]])
        self.assertEqual([item["version_id"] for item in unbound], [self.b["version_id"]])
        self.assertEqual(bound[0]["_binding_basis"], "explicit_id")

    def test_generic_words_alone_do_not_bind(self):
        question = "请说明方案决策采用的原因和当前状态。"
        bound, _ = bind_decisions(question, [self.b, self.c], PROJECT)
        self.assertEqual(bound, [])

    def test_bound_proposed_version_is_not_a_reviewed_decision(self):
        question = "Atlas 为什么采用固定分区归属 B？同一账户写入有序的讨论与哪个提交相互印证？"
        proposed = {**self.b, "version_id": "dv_" + "d" * 32,
                    "status": "proposed", "system_current": False}
        coverage = self.coverage(question, [proposed])
        statuses = {item["target"]: item["status"] for item in coverage["targets"]}
        self.assertEqual(statuses["decision_formation"], "observed_unverified")
        authority = {item["level"]: item for item in coverage["authority_levels"]}
        self.assertFalse(authority["reviewed_decision"]["observed"])


class OutputBudgetRetryTests(unittest.TestCase):
    def agent(self, temporary):
        return ReActAgent(
            scope=EvidenceScope(project_id=PROJECT, dataset_kind="live"),
            db_path=Path(temporary) / "evidence.sqlite3",
            budget=Budget(max_output_tokens=2048,
                          max_truncation_retry_output_tokens=4096))

    def test_truncated_action_gets_one_larger_retry_budget(self):
        with tempfile.TemporaryDirectory(prefix="cma-output-budget-") as temporary:
            agent = self.agent(temporary)
            agent.api_key = "scripted-model-no-network"
            replies = iter([
                ModelReply(text='{"action":"tool_call"', finish_reason="length",
                           usage=TokenUsage(input_tokens=20, output_tokens=2048, estimated=False),
                           reasoning_tokens=1900),
                ModelReply(text=FinishAction().model_dump_json(), finish_reason="stop",
                           usage=TokenUsage(input_tokens=20, output_tokens=20, estimated=False)),
            ])
            agent._call_llm = lambda messages: next(replies)
            result = agent.run_result("Atlas 固定分区归属 B 的讨论理由是什么？")
        first, second = result.trace[0], result.trace[1]
        self.assertEqual(first.requested_output_tokens, 2048)
        self.assertEqual(first.finish_reason, "length")
        self.assertEqual(first.protocol_category, "output_truncated")
        self.assertEqual(second.requested_output_tokens, 4096)
        self.assertEqual(second.recovery_kind, "output_budget_retry")
        self.assertEqual(second.finish_reason, "stop")

    def test_ordinary_protocol_error_does_not_raise_output_budget(self):
        with tempfile.TemporaryDirectory(prefix="cma-output-budget-") as temporary:
            agent = self.agent(temporary)
            agent.api_key = "scripted-model-no-network"
            replies = iter([
                ModelReply(text="not json", finish_reason="stop",
                           usage=TokenUsage(input_tokens=10, output_tokens=2, estimated=False)),
                ModelReply(text=FinishAction().model_dump_json(), finish_reason="stop",
                           usage=TokenUsage(input_tokens=10, output_tokens=20, estimated=False)),
            ])
            agent._call_llm = lambda messages: next(replies)
            result = agent.run_result("Atlas 固定分区归属 B 的讨论理由是什么？")
        first, second = result.trace[0], result.trace[1]
        self.assertEqual(first.protocol_category, "json_syntax")
        self.assertEqual(second.requested_output_tokens, 2048)
        self.assertIsNone(second.recovery_kind)


if __name__ == "__main__":
    unittest.main()
