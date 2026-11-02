"""Explicit-opt-in online runner for Phase 4 (ReAct or fixed Workflow).

Default mode is a plan: no credential inspection and no network request.  A
paid run requires *all* of: ``--online``, ``--i-understand-this-makes-paid-model-calls``,
an exact HTTPS ``--approved-origin``, explicit provider/model/base URL, a new
or resumable output directory, and positive total call/token caps.

Both backends reuse the production chains.  ReAct has its own budget protocol;
the fixed Workflow has none, so the runner wraps every provider request with an
external call/token guard (402 stops immediately, 429/503 get limited whole-task
retries).  Workflow lacks a Claim Ledger and ``known_at`` support; those
dimensions are recorded as incomparable, and no difference is attributed to
ReAct reasoning as a cause.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from src.llm_config import is_placeholder_secret

from . import dataset, indexing
from .dataset import Snapshot, clone_databases, materialize_template, summary
from .indexing import IndexPreparationError
from .llm_config import PROVIDERS, provider_environment
from .runtime import COMPARABILITY_NOTES, ONLINE_CAPABILITY_NOTES, run_task
from .scoring import SCORING_VERSION, aggregate, diagnose, pending_grading, score_run
from .run_offline import (RetrievalChannelError, _git_state, _group_hash, _hash,
                          assert_requested_channel, ROOT, SOURCE_FILES, VERSION_GROUPS)

REPORT_VERSION = "cma-v4-online-report-v1"
DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True)
class OnlineConfig:
    provider: str
    model: str
    base_url: str
    approved_origin: str | None
    api_key_env: str
    output: Path | None
    resume: Path | None
    task_ids: tuple[str, ...]
    categories: tuple[str, ...]
    max_tasks: int | None
    max_model_calls: int
    max_budget_tokens: int
    max_model_rounds: int
    max_tool_calls: int
    max_output_tokens: int
    max_retries: int
    timeout_seconds: float
    retrieval_mode: str
    backend: str = "react"


def _origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("base URL must be an exact HTTPS origin without credentials/query/fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


def _safe_origin(base_url: str) -> str | None:
    try:
        return _origin(base_url)
    except ValueError:
        return None


def _select(snapshot: Snapshot, config: OnlineConfig) -> list[dict[str, Any]]:
    tasks = list(snapshot.public_tasks)
    if config.categories:
        tasks = [task for task in tasks if task["category"] in config.categories]
    if config.task_ids:
        selected = [task for task in tasks if task["task_id"] in set(config.task_ids)]
        missing = set(config.task_ids) - {task["task_id"] for task in selected}
        if missing:
            raise ValueError("Unknown task_id(s): " + ", ".join(sorted(missing)))
        tasks = selected
    if config.max_tasks is not None:
        if config.max_tasks < 1:
            raise ValueError("--max-tasks must be positive")
        tasks = tasks[:config.max_tasks]
    if not tasks:
        raise ValueError("No tasks selected")
    return tasks


def _config_payload(config: OnlineConfig, snapshot: Snapshot) -> dict[str, Any]:
    return {
        "backend": config.backend,
        "provider": config.provider,
        "model": config.model,
        "base_url_origin": _safe_origin(config.base_url),
        "api_key_env": config.api_key_env,
        "snapshot_id": snapshot.snapshot_id,
        "task_set_version": snapshot.task_set_version,
        "scoring_version": SCORING_VERSION,
        "retrieval_mode": config.retrieval_mode,
        "max_tasks": config.max_tasks,
        "task_ids": list(config.task_ids),
        "categories": list(config.categories),
        "max_model_calls": config.max_model_calls,
        "max_budget_tokens": config.max_budget_tokens,
        "max_model_rounds": config.max_model_rounds,
        "max_tool_calls": config.max_tool_calls,
        "max_output_tokens": config.max_output_tokens,
        "max_retries": config.max_retries,
        "timeout_seconds": config.timeout_seconds,
        "temperature": 0.1,
    }


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _turns(tasks: list[dict[str, Any]]) -> int:
    return sum(len(task.get("turns") or [task["question"]]) for task in tasks)


def _call_upper_bound(tasks: list[dict[str, Any]], backend: str,
                      max_model_rounds: int) -> tuple[int, int]:
    turns = _turns(tasks)
    if backend == "workflow":
        return turns * 3, 0
    return turns * (max_model_rounds + 1), turns


def plan(snapshot: Snapshot, tasks: list[dict[str, Any]], config: OnlineConfig,
         *, status: str = "pending_authorization") -> dict[str, Any]:
    payload = _config_payload(config, snapshot)
    upper_calls, truncation_calls = _call_upper_bound(
        tasks, config.backend, config.max_model_rounds)
    return {
        "report_version": REPORT_VERSION,
        "status": status,
        "online_executed": False,
        "message": ("No model call was made. A paid run requires --online, "
                    "--i-understand-this-makes-paid-model-calls, an exact --approved-origin, "
                    "a new or resumed --output, and positive call/token caps."),
        "snapshot": summary(snapshot),
        "task_count": len(tasks),
        "task_ids": [task["task_id"] for task in tasks],
        "categories": sorted({task["category"] for task in tasks}),
        "selected_backend": config.backend,
        "supported_online_backends": ["react", "workflow"],
        "workflow_online_status": "supported_with_disclosed_capability_differences",
        "workflow_reason": ("Workflow reuses the fixed LangGraph pipeline. It has no native "
                            "budget protocol, no Claim Ledger and no known_at support; the "
                            "runner enforces external call/token caps. Results are not "
                            "comparable on those dimensions."),
        "config": payload,
        "config_fingerprint": _fingerprint(payload),
        "max_model_calls": config.max_model_calls,
        "max_normal_model_calls_upper_bound": upper_calls,
        "max_truncation_recovery_calls_upper_bound": truncation_calls,
        "max_tool_calls": _turns(tasks) * config.max_tool_calls,
        "max_budget_tokens": config.max_budget_tokens,
        "max_requested_output_tokens_per_call": config.max_output_tokens,
        "cost_status": "unavailable_no_pricing",
        "backend_capabilities": ONLINE_CAPABILITY_NOTES[config.backend],
        "backend_comparability": COMPARABILITY_NOTES,
        "cap_applicability": {
            "max_model_calls": "shared hard cap across all selected tasks and retries",
            "max_budget_tokens": "shared hard cap; provider usage when reported, otherwise estimated",
            "max_model_rounds": ("ReAct only; Workflow has no native round concept"
                                 if config.backend == "workflow" else "ReAct budget"),
            "max_tool_calls": "per task",
        },
        "required_flags": [
            "--online",
            "--i-understand-this-makes-paid-model-calls",
            "--approved-origin <exact HTTPS origin>",
            "--provider <deepseek|siliconflow|openai>",
            "--model <name>",
            "--base-url <https URL>",
            "--backend <react|workflow>",
            "--output <new directory> or --resume <directory>",
            "--max-model-calls <positive integer>",
            "--max-budget-tokens <positive integer>",
        ],
        "unverified_steps": [
            "semantic task success and citation support (human review)",
            "provider-side cost (no pricing table assumed)",
            "Workflow dimensions: budget protocol, Claim Ledger, known_at (incomparable)",
            "ReAct vs Workflow difference is architecture-level, not a causal reasoning gain",
        ],
    }


def _model_error_statuses(raw: dict[str, Any]) -> list[int | None]:
    statuses: list[int | None] = list(raw.get("model_error_http_statuses") or [])
    for turn in raw.get("trace", []):
        for step in turn.get("steps", []):
            for error in step.get("errors", []):
                if error.get("error_code") == "model_error":
                    statuses.append((error.get("diagnostic") or {}).get("http_status"))
    return statuses


def _classify(statuses: list[int | None], raw: dict[str, Any] | None = None) -> str:
    if 402 in statuses:
        return "insufficient_balance"
    if 429 in statuses:
        return "rate_limited"
    if 503 in statuses:
        return "provider_unavailable"
    reason = (raw or {}).get("termination_reason")
    if reason == "model_error":
        return "model_error"
    if reason in {"max_model_calls", "max_budget_tokens"}:
        return "budget_exhausted"
    if statuses:
        return "model_error"
    return "completed"


def _per_task_budget(config: OnlineConfig, remaining_calls: int,
                     remaining_tokens: int) -> dict[str, Any]:
    rounds = max(1, min(config.max_model_rounds, remaining_calls))
    return {
        "max_model_rounds": rounds,
        "max_tool_calls": config.max_tool_calls,
        "max_tokens": max(1, remaining_tokens),
        "max_output_tokens": config.max_output_tokens,
        "deadline_seconds": config.timeout_seconds,
        "model_timeout": min(30.0, config.timeout_seconds),
        "max_retries": 0,
    }


def _row(snapshot: Snapshot, task: dict[str, Any], raw: dict[str, Any],
         databases: tuple[Path, Path], attempts: int) -> dict[str, Any]:
    label = snapshot.label(task["task_id"])
    scored = score_run(raw, task, label, evidence_db=databases[0])
    statuses = _model_error_statuses(raw)
    scored["attempts"] = attempts
    scored["backend"] = raw.get("backend")
    scored["model_error_http_statuses"] = statuses
    scored["run_status"] = _classify(statuses, raw)
    if raw.get("runtime_error") and scored["run_status"] == "completed":
        scored["run_status"] = "runtime_error"
    if raw.get("mode") == "offline_fallback":
        scored["run_status"] = "offline_fallback"
    scored["run_id"] = raw.get("run_id")
    scored["attempt_model_calls"] = raw.get("model_calls", 0)
    scored["attempt_provider_tokens"] = (raw.get("provider_input_tokens", 0) +
                                         raw.get("provider_output_tokens", 0))
    scored["external_caps"] = raw.get("external_caps")
    scored["online_capabilities"] = raw.get("online_capabilities")
    return scored


def _write_summary(output: Path, snapshot: Snapshot, config: OnlineConfig,
                   rows: list[dict[str, Any]], *, status: str, stopped_reason: str | None,
                   attempts_total: int, started_at: str, execution_seconds: float,
                   total_model_calls: int, total_tokens: int,
                   index_report: dict[str, Any] | None = None) -> dict[str, Any]:
    overall = aggregate(rows)
    payload = {
        "report_version": REPORT_VERSION,
        "run_id": str(uuid4()),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "execution_seconds": execution_seconds,
        "status": status,
        "online_executed": total_model_calls > 0,
        "stopped_reason": stopped_reason,
        "snapshot": summary(snapshot),
        "config": {
            **_config_payload(config, snapshot),
            "api_key_env": config.api_key_env,
            "api_key_recorded": False,
            "execution_mode": "online_model",
            "temperature": 0.1,
            "provider_origin": _origin(config.base_url),
            "retrieval_mode": config.retrieval_mode,
            "index_preparation": indexing.summary(index_report),
            "index_versions": [entry["index_version"] for entry
                               in (index_report or {}).get("indexes", [])],
            "backend_capabilities": ONLINE_CAPABILITY_NOTES[config.backend],
            "backend_comparability": COMPARABILITY_NOTES,
            "git": _git_state(),
            "source_sha256": {relative: _hash(ROOT / relative) for relative in SOURCE_FILES},
            **{name: _group_hash(paths) for name, paths in VERSION_GROUPS.items()},
            "actual_cost_usd": None,
            "actual_cost_status": "not_measured_no_pricing",
            "network_policy": "HTTPS only, exact approved origin, redirects refused",
            "model_calls_total": total_model_calls,
            "provider_tokens_total": total_tokens,
        },
        "aggregates": {"overall": overall},
        "execution": {
            "tasks_selected": len({row["task_id"] for row in rows}),
            "runs_recorded": len(rows),
            "attempts": attempts_total,
            "model_calls": total_model_calls,
            "provider_tokens": total_tokens,
            "completed_runs": sum(row["run_status"] == "completed" for row in rows),
            "unfinished_runs": sum(row["run_status"] != "completed" for row in rows),
        },
        "failure_cases": diagnose(rows),
        "pending_grading": pending_grading(rows),
        "known_limitations": [
            "TaskSuccessRate is null until independent human review.",
            "Citation integrity is automatic; semantic claim support is manual.",
            "Known-at and conflict checks are deterministic signals, not semantic grades.",
            "Cost is unavailable because no pricing table is assumed.",
            "Workflow has no native budget protocol, Claim Ledger or known_at support; "
            "external caps stop it but do not make those dimensions comparable.",
            "ReAct vs Workflow differences are end-to-end architecture differences, not "
            "causal evidence that ReAct reasoning caused an improvement.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    return payload


def run(config: OnlineConfig, *, online: bool = False, confirmed: bool = False,
        plan_only: bool = False) -> dict[str, Any]:
    snapshot = dataset.load()
    if config.backend not in {"react", "workflow"}:
        raise ValueError("backend must be react or workflow")
    tasks = _select(snapshot, config)
    if not (config.base_url.startswith("https://") and config.approved_origin):
        return plan(snapshot, tasks, config, status="pending_authorization")
    if _origin(config.base_url) != config.approved_origin:
        raise ValueError("--approved-origin must exactly match the base URL origin")
    if config.provider not in PROVIDERS:
        raise ValueError("Unsupported provider")
    if not online or plan_only:
        return plan(snapshot, tasks, config)
    if not confirmed:
        return plan(snapshot, tasks, config, status="refused_missing_explicit_confirmation")
    if config.max_model_calls <= 0 or config.max_budget_tokens <= 0:
        raise ValueError("Positive --max-model-calls and --max-budget-tokens are required")
    if config.resume is None:
        if config.output is None:
            raise ValueError("Online mode requires a new --output directory")
        if config.output.exists():
            raise FileExistsError("Online report directory exists; choose a new path or --resume")
        if not config.output.parent.is_dir():
            raise FileNotFoundError("Create the output parent directory before authorizing provider requests")
    env = provider_environment(config.provider, config.model, config.base_url,
                               config.api_key_env)
    key = env.get(PROVIDERS[config.provider]["key"], "")
    if is_placeholder_secret(key):
        return plan(snapshot, tasks, config, status="not_executed_no_credentials")

    if config.resume is not None:
        output = config.resume
        if not output.exists():
            raise FileNotFoundError("Resume directory does not exist")
        state = json.loads((output / "run_state.json").read_text(encoding="utf-8"))
        expected = _fingerprint(_config_payload(config, snapshot))
        if state.get("config_fingerprint") != expected:
            raise ValueError("Resume configuration does not match the original run")
        prior = [json.loads(line) for line in
                 (output / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = [row for row in prior if row.get("run_status") == "completed"]
        completed = {row["task_id"] for row in rows}
    else:
        output = config.output
        output.mkdir(parents=True, exist_ok=False)
        rows, completed = [], set()
        (output / "run_state.json").write_text(json.dumps({
            "run_id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_id": snapshot.snapshot_id,
            "task_set_version": snapshot.task_set_version,
            "backend": config.backend,
            "config_fingerprint": _fingerprint(_config_payload(config, snapshot)),
            "supported_online_backends": ["react", "workflow"],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    traces = output / "traces"
    traces.mkdir(exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    total_calls = sum(row.get("attempt_model_calls", 0) for row in rows)
    total_tokens = sum(row.get("attempt_provider_tokens", 0) for row in rows)
    attempts_total = sum(row.get("attempts", 1) for row in rows)
    status, stopped_reason = "completed_pending_human_review", None
    pending = [task for task in tasks if task["task_id"] not in completed]
    env["LIVE_RETRIEVAL_MODE"] = config.retrieval_mode
    index_report: dict[str, Any] | None = None
    with patch.dict(os.environ, env), \
            tempfile.TemporaryDirectory(prefix="cma-v4-online-") as temporary:
        template = materialize_template(snapshot, Path(temporary) / "template")
        if config.retrieval_mode in {"dense", "hybrid"}:
            # Same verified-index requirement as the offline formal runner.
            # Failure aborts instead of silently scoring a BM25 fallback.
            try:
                index_report = indexing.prepare_indexes(template[0], snapshot)
            except IndexPreparationError:
                if config.resume is None:
                    shutil.rmtree(output, ignore_errors=True)
                raise
        for task in pending:
            remaining_calls = config.max_model_calls - total_calls
            remaining_tokens = config.max_budget_tokens - total_tokens
            if remaining_calls <= 0:
                status, stopped_reason = "stopped_model_call_budget", "max_model_calls"
                break
            if remaining_tokens <= 0:
                status, stopped_reason = "stopped_token_budget", "max_budget_tokens"
                break
            attempt = 0
            while True:
                attempt += 1
                clone = clone_databases(template, Path(temporary) / "runs" /
                                        f"{task['task_id']}-{attempt}-{uuid4().hex[:6]}")
                raw = run_task(
                    snapshot, task, config.backend, clone,
                    injected_failure=snapshot.label(task["task_id"]).get("injected_failure"),
                    budget_override=_per_task_budget(
                        config, config.max_model_calls - total_calls,
                        config.max_budget_tokens - total_tokens),
                    online=True,
                    online_limits={
                        "max_model_calls": max(1, config.max_model_calls - total_calls),
                        "max_budget_tokens": max(1, config.max_budget_tokens - total_tokens),
                        "timeout_seconds": config.timeout_seconds,
                    })
                if config.retrieval_mode in {"dense", "hybrid"}:
                    try:
                        assert_requested_channel(raw, config.retrieval_mode,
                                                 task["task_id"])
                    except RetrievalChannelError:
                        if config.resume is None:
                            shutil.rmtree(output, ignore_errors=True)
                        raise
                total_calls += raw.get("model_calls", 0)
                total_tokens += (raw.get("provider_input_tokens", 0) +
                                 raw.get("provider_output_tokens", 0))
                attempts_total += 1
                row = _row(snapshot, task, raw, clone, attempt)
                if (row["run_status"] in {"rate_limited", "provider_unavailable"}
                        and attempt <= config.max_retries):
                    time.sleep(min(2 ** (attempt - 1), 5))
                    continue
                break
            rows.append(row)
            (traces / f"{task['task_id']}.json").write_text(
                json.dumps({"task_id": task["task_id"], "backend": config.backend,
                            "run_id": raw.get("run_id"), "attempts": attempt,
                            "result": raw}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            if row["run_status"] == "insufficient_balance":
                status, stopped_reason = "stopped_insufficient_balance", "http_402"
                break
            if (config.backend == "react" and row["run_status"] == "offline_fallback"
                    and len(rows) == 1):
                status, stopped_reason = "not_executed_no_effective_credentials", "offline_fallback"
                break
            if row["run_status"] == "budget_exhausted":
                status, stopped_reason = "stopped_model_budget_exhausted", "external_cap"
                break
    execution_seconds = time.perf_counter() - started

    (output / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (output / "pending_grading.json").write_text(
        json.dumps(pending_grading(rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _write_summary(output, snapshot, config, rows, status=status,
                          stopped_reason=stopped_reason, attempts_total=attempts_total,
                          started_at=started_at, execution_seconds=execution_seconds,
                          total_model_calls=total_calls, total_tokens=total_tokens,
                          index_report=index_report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true",
                        help="explicitly authorize external requests")
    parser.add_argument("--i-understand-this-makes-paid-model-calls", action="store_true",
                        dest="confirmed", help="required second confirmation")
    parser.add_argument("--plan-only", action="store_true", help="print the plan and make no request")
    parser.add_argument("--backend", choices=["react", "workflow"], default="react",
                        help="execution backend; both reuse the production chains")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=False)
    parser.add_argument("--model", required=False)
    parser.add_argument("--base-url", required=False)
    parser.add_argument("--api-key-env", help="environment variable name holding the key")
    parser.add_argument("--approved-origin", help="exact HTTPS origin authorized by the operator")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--task", action="append", dest="task_ids", default=[])
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--max-model-calls", type=int, required=False)
    parser.add_argument("--max-budget-tokens", type=int, required=False)
    parser.add_argument("--max-model-rounds", type=int, default=5)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--deadline-seconds", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retrieval-mode", choices=["bm25", "dense", "hybrid"], default="bm25")
    return parser


def main() -> None:
    args = _parser().parse_args()
    provider = args.provider or "deepseek"
    env_name = args.api_key_env or PROVIDERS[provider]["key"]
    config = OnlineConfig(
        provider=provider, model=args.model or "", base_url=args.base_url or "",
        approved_origin=args.approved_origin, api_key_env=env_name,
        output=args.output, resume=args.resume, task_ids=tuple(args.task_ids),
        categories=tuple(args.category), max_tasks=args.max_tasks,
        max_model_calls=args.max_model_calls or 0,
        max_budget_tokens=args.max_budget_tokens or 0,
        max_model_rounds=args.max_model_rounds, max_tool_calls=args.max_tool_calls,
        max_output_tokens=args.max_output_tokens, max_retries=args.max_retries,
        timeout_seconds=args.deadline_seconds, retrieval_mode=args.retrieval_mode,
        backend=args.backend)
    summary = run(config, online=args.online, confirmed=args.confirmed,
                  plan_only=args.plan_only)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
