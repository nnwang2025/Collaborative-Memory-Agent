"""Historical state evolution vs. genuine assertion conflicts.

These tests are offline and synthetic except for the final replay, which uses
the frozen V4 snapshot's records only.  No provider request is made.
"""
from __future__ import annotations

import unittest

from benchmark.v4.dataset import load as load_v4
from src.agents.answer_agent import AnswerAgent
from src.agents.react_agent import finish_termination_reason
from src.evidence import EvidenceScope
from src.ingest import normalize_record
from src.investigation import Claim, Conflict, EvidenceLedger, EvidenceRef, FinishAction


PROJECT = "state-evolution-project"
SCOPE = EvidenceScope(project_id=PROJECT, dataset_kind="live")


def _record(content, record_id, event_time, *, role=None, assertions=None,
            related_issue=None, metric_name=None):
    metadata = {}
    if role:
        metadata["record_role"] = role
    if assertions is not None:
        metadata["assertions"] = assertions
    if related_issue:
        metadata["related_issue"] = related_issue
    if metric_name:
        metadata["metric_name"] = metric_name
    return normalize_record(
        content, "review://" + record_id, "document", record_id, metadata,
        project_id=PROJECT, event_time=event_time)


def _ref(record):
    return EvidenceRef(evidence_id=record["evidence_id"], content_hash=record["content_hash"])


def _claim(record):
    return Claim(text=record["content"], refs=[_ref(record)])


class StateEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.agent = AnswerAgent()
        self.records = {}

    def register(self, records):
        self.records = {record["evidence_id"]: record for record in records}
        ledger = EvidenceLedger(SCOPE)
        ledger.register(records, "search_docs")
        return ledger

    def getter(self, evidence_id, content_hash):
        record = self.records.get(evidence_id)
        return record if record is not None and record["content_hash"] == content_hash else None

    def compose(self, ledger, finish):
        return self.agent.compose_investigation(ledger, finish, self.getter)

    def change_pair(self):
        original = _record(
            "Issue 关闭为重复：保留原始记录，不改变 batch_size 的结论。",
            "ISSUE-DUP", "2025-01-18T19:00:00+00:00",
            role="issue_duplicate", assertions={"batch_size": "500"},
            related_issue="ISSUE-1", metric_name="batch_size")
        recovered = _record(
            "日志：调整后 batch_size=2000，服务恢复。",
            "LOG-RECOVERY", "2025-01-18T17:00:00+00:00",
            role="log_recovery", assertions={"batch_size": "2000"},
            related_issue="ISSUE-1", metric_name="batch_size")
        return original, recovered

    def test_same_day_explicit_pre_post_is_state_evolution_not_conflict(self):
        original, recovered = self.change_pair()
        ledger = self.register([original, recovered])
        self.assertEqual(ledger.conflicts, [])
        self.assertEqual(len(ledger.state_evolutions), 1)
        evolution = ledger.state_evolutions[0]
        self.assertEqual(evolution.status, "state_evolution")
        self.assertEqual(evolution.fields, ["batch_size"])
        self.assertIn("issue_duplicate", evolution.description)
        self.assertIn("log_recovery", evolution.description)

        claims, gaps, conflicts, counts = self.compose(
            ledger, FinishAction(claims=[_claim(recovered)]))
        self.assertEqual(conflicts, [])
        self.assertEqual(counts["claims_accepted"], 1)
        self.assertEqual(counts["claims_discarded_by_conflict"], 0)
        self.assertEqual(claims[0].verification_status, "supported_extract")
        self.assertEqual(finish_termination_reason(conflicts, gaps, claims), "sufficient")
        answer = self.agent.render_investigation(
            ledger, claims, gaps, conflicts, "sufficient",
            state_evolutions=ledger.state_evolutions)
        self.assertIn("历史状态演进", answer)
        self.assertIn("非竞争性冲突", answer)

    def test_same_day_genuine_competing_assertions_remain_conflict(self):
        first = _record("Atlas mode 记录 A。", "MODE-A", "2025-01-01T10:00:00+00:00",
                        assertions={"mode": "A"})
        second = _record("Atlas mode 记录 B。", "MODE-B", "2025-01-01T10:10:00+00:00",
                         assertions={"mode": "B"})
        ledger = self.register([first, second])
        self.assertEqual(len(ledger.conflicts), 1)
        self.assertEqual(ledger.conflicts[0].status, "undetermined")
        self.assertEqual(ledger.conflicts[0].fields, ["mode"])
        self.assertEqual(ledger.state_evolutions, [])

        claims, gaps, conflicts, counts = self.compose(
            ledger, FinishAction(claims=[_claim(first)]))
        self.assertEqual(claims, [])
        self.assertEqual(counts["claims_discarded_by_conflict"], 1)
        self.assertEqual(conflicts[0].status, "undetermined")
        self.assertEqual(finish_termination_reason(conflicts, gaps, claims), "conflict")

    def test_different_time_same_effective_version_is_conflict(self):
        fixed = _record("提交：batch_size 调整为 2000。", "COMMIT-FIX",
                        "2025-01-17T19:00:00+00:00", role="commit_fix",
                        assertions={"batch_size": "2000"}, related_issue="ISSUE-2",
                        metric_name="batch_size")
        config = _record("配置文件：batch_size: 3000。", "CONFIG",
                         "2025-01-19T21:00:00+00:00", role="code_config",
                         assertions={"batch_size": "3000"}, related_issue="ISSUE-2",
                         metric_name="batch_size")
        ledger = self.register([fixed, config])
        self.assertEqual(len(ledger.conflicts), 1)
        self.assertEqual(ledger.conflicts[0].status, "competing")
        self.assertEqual(ledger.conflicts[0].fields, ["batch_size"])
        self.assertEqual(ledger.state_evolutions, [])

    def test_different_time_unclear_state_semantics_stays_undetermined(self):
        changed = _record("调整后 batch_size=2000。", "CHANGED",
                          "2025-01-18T17:00:00+00:00",
                          assertions={"batch_size": "2000"}, related_issue="ISSUE-3",
                          metric_name="batch_size")
        unclear = _record("batch_size=500 的记录。", "UNCLEAR",
                          "2025-01-18T19:00:00+00:00",
                          assertions={"batch_size": "500"}, related_issue="ISSUE-3",
                          metric_name="batch_size")
        ledger = self.register([changed, unclear])
        self.assertEqual(len(ledger.conflicts), 1)
        self.assertEqual(ledger.conflicts[0].status, "undetermined")
        self.assertEqual(ledger.state_evolutions, [])

        claims, gaps, conflicts, counts = self.compose(
            ledger, FinishAction(claims=[_claim(changed)]))
        self.assertEqual(claims, [])
        self.assertEqual(counts["claims_discarded_by_conflict"], 1)
        self.assertEqual(finish_termination_reason(conflicts, gaps, claims), "conflict")

    def test_time_order_alone_does_not_create_state_evolution(self):
        older = _record("batch_size=500 的记录。", "TIME-OLD",
                        "2025-01-01T00:00:00+00:00",
                        assertions={"batch_size": "500"}, related_issue="ISSUE-5",
                        metric_name="batch_size")
        newer = _record("batch_size=2000 的记录。", "TIME-NEW",
                        "2025-01-02T00:00:00+00:00",
                        assertions={"batch_size": "2000"}, related_issue="ISSUE-5",
                        metric_name="batch_size")
        ledger = self.register([older, newer])
        self.assertEqual(len(ledger.conflicts), 1)
        self.assertEqual(ledger.conflicts[0].status, "undetermined")
        self.assertEqual(ledger.state_evolutions, [])

    def test_model_reported_duplicate_of_state_evolution_is_not_a_conflict(self):
        original, recovered = self.change_pair()
        ledger = self.register([original, recovered])
        model_conflict = Conflict(
            description="模型报告 batch_size 不一致。",
            refs=[_ref(original), _ref(recovered)])
        claims, gaps, conflicts, counts = self.compose(
            ledger, FinishAction(claims=[_claim(recovered)], conflicts=[model_conflict]))
        self.assertEqual(conflicts, [])
        self.assertEqual(counts["claims_accepted"], 1)
        self.assertEqual(len(ledger.state_evolutions), 1)
        self.assertEqual(finish_termination_reason(conflicts, gaps, claims), "sufficient")

    def test_model_reported_duplicate_of_structured_conflict_is_deduped(self):
        first = _record("Atlas mode 记录 A。", "DUP-A", "2025-01-01T10:00:00+00:00",
                        assertions={"mode": "A"})
        second = _record("Atlas mode 记录 B。", "DUP-B", "2025-01-01T10:10:00+00:00",
                         assertions={"mode": "B"})
        ledger = self.register([first, second])
        model_conflict = Conflict(
            description="模型也报告 mode 不一致。",
            refs=[_ref(first), _ref(second)])
        claims, gaps, conflicts, counts = self.compose(
            ledger, FinishAction(conflicts=[model_conflict]))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].origin, "structured_assertion")
        self.assertEqual(conflicts[0].status, "undetermined")
        self.assertEqual(finish_termination_reason(conflicts, gaps, claims), "conflict")

    def test_independent_valid_claim_is_retained_while_conflict_remains(self):
        first = _record("Atlas mode 记录 A。", "IND-A", "2025-01-01T10:00:00+00:00",
                        assertions={"mode": "A"})
        second = _record("Atlas mode 记录 B。", "IND-B", "2025-01-01T10:10:00+00:00",
                         assertions={"mode": "B"})
        independent = _record("独立记录：p99 延迟为 120ms。", "IND-C",
                              "2025-01-02T10:00:00+00:00",
                              assertions={"latency": "120ms"},
                              related_issue="ISSUE-4")
        ledger = self.register([first, second, independent])
        self.assertEqual(len(ledger.conflicts), 1)
        claims, gaps, conflicts, counts = self.compose(
            ledger, FinishAction(claims=[_claim(independent)]))
        self.assertEqual(counts["claims_accepted"], 1)
        self.assertEqual(counts["claims_discarded_by_conflict"], 0)
        self.assertEqual(claims[0].verification_status, "supported_extract")
        self.assertEqual(conflicts[0].relation, "independent_of_submitted_claims")
        self.assertEqual(finish_termination_reason(conflicts, gaps, claims), "sufficient")

    def test_third_run_evidence_replays_as_state_evolution(self):
        snapshot = load_v4()
        by_issue = {}
        for record in snapshot.records:
            issue = record.metadata.get("related_issue")
            role = record.metadata.get("record_role")
            if issue and role:
                by_issue.setdefault(issue, []).append(record)
        issue, records = next(
            (issue, records) for issue, records in by_issue.items()
            if {"issue_duplicate", "log_recovery", "issue_fix", "log_error"} <= {
                record.metadata.get("record_role") for record in records})
        records = [record for record in records
                   if record.metadata.get("record_role") in
                   {"issue_open", "issue_triage", "issue_fix", "issue_duplicate",
                    "log_error", "log_recovery"}]
        self.assertGreaterEqual(len(records), 5)
        payloads = [record.model_dump(mode="json") for record in records]
        self.records = {payload["evidence_id"]: payload for payload in payloads}
        ledger = EvidenceLedger(EvidenceScope(project_id=records[0].project_id,
                                              dataset_kind=records[0].dataset_kind))
        ledger.register(payloads, "search_issues")
        self.assertEqual(ledger.conflicts, [])
        self.assertGreaterEqual(len(ledger.state_evolutions), 1)
        by_role = {record.metadata["record_role"]: record for record in records}
        duplicate = by_role["issue_duplicate"]
        recovery = by_role["log_recovery"]
        model_conflict = Conflict(
            description="同一事件日 batch_size 不一致；两份记录都保留。",
            refs=[EvidenceRef(evidence_id=duplicate.evidence_id,
                              content_hash=duplicate.content_hash),
                  EvidenceRef(evidence_id=recovery.evidence_id,
                              content_hash=recovery.content_hash)])
        claims, gaps, conflicts, counts = self.compose(
            ledger, FinishAction(claims=[_claim(record.model_dump(mode="json"))
                                         for record in records],
                                 conflicts=[model_conflict]))
        self.assertEqual(conflicts, [])
        self.assertEqual(counts["claims_accepted"], len(records))
        self.assertEqual(counts["claims_discarded_by_conflict"], 0)
        self.assertEqual(finish_termination_reason(conflicts, gaps, claims), "sufficient")
        self.assertEqual(
            sum(1 for evolution in ledger.state_evolutions
                if {ref.evidence_id for ref in evolution.refs} ==
                {duplicate.evidence_id, recovery.evidence_id}), 1)


if __name__ == "__main__":
    unittest.main()
