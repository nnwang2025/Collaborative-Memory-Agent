"""Run three synthetic decision-investigation cases through the real live path.

This demo never writes through the read-only tool surface. It ingests synthetic
CSV records, asks the retrieval layer for real Evidence, then simulates an
explicit local human review (propose -> review) to create Decision Memory.
Answers are printed from read-only queries and coverage checks, not hardcoded.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from examples.team_collaboration.seed_demo import csv_specs
from src.agents.react_agent import ToolExecutor
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.ingest import ingest_all
from src.investigation import EvidenceLedger, EvidenceRef, Filters, InvestigationResult
from src.investigation_context import task_coverage
from src.memory.decisions import DecisionStore
from src.retrieval import retrieve_evidence


def observe(scope, hits, wanted, run_id):
    selected = [hit for hit in hits if hit.get("original_record_id") in wanted]
    missing = wanted - {hit.get("original_record_id") for hit in selected}
    if missing:
        raise RuntimeError(f"Retrieval did not return expected synthetic records: {sorted(missing)}")
    return selected, InvestigationResult(
        run_id=run_id, scope=scope, answer="Synthetic demo observation", termination_reason="sufficient",
        evidence_refs=[EvidenceRef(evidence_id=hit["evidence_id"], content_hash=hit["content_hash"])
                       for hit in selected], claims=[])


def print_timeline(title, timeline):
    print(f"\n{title}")
    for item in timeline:
        print(f"  {item['revision']}. {item['version_id']} status={item['status']} "
              f"event_time={item['event_time']} ({item['event_time_status']}) "
              f"decision_known_at={item['decision_known_at']} recorded_at={item['recorded_at']}")
        print(f"     {item['statement']}")
        print("     evidence: " + ", ".join(
            f"{ref['evidence_id']}@{ref['content_hash']}[{ref['relation_type']}]"
            for ref in item["evidence_refs"]))


def run(project_id, workdir):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    evidence_db = workdir / "evidence.sqlite3"
    decision_db = workdir / "decisions.sqlite3"
    if evidence_db.exists() or decision_db.exists():
        raise FileExistsError("Use a fresh --workdir; this demo never overwrites an existing database")
    scope = EvidenceScope(dataset_kind="live", project_id=project_id)
    checks = []

    print(f"Ingesting synthetic CSV records into {evidence_db}")
    counts = ingest_all(csv_files=csv_specs(), project_id=project_id, db_path=evidence_db)
    checks.append(("formal ingest", counts.get("total_records", 0) > 0, str(counts)))

    clock = {"now": "2026-01-16T00:00:00+00:00"}
    store = DecisionStore(decision_db, evidence_db_path=evidence_db,
                          clock=lambda: datetime.fromisoformat(clock["now"]))
    topic = store.create("检索后端方案（合成调查）", scope)
    decision_id = topic["decision_id"]

    hits_v1 = retrieve_evidence("会议结论 采用 SQLite BM25 基线 向量数据库",
                                scope=scope, db_path=evidence_db, top_k=10)
    _, result_v1 = observe(scope, hits_v1, {"MTG-001", "REQ-003"}, "demo-v1-observation")
    proposal_v1 = store.propose(decision_id, result_v1,
        "采用 SQLite Evidence + BM25 作为 v1 基线，暂不引入向量数据库。",
        event_time="2026-01-15T14:00:00+08:00")
    store.review(proposal_v1["proposal_id"], scope, operation="accept",
                 reviewer_id="synthetic-demo-reviewer", expected_revision=0,
                 note="Synthetic case: checked MTG-001 and REQ-003", acknowledge_risks=True)
    version_v1 = store.snapshot(decision_id, scope)["current_confirmed"]

    clock["now"] = "2026-03-13T00:00:00+00:00"
    hits_v2 = retrieve_evidence("取消 Q2 向量数据库迁移 决策变更",
                                scope=scope, db_path=evidence_db, top_k=10)
    selected_v2, result_v2 = observe(scope, hits_v2, {"MTG-003", "DEC-001", "CHAT-004"}, "demo-v2-observation")
    relations = []
    for hit in selected_v2:
        relations.append({"evidence_id": hit["evidence_id"], "content_hash": hit["content_hash"],
                          "relation_type": "contradicts" if hit["original_record_id"] == "CHAT-004" else "supports"})
    proposal_v2 = store.propose(decision_id, result_v2,
        "取消 Q2 向量数据库迁移，v1 保持 SQLite + BM25，待真实数据接入后重新评估。",
        refs=relations, event_time="2026-03-12T15:00:00+08:00")
    store.review(proposal_v2["proposal_id"], scope, operation="accept",
                 reviewer_id="synthetic-demo-reviewer", expected_revision=1,
                 note="Synthetic case: checked MTG-003/DEC-001; CHAT-004 kept as disagreement",
                 acknowledge_risks=True)
    snapshot = store.snapshot(decision_id, scope)
    version_v2 = snapshot["current_confirmed"]

    # Case 1: reviewed history, body search, and read-only agent display.
    question_one = "当时为什么决定采用 SQLite + BM25？后来有什么变化？"
    print(f"\n=== Case 1: {question_one}")
    print_timeline("Reviewed decision timeline (event_time order; unknown last)", snapshot["timeline"])
    os.environ["EVIDENCE_DB_PATH"] = str(evidence_db)
    os.environ["DECISION_DB_PATH"] = str(decision_db)
    output = ToolExecutor(scope=scope, db_path=evidence_db).execute(
        "search_memory", question_one,
        filters={"decision_id": decision_id, "memory_view": "history"})
    memories = output["decision_memories"]
    current = [item for item in memories if item["system_current"]]
    print("Read-only agent decision memories:")
    for item in memories:
        print(f"  {item['version_id']} system_current={item['system_current']} "
              f"event_time={item['event_time']} event_time_status={item['event_time_status']} "
              f"decision_known_at={item['decision_known_at']} execution_status={item['execution_status']}")
        print(f"     {item['statement']}")
        print("     evidence: " + ", ".join(
            f"{ref['evidence_id']}@{ref['content_hash']}[{ref['relation_type']}; "
            f"event_time={ref['event_time']}; ingested_at={ref['ingested_at']}; {ref['provenance_status']}]"
            for ref in item["evidence"]))
    checks.append(("history has both reviewed versions", len(memories) == 2, [m["version_id"] for m in memories]))
    checks.append(("one system-current version", len(current) == 1 and current[0]["version_id"] == version_v2["version_id"],
                   [m["version_id"] for m in current]))
    checks.append(("supersedes link preserved", version_v2["supersedes_version_id"] == version_v1["version_id"],
                   version_v2["supersedes_version_id"]))
    store_evidence = EvidenceStore(evidence_db)
    checks.append(("all displayed refs refetch under scope", all(
        store_evidence.get(ref["evidence_id"], scope, ref["content_hash"]) is not None
        for item in memories for ref in item["evidence"]), "identity+hash+scope"))

    # Case 2: system knowledge at a cutoff, not world state.
    cutoff = "2026-02-01T00:00:00+00:00"
    question_two = "截至 2026-02-01，系统确认了什么？"
    print(f"\n=== Case 2: {question_two}")
    as_of = store.search("系统确认了什么", scope, decision_id=decision_id, view="current", known_at=cutoff)
    for item in as_of:
        print(f"  version={item['version_id']} knowledge_cutoff={item['knowledge_cutoff']} "
              f"decision_known_at={item['decision_known_at']} event_time={item['event_time']}")
        print(f"  {item['statement']}")
    checks.append(("cutoff sees only the first version", len(as_of) == 1 and as_of[0]["version_id"] == version_v1["version_id"],
                   [item["version_id"] for item in as_of]))

    # Case 3: discussion vs formal decision vs execution; contradictions retained.
    question_three = "聊天曾说 Q2 上线向量数据库，后来会议和决策记录怎么说？现在系统确认了什么，和实际部署一致吗？"
    print(f"\n=== Case 3: {question_three}")
    hits_three = retrieve_evidence("Q2 向量数据库 迁移 部署 状态", scope=scope,
                                   db_path=evidence_db, top_k=10)
    ledger = EvidenceLedger(scope)
    ledger.register(hits_three, "search_memory")
    memories_three = store.search("Q2 向量数据库迁移 当前状态", scope,
                                  decision_id=decision_id, view="current")
    coverage = task_coverage(question_three, ledger, memories_three,
                             conflicts=ledger.conflicts, gaps=())
    print("Retrieved sources:")
    for entry in ledger.entries:
        evidence = entry.evidence
        print(f"  {evidence.original_record_id} source_type={evidence.source_type} "
              f"event_time={evidence.event_time} ingested_at={evidence.ingested_at}")
        print(f"     {evidence.content.splitlines()[-1]}")
    print("Unresolved conflicts (system does not choose a side):")
    for conflict in ledger.conflicts:
        print("  " + conflict.description + " refs=" + ", ".join(
            f"{ref.evidence_id}@{ref.content_hash}" for ref in conflict.refs))
    print("Coverage: " + json.dumps({key: coverage[key] for key in
          ("method", "semantic_completeness", "missing_targets", "completion_basis")}, ensure_ascii=False))
    for target in coverage["targets"]:
        print(f"  target={target['target']} status={target['status']} basis={target['basis']}")
    for level in coverage["authority_levels"]:
        print(f"  authority={level['level']} observed={level['observed']}")
    checks.append(("structured staging deployment contradiction detected", len(ledger.conflicts) >= 1,
                   len(ledger.conflicts)))
    authority = {item["level"]: item for item in coverage["authority_levels"]}
    checks.append(("discussion, meeting, reviewed decision are distinct",
                   all(authority[name]["observed"] for name in ("discussion", "meeting_record", "reviewed_decision")),
                   {name: authority[name]["observed"] for name in ("discussion", "meeting_record", "reviewed_decision")}))
    checks.append(("execution wording is literal only", authority["execution"]["observed"]
                   and authority["execution"]["basis"] == "literal_word_only_not_execution_proof",
                   authority["execution"]["basis"]))
    checks.append(("system confirmation is not world state",
                   all(item["world_state"] == "unknown" and item["execution_status"] == "unknown"
                       for item in memories_three), "world_state=unknown"))

    # Case 4: unknown event time remains unknown.
    question_four = "无时间聊天能否证明决策发生在某一天？"
    print(f"\n=== Case 4: {question_four}")
    undated = retrieve_evidence("Q2 迁移 向量数据库", scope=scope, db_path=evidence_db,
                                filters=Filters(undated_only=True), top_k=10)
    for hit in undated:
        print(f"  {hit['original_record_id']} event_time={hit['event_time']} "
              f"event_time_status={hit['metadata'].get('event_time_status')}")
    timeline_event_times = [item["event_time"] for item in snapshot["timeline"]]
    checks.append(("undated chat has no invented time",
                   all(hit["event_time"] is None for hit in undated)
                   and all(value is not None for value in timeline_event_times),
                   [hit["original_record_id"] for hit in undated]))

    print("\nChecks:")
    for name, passed, detail in checks:
        print(f"  {'PASS' if passed else 'FAIL'} {name}: {detail}")
    print(f"\nEvidence DB: {evidence_db}")
    print(f"Decision DB: {decision_db}")
    return 0 if all(passed for _, passed, _ in checks) else 1


def main():
    parser = argparse.ArgumentParser(description="Run synthetic decision-investigation cases")
    parser.add_argument("--project-id", default="cma-team-decision-demo")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="Fresh directory for demo databases; defaults to a temp directory")
    args = parser.parse_args()
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="cma-decision-demo-"))
    return run(args.project_id, workdir)


if __name__ == "__main__":
    raise SystemExit(main())
