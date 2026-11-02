"""Load the synthetic team-collaboration story through the real live ingest path.

The CSV files contain no private chats or internal group documents. They are
deliberately synthetic and exist only to exercise category mapping, provenance,
time-unknown handling, duplicate sync, and cross-source retrieval.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ingest import ingest_all


HERE = Path(__file__).resolve().parent


def csv_specs() -> list[dict]:
    common = {"content_column": "content", "id_column": None}
    return [
        {**common, "path": HERE / "requirements.csv", "category": "document",
         "id_column": "requirement_id", "time_column": "event_time", "sender_column": "author",
         "channel_column": "channel", "subject_column": "subject", "source_ref_column": "source_ref",
         "name": "requirements"},
        {**common, "path": HERE / "chats.csv", "category": "chat",
         "id_column": "message_id", "time_column": "event_time", "sender_column": "sender",
         "channel_column": "channel", "subject_column": "subject", "source_ref_column": "source_ref",
         "name": "chats"},
        {**common, "path": HERE / "meetings.csv", "category": "meeting",
         "id_column": "meeting_id", "time_column": "event_time", "sender_column": "chair",
         "channel_column": "channel", "subject_column": "topic", "source_ref_column": "source_ref",
         "name": "meetings"},
        {**common, "path": HERE / "reviews.csv", "category": "document",
         "id_column": "review_id", "time_column": "event_time", "sender_column": "reviewer",
         "channel_column": "channel", "subject_column": "subject", "source_ref_column": "source_ref",
         "name": "reviews"},
        {**common, "path": HERE / "issues.csv", "category": "issue",
         "id_column": "issue_id", "time_column": "event_time", "sender_column": "reporter",
         "channel_column": "channel", "subject_column": "subject", "source_ref_column": "source_ref",
         "name": "issues"},
        {**common, "path": HERE / "documents.csv", "category": "document",
         "id_column": "document_id", "time_column": "event_time", "sender_column": "author",
         "channel_column": "channel", "subject_column": "subject", "source_ref_column": "source_ref",
         "name": "documents"},
        {**common, "path": HERE / "decisions.csv", "category": "document",
         "id_column": "decision_id", "time_column": "event_time", "sender_column": "owner",
         "channel_column": "channel", "subject_column": "subject", "source_ref_column": "source_ref",
         "name": "decisions"},
        {**common, "path": HERE / "deployments.csv", "category": "document",
         "id_column": "deployment_id", "time_column": "event_time", "sender_column": "sender",
         "channel_column": "channel", "subject_column": "subject", "source_ref_column": "source_ref",
         "assertions_column": "assertions", "name": "deployments"},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the synthetic cross-source team demo")
    parser.add_argument("--project-id", default="cma-team-demo")
    parser.add_argument("--db", type=Path, default=None,
                        help="Evidence SQLite path; defaults to data/live/evidence.sqlite3")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    counts = ingest_all(csv_files=csv_specs(), project_id=args.project_id,
                        db_path=args.db, dry_run=args.dry_run)
    print(f"\nDemo project: {args.project_id}")
    print(f"Counts: {counts}")
    if not args.dry_run:
        print("\nNext:")
        print(f"  python main.py --react --dataset-kind live --project-id {args.project_id}"
              + (f" --db {args.db}" if args.db else ""))
        print("  python main.py --dataset-kind live --project-id " + args.project_id
              + "  # LangGraph workflow comparison")
        print("  set EVIDENCE_DB_PATH=... before starting api.py to use a non-default DB")


if __name__ == "__main__":
    main()
