# Synthetic team-collaboration demo

All records here are synthetic. They do **not** contain real private chats,
meeting minutes, internal group documents, customer data, credentials, or live
platform exports.

The story is intentionally continuous:

1. Requirement discussion (`requirements.csv`)
2. Team meeting decision (`meetings.csv`, `MTG-001`/`MTG-002`)
3. Design review issue (`reviews.csv`, `ISS-001`/`ISS-002`)
4. Revision (`documents.csv`)
5. Decision change/cancellation (`meetings.csv` `MTG-003`, `decisions.csv`)

It also includes two deliberate edge cases:

- `CHAT-003` has no event time. Its blank time stays `None` / `unknown`; the
  seed script does not invent a timestamp.
- `CHAT-004` and `DOC-002` conflict about the Q2 vector-database plan on
  `2026-02-10`. A later meeting (`MTG-003`) cancels that plan.
- `DEPLOY-001` and `DEPLOY-002` are same-day staging release records with
  machine-readable `assertions` that disagree on `vector_backend`; the system
  retains the contradiction and does not choose a side.

## CSV contract

Every CSV has a header row. Required fields are a stable record ID and a content
column. This demo uses:

| File | Category | ID column | Content | Event time | Sender-like | Channel | Subject/topic | Original locator | Assertions |
|---|---|---|---|---|---|---|---|---|---|
| `requirements.csv` | document | `requirement_id` | `content` | `event_time` | `author` | `channel` | `subject` | `source_ref` | - |
| `chats.csv` | chat | `message_id` | `content` | `event_time` | `sender` | `channel` | `subject` | `source_ref` | - |
| `meetings.csv` | meeting | `meeting_id` | `content` | `event_time` | `chair` | `channel` | `topic` | `source_ref` | - |
| `reviews.csv` | document | `review_id` | `content` | `event_time` | `reviewer` | `channel` | `subject` | `source_ref` | - |
| `issues.csv` | issue | `issue_id` | `content` | `event_time` | `reporter` | `channel` | `subject` | `source_ref` | - |
| `documents.csv` | document | `document_id` | `content` | `event_time` | `author` | `channel` | `subject` | `source_ref` | - |
| `decisions.csv` | document | `decision_id` | `content` | `event_time` | `owner` | `channel` | `subject` | `source_ref` | - |
| `deployments.csv` | document | `deployment_id` | `content` | `event_time` | `sender` | `channel` | `subject` | `source_ref` | `assertions` |

`assertions` is optional. When present it must be a JSON object whose values are
strings, numbers, or booleans; it is stored in `metadata.assertions` and used
only for deterministic same-day contradiction checks. It is never treated as
proof of execution.

`event_time` must be ISO-8601 with an explicit timezone when present, for
example `2026-01-15T14:00:00+08:00`. A blank cell means unknown. A non-blank
value without a timezone is rejected rather than guessed. Any conclusion that
cites an undated structured record is marked time-unknown and treated as
`insufficient`; the importer and answer validator never invent a date.

The importer preserves the original row in `metadata.columns`, stores the
source locator (`csv_row`, `csv_path`, optional `source_ref`), sets
`metadata.event_time_status` to `recorded` or `unknown`, and uses a stable
Evidence ID derived from project, source URI, source type, and record ID.
Re-importing unchanged rows appends no new observation; changing content under
the same ID creates a new observation/version.

## Seed and query

```bash
python examples/team_collaboration/seed_demo.py --dry-run
python examples/team_collaboration/seed_demo.py
python main.py --react --dataset-kind live --project-id cma-team-demo
```

Use `--db path/to/evidence.sqlite3` on both the seed script and `main.py` to
keep this demo isolated from other live projects. The API uses
`EVIDENCE_DB_PATH` when a non-default database is required.

## Reviewed decision investigation demo

`decision_demo.py` runs four synthetic cases against the real live ingest and
retrieval path, then simulates an explicit local human review
(`DecisionStore.propose` -> `review`) to create two reviewed versions. It does
not hardcode answers and does not use gold labels.

```bash
python examples/team_collaboration/decision_demo.py
# or keep inspectable databases in a fresh directory:
python examples/team_collaboration/decision_demo.py --workdir D:\tmp\cma-decision-demo
```

The demo prints and checks:

1. Reviewed decision history at the current time, including supersedes links,
   evidence IDs/hashes, and the difference between `event_time`,
   `decision_known_at`, and `ingested_at`.
2. A `known_at=2026-02-01` system-knowledge snapshot that sees only the first
   reviewed version.
3. Discussion chat vs meeting record vs reviewed decision vs deployment wording,
   an unresolved structured staging contradiction, and `world_state=unknown`.
4. An undated chat that stays unknown and is never used to date a decision.

The demo only proves deterministic linkage and disclosure. It does not prove
semantic correctness, actual production deployment, or current real-world
state.
