"""Trusted local review interface. Audit labels here are NOT user authentication."""
import argparse
import json

from src.evidence import EvidenceScope
from src.investigation import InvestigationResult
from src.memory.decisions import DecisionStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--dataset-kind", choices=["fixture", "live"], default="live")
    parser.add_argument("--db")
    parser.add_argument("--evidence-db")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--title", required=True)
    propose = commands.add_parser("propose")
    propose.add_argument("--decision-id", required=True)
    propose.add_argument("--result-file", required=True)
    propose.add_argument("--statement", required=True)
    propose.add_argument("--event-time")
    review = commands.add_parser("review")
    review.add_argument("--proposal-id", required=True)
    review.add_argument("--operation", choices=["accept", "reject"], required=True)
    review.add_argument("--reviewer-label", required=True)
    review.add_argument("--expected-revision", type=int, required=True)
    review.add_argument("--note", required=True)
    review.add_argument("--acknowledge-risks", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("--decision-id", required=True)
    show.add_argument("--known-at")
    proposal = commands.add_parser("proposal")
    proposal.add_argument("--proposal-id", required=True)
    args = parser.parse_args()
    scope = EvidenceScope(project_id=args.project_id, dataset_kind=args.dataset_kind)
    store = DecisionStore(args.db, evidence_db_path=args.evidence_db)
    if args.command == "create":
        result = store.create(args.title, scope)
    elif args.command == "propose":
        with open(args.result_file, encoding="utf-8") as stream:
            investigation = InvestigationResult.model_validate_json(stream.read())
        if investigation.scope != scope:
            parser.error("Investigation scope differs from the trusted local command scope")
        result = store.propose(args.decision_id, investigation, args.statement, event_time=args.event_time)
    elif args.command == "review":
        result = store.review(args.proposal_id, scope, operation=args.operation, reviewer_id=args.reviewer_label,
            expected_revision=args.expected_revision, note=args.note, acknowledge_risks=args.acknowledge_risks)
    elif args.command == "proposal":
        result = store.proposal(args.proposal_id, scope)
    else:
        result = store.snapshot(args.decision_id, scope, known_at=args.known_at)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
