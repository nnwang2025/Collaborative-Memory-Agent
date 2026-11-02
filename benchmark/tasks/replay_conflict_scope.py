"""Deterministic offline replay for the cross-atlas conflict-scope regression.

The historical trace retains evidence references and the two conflicts, but not
the model's submitted claims (the final claim list is empty because the old
runtime wiped it).  The claims below are therefore an explicit fixture.  This
command does not call a model, read credentials or write a report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from benchmark.dataset import load as load_dataset
from benchmark.tasks.scoring import _answer_body
from src.agents.answer_agent import AnswerAgent
from src.agents.react_agent import finish_termination_reason
from src.evidence import EvidenceScope
from src.investigation import Claim, Conflict, EvidenceLedger, EvidenceRef, FinishAction

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACE = ROOT / "benchmark/reports/third-batch-online-cross-atlas-01/traces/cross-atlas.json"
CHAT_ID = "ev_9ccc9fcf6f0f451cfc3a4ecc8d405ebe7d8f54e05f1bd49b7d03740f5124b7aa"
ISSUE_ID = "ev_040364740331b53779419de0bb315a9c06aa4ef378aee78b41b9a90adbd5fe0a"


def _signature(conflict):
    return tuple(sorted((ref.evidence_id, ref.content_hash) for ref in conflict.refs))


def replay(trace_path=DEFAULT_TRACE):
    trace = json.loads(Path(trace_path).read_text(encoding="utf-8"))
    historical = trace["result"]
    historical_conflicts = historical.get("conflicts") or []
    data = load_dataset()
    records = {record.evidence_id: record for record in data["records"]}
    referenced = [ref["evidence_id"] for conflict in historical_conflicts for ref in conflict["refs"]]
    selected_ids = list(dict.fromkeys([CHAT_ID, ISSUE_ID, *referenced]))
    selected = [records[evidence_id] for evidence_id in selected_ids if evidence_id in records]
    scope = EvidenceScope(project_id=selected[0].project_id, dataset_kind=selected[0].dataset_kind)
    ledger = EvidenceLedger(scope)
    ledger.register([record.model_dump(mode="json") for record in selected], "search_memory")

    def ref(record):
        return EvidenceRef(evidence_id=record.evidence_id, content_hash=record.content_hash)

    auto_signatures = {_signature(conflict) for conflict in ledger.conflicts}
    fixture_conflicts = []
    for item in historical_conflicts:
        refs = [EvidenceRef.model_validate(ref_value) for ref_value in item["refs"]]
        if tuple(sorted((value.evidence_id, value.content_hash) for value in refs)) in auto_signatures:
            continue  # the ledger rebuilds this structured conflict from assertions
        fixture_conflicts.append(Conflict.model_validate({
            "description": item["description"], "refs": refs}))
    chat, issue = records[CHAT_ID], records[ISSUE_ID]
    finish = FinishAction(
        claims=[Claim(text=chat.content, refs=[ref(chat)]),
                Claim(text=issue.content, refs=[ref(issue)])],
        conflicts=fixture_conflicts,
    )

    def getter(evidence_id, content_hash):
        record = records.get(evidence_id)
        return record if record is not None and record.content_hash == content_hash else None

    claims, gaps, conflicts, counts = AnswerAgent().compose_investigation(
        ledger, finish, getter)
    reason = finish_termination_reason(conflicts, gaps, claims)
    answer = AnswerAgent().render_investigation(ledger, claims, gaps, conflicts, reason)
    body, body_basis = _answer_body(answer)
    return {
        "replay_kind": "deterministic_fixture_not_real_model_replay",
        "original_model_actions_available": False,
        "explanation": ("The historical trace stores the final claims as empty and does not store the "
                        "model's submitted claims.  The two claims below are a fixed regression fixture, "
                        "not a replay of real model output."),
        "trace": str(Path(trace_path).relative_to(ROOT)).replace("\\", "/"),
        "historical": {
            "final_claims": historical.get("claims") or [],
            "conflict_count": len(historical_conflicts),
            "conflict_refs": [item["refs"] for item in historical_conflicts],
            "termination_reason": historical.get("termination_reason"),
        },
        "fixture_claims": [CHAT_ID, ISSUE_ID],
        "result": {
            "claim_counts": counts,
            "claims": [{"evidence_id": claim.refs[0].evidence_id,
                        "verification_status": claim.verification_status} for claim in claims],
            "conflicts": [{"origin": conflict.origin, "fields": conflict.fields,
                           "relation": conflict.relation,
                           "affected_claim_indexes": conflict.affected_claim_indexes}
                          for conflict in conflicts],
            "gaps": gaps,
            "termination_reason": reason,
            "answer_body_basis": body_basis,
            "answer_body_chars": len(body),
        },
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    args = parser.parse_args()
    print(json.dumps(replay(args.trace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
