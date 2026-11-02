# Phase 2 – Persistent Agent Runtime

This document describes the Session/Run runtime added in Phase 2. The runtime
is a **single-user, local deployment** component. It is not a production
multi-tenant authorization system.

## Architecture

```
FastAPI (/sessions, /runs)
        │
        ├── SessionService ── ConversationContextBuilder ── EvidenceStore
        │
        └── RunService (state machine, timers, worker pool)
                │
                ├── RunObserver ── EventStore / ToolCall / EvidenceRef
                └── ReActAgent (existing investigation semantics)
```

The Agent remains synchronous and is executed in a worker thread. HTTP
handlers never run the Agent on the event loop. The existing
`EvidenceLedger`, claim validation, Decision Memory, Context Engineering and
live retrieval paths are reused unchanged.

## Run lifecycle

```
queued ──> running ──> succeeded
   │          │      ├─> insufficient
   │          │      ├─> conflict
   │          │      ├─> failed
   │          │      ├─> timed_out
   │          │      ├─> cancelled
   │          │      └─> interrupted
   └──────────┴─────────> cancelled / timed_out / interrupted
```

Terminal statuses are immutable. Every transition is written to `runs` and
appended to `run_events` in the same SQLite transaction.

`status` is the system lifecycle state. `termination_reason` is the business
reason reported by the investigation:

| Investigation reason | Run status |
| --- | --- |
| `sufficient` | `succeeded` |
| `insufficient` | `insufficient` (not a system failure) |
| `conflict` | `conflict` |
| `tool_failure` | `failed` |
| `budget` | `failed` |
| user cancel | `cancelled` |
| deadline | `timed_out` |
| runtime exception | `failed` (`system_error`) |
| process restart | `interrupted` |

## Persistence

All tables are additive members of the existing Evidence SQLite database and
use the existing `schema_migrations` table.

| Table | Purpose |
| --- | --- |
| `sessions` | project/dataset scope, status, timestamps |
| `session_messages` | ordered user/assistant turns linked to run ids |
| `runs` | run state, budget, model identifier, retrieval metadata, usage |
| `run_events` | append-only lifecycle log, `UNIQUE(run_id, event_seq)` |
| `run_tool_calls` | one row per tool attempt, with trace step index |
| `run_evidence_refs` | evidence id + content hash observed by a run |
| `run_results` | immutable terminal result payload |

Migration versions 3 and 4 are recorded. Version 4 adds
`runs.fallback_reason`; existing version-3 databases are upgraded with
`ALTER TABLE` during the next write.

`run_events` has SQLite triggers that reject `UPDATE` and `DELETE`. Events
are strictly ordered per run. Instrumentation that arrives after the run has
reached a terminal state is discarded, so a terminal event history cannot
grow retroactively.

### Sanitization

Event payloads, run/tool error messages and public API rows pass through a
sanitizer that redacts credential keys, bearer/sk tokens, long hex strings
(except runtime identity fields such as `content_hash`), and local filesystem
paths, and truncates oversized values. Model request messages, provider
response bodies and full Evidence content are never persisted in runtime
events.

### SQLite path, permissions and retention

- Default path: `EVIDENCE_DB_PATH`, otherwise the same default database used
  by `EvidenceStore` (`data/live/evidence.sqlite3` in this repository).
- The directory is created on first write; SQLite creates `-wal`/`-shm`
  sidecar files when applicable.
- On POSIX deployments restrict the file to the service account
  (`chmod 600`, directory `700`). The local API has no authentication.
- No automatic retention or vacuum job is provided. Events are immutable and
  runs reference results and evidence. To remove old data, take a backup and
  perform an explicit offline maintenance operation (temporarily dropping the
  append-only triggers or rebuilding the database). Do not delete individual
  event rows in place.

## Idempotency and concurrency

- `Idempotency-Key` (header or `idempotency_key` body field) is unique per
  session. Repeating the same request returns the original run with
  `created=false` / `idempotent_replay=true`.
- Reusing a key with a different normalized request (question, scope, budget,
  retrieval request) returns `409 idempotency_key_reused_with_different_request`.
- An idempotent replay still resolves after the session is closed.
- Independent sessions execute concurrently in the worker pool, bounded by
  `AGENT_RUNTIME_WORKERS` and `AGENT_MAX_CONCURRENT_RUNS`.
- A session has at most one active run. A second submission while one is
  queued/running returns `409 session_run_in_progress` (serial policy, not
  silent queueing).
- SQLite writes use `BEGIN IMMEDIATE`, `busy_timeout=15000` and bounded
  lock-retry backoff. Duplicate starts are rejected by the queued → running
  transition.

## Cancellation and timeout semantics

Cancellation is **cooperative**. The Agent checks a cancellation event before
each model decision, model request, tool call and recovery call. Closing an
SSE stream never cancels a run.

- Cancelling a queued run moves it directly to `cancelled`.
- Cancelling a running run records `cancel_requested`, emits
  `run_cancel_requested`, and closes at the next cooperative boundary.
- The timeout timer marks `timed_out` independently of the worker; a late
  Agent result is discarded because terminal rows are immutable.
- There is no unsafe thread kill. A provider call that is already in flight
  can continue until it returns; its result cannot change the terminal state.
- Timeout and cancel remain distinct even when the timer and cancel race: the
  cancel flag is written before the cancel event, while a timeout only sets
  the event.

## Restart recovery

On the first runtime use after a restart, leftover `queued`/`running` rows are
marked `interrupted` with `error_code=process_restarted` and a
`run_interrupted` event.

There is **no checkpoint/resume** of a partially executed ReAct loop. The
Agent does not expose an idempotent step checkpoint, so resuming would risk
duplicate tool side effects and fabricated progress. Terminal runs are never
rerun; prior results, tool calls, evidence refs and session history remain
queryable.

## Session follow-up context

A follow-up run receives a bounded JSON context built by
`ConversationContextBuilder`:

- recent terminal runs (default 3), byte-bounded (default 6000 bytes);
- previous question, status, short answer hint, gaps and conflicts;
- up to 10 verified EvidenceRefs, revalidated against `EvidenceStore` by
  evidence id **and** content hash;
- Decision Memory clues.

The context is injected into the Agent user turn with an explicit note that
historical answers are not fact sources. It does not populate the
EvidenceLedger; any citation must be retrieved and validated again in the
current run. Stale or unverifiable refs are dropped rather than trusted.

## API

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/sessions` | create session (201) |
| GET | `/sessions/{id}` | session, recent messages, recent runs |
| POST | `/sessions/{id}/close` | close session |
| GET | `/sessions/{id}/runs` | paginated run list |
| POST | `/runs` | submit; 202; accepts `Idempotency-Key` |
| GET | `/runs/{id}?project_id=` | run state |
| GET | `/runs/{id}/events?project_id=&after_seq=&limit=` | ordered events |
| GET | `/runs/{id}/result?project_id=` | terminal result; 409 before terminal |
| GET | `/runs/{id}/tool-calls?project_id=` | tool attempts |
| GET | `/runs/{id}/evidence-refs?project_id=` | observed evidence ids/hashes |
| POST | `/runs/{id}/cancel?project_id=` | cooperative cancel |
| GET | `/runs/{id}/stream?project_id=&after_seq=` | persisted SSE replay |

`project_id` is mandatory on run-specific endpoints. A mismatch returns 404,
so identifiers from one project cannot be used to read another project's run.
This is a scope guard for a single-user API, not authentication.

`GET /health`, `POST /chat` and `POST /chat/stream` are preserved.

## SSE

- Frames use `id: <event_seq>`, `event: <event_type>`, `data: <json>`.
- `Last-Event-ID` and `after_seq` resume from a persisted cursor.
- Comment frames (`: heartbeat`) are sent while a run is active.
- The stream ends with `event: stream_end` containing the terminal status,
  termination reason and last event sequence.
- Disconnects release the iterator and never cancel the background run.
- Events are read from SQLite; the stream is a replay/catch-up view, not an
  in-memory-only pub/sub.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVIDENCE_DB_PATH` | shared default | SQLite path |
| `AGENT_RUNTIME_WORKERS` | 4 | worker threads |
| `AGENT_RUN_TIMEOUT_SECONDS` | 120 | default run deadline |
| `AGENT_MAX_CONCURRENT_RUNS` | 8 | execution slots |
| `AGENT_SSE_HEARTBEAT_SECONDS` | 15 | heartbeat interval |
| `LIVE_RETRIEVAL_MODE` | bm25 | existing retrieval mode |

Importing `api` does not read `.env`. The server entry point calls
`load_env_file()` explicitly in `__main__`; the CLI calls `load_dotenv()` in
`main()`.

## Observability

The runtime uses Python structured-ish logs with event names and JSON fields:

- `run_submitted`, `run_started`, `run_finished`, `run_cancelled`,
  `run_timed_out`, `run_failed`, `runs_marked_interrupted`;
- `agent_event` for `tool_failed`, retry scheduling, conflicts and model
  errors.

Records include `run_id` and, when known, `session_id`. Failure logs classify
`model_timeout`, `model_error`, `tool_error`, `retrieval_error`, `conflict`,
`database_error`, `timeout` and `runtime_error`. Credentials, authorization
headers and full model requests are never logged.

## Known limits

- No hard cancellation of an in-flight provider call.
- No checkpoint/resume; restart recovery marks interruption only.
- The runtime is single-process. Do not run multiple application processes
  (for example multiple Uvicorn workers) against the same runtime database:
  each process's startup recovery would treat the other process's active runs
  as interrupted. Multi-process ownership/lease recovery is not implemented.
- One active run per session; no fair queueing across sessions beyond worker
  slots.
- `run_events` is immutable; there is no automated retention job.
- Session endpoints assume a trusted single user; the project id guard is not
  an authentication mechanism.
- Retrieval mode is observed from diagnostics, not selectable through the
  run API. The Phase 1 relevance policy and thresholds are unchanged.
