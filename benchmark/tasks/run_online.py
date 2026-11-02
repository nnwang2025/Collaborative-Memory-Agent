"""Explicit-opt-in online task evaluation through the formal live ReAct chain.

Default mode is a plan only: no provider client, no credential inspection and no
network request.  Online mode requires an exact HTTPS origin, a new output
directory and an explicit ``--online`` flag.  The runtime receives only public
task questions; labels are joined after execution for scoring.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

from benchmark.dataset import ROOT as BENCHMARK_ROOT, load as load_dataset, materialize
from benchmark.tasks.scoring import (CAPABILITIES, aggregate, configuration, diagnose, load_tasks,
                                     pending_grading, score_task, validate_tasks)
from src.agents.react_agent import ReActAgent
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.investigation import Budget
from src.llm_config import resolve_api_key, resolve_base_url, resolve_model_name


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward credentials to another origin


def _model_name():
    return resolve_model_name(resolve_base_url())


def _configured_base_url():
    return resolve_base_url()


def _origin(configured, approved_origin):
    parsed = urlsplit(configured)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or approved_origin != origin):
        raise ValueError("Explicit authorization must exactly match the configured HTTPS service origin")
    return origin, parsed.hostname


def _select(tasks, task_ids, max_tasks):
    selected = [task for task in tasks["public"].tasks if not task_ids or task.task_id in task_ids]
    if task_ids:
        missing = set(task_ids) - {task.task_id for task in selected}
        if missing:
            raise ValueError("Unknown task_id(s): " + ", ".join(sorted(missing)))
    if max_tasks is not None:
        if max_tasks < 1:
            raise ValueError("--max-tasks must be positive")
        selected = selected[:max_tasks]
    if not selected:
        raise ValueError("No tasks selected")
    return selected


def plan(selected, budget, *, output=None):
    return {
        "status": "pending_authorization",
        "online_status": "pending_authorization",
        "online_executed": False,
        "message": "No model call was made. Authorize explicitly with --online, an exact --approved-origin, and a new --output directory.",
        "task_set_version": "cma-task-eval-v1",
        "task_count": len(selected),
        "categories": sorted({task.category for task in selected}),
        "max_normal_model_calls": len(selected) * budget.max_model_rounds,
        "max_truncation_recovery_calls_per_task": 1,
        "max_truncation_recovery_calls": len(selected),
        "max_model_calls": len(selected) * (budget.max_model_rounds + 1),
        "max_tool_calls": len(selected) * budget.max_tool_calls,
        "max_requested_output_tokens_per_call": max(budget.max_output_tokens,
                                                    budget.max_truncation_retry_output_tokens),
        "max_requested_output_tokens_upper_bound": len(selected) * (budget.max_model_rounds + 1) * max(
            budget.max_output_tokens, budget.max_truncation_retry_output_tokens),
        "budget": budget.model_dump(),
        "output": str(output) if output else None,
        "cost_status": "unavailable_no_pricing",
        "unverified_steps": [
            "real-model semantic task success (requires human grading)",
            "real-model tool-failure recovery quality",
            "provider-side cost (no pricing table is assumed)",
        ],
        "required_flags": ["--online", "--approved-origin <exact configured HTTPS origin>", "--output <new directory>"],
    }


def _events(result):
    events = []
    for step in result.trace:
        if step.action != "tool_attempt":
            continue
        events.append({
            "tool_name": step.tool_name,
            "query": step.query,
            "filters": step.filters,
            "evidence_ids": list(step.evidence_ids),
            "error": step.errors[0].error_code if step.errors else None,
            "order": len(events) + 1,
            "elapsed_ms": step.elapsed_ms,
        })
    return events


def _raw_result(result, scope, store, started_at, latency_ms):
    events = _events(result)
    valid = [ref for ref in result.evidence_refs if store.get(ref.evidence_id, scope, ref.content_hash) is not None]
    # Delivered evidence (tool observations), not citations: recall must not be
    # capped by what the answer happens to cite.
    retrieved = list(dict.fromkeys(identifier for event in events for identifier in event["evidence_ids"]))
    total_tokens = result.usage.input_tokens + result.usage.output_tokens
    return {
        "run_id": result.run_id,
        "backend": "react",
        "mode": result.mode,
        "answer": result.answer,
        "runtime_error": None,
        "started_at": started_at,
        "latency_ms": latency_ms,
        "model_calls": sum(step.usage.input_tokens + step.usage.output_tokens > 0 for step in result.trace),
        "provider_input_tokens": result.usage.input_tokens,
        "provider_output_tokens": result.usage.output_tokens,
        "estimated_tokens": total_tokens if result.usage.estimated else 0,
        "termination_reason": result.termination_reason,
        "termination_detail": result.termination_detail,
        "citation_refs": [ref.model_dump() for ref in result.evidence_refs],
        "citation_count": len(result.evidence_refs),
        "valid_citations": len(valid),
        "valid_cited_ids": [ref.evidence_id for ref in valid],
        "retrieved_ids": retrieved,
        "decision_memories": result.decision_memories,
        "invalid_actions": sum(error.error_code == "protocol_error" for step in result.trace for error in step.errors),
        "duplicate_calls": sum(error.error_code == "duplicate_call" for step in result.trace for error in step.errors),
        "action_tool_requests": sum(step.action == "tool_attempt" for step in result.trace),
        "tool_attempts": len(events),
        "failure_triggered": any(event["error"] for event in events),
        "events": events,
        "raw_investigation": result.model_dump(mode="json"),
    }


def _error_raw(started_at, latency_ms, exc):
    return {
        "run_id": None, "backend": "react", "mode": "react", "answer": "", "runtime_error": type(exc).__name__,
        "started_at": started_at, "latency_ms": latency_ms, "model_calls": 0,
        "provider_input_tokens": 0, "provider_output_tokens": 0, "estimated_tokens": 0,
        "termination_reason": None, "citation_refs": [], "citation_count": 0, "valid_citations": 0,
        "valid_cited_ids": [], "retrieved_ids": [], "decision_memories": [], "invalid_actions": 0,
        "duplicate_calls": 0, "action_tool_requests": 0, "tool_attempts": 0,
        "failure_triggered": False, "events": [], "raw_investigation": None,
    }


def run(*, online=False, approved_origin=None, output=None, task_ids=None, max_tasks=None,
        max_model_rounds=5, max_tool_calls=8, max_output_tokens=2048,
        max_truncation_retry_output_tokens=4096, deadline_seconds=120, root=BENCHMARK_ROOT):
    data = load_dataset(root)
    tasks = load_tasks()
    validation = validate_tasks(tasks, data)
    selected = _select(tasks, task_ids, max_tasks)
    budget = Budget(max_model_rounds=max_model_rounds, max_tool_calls=max_tool_calls,
                    max_output_tokens=max_output_tokens,
                    max_truncation_retry_output_tokens=max_truncation_retry_output_tokens,
                    deadline_seconds=deadline_seconds)
    if not online:
        return plan(selected, budget, output=output)

    configured = _configured_base_url()
    origin, provider = _origin(configured, approved_origin)
    if not output:
        raise ValueError("Online mode requires a new --output directory")
    output = Path(output)
    if output.exists():
        raise FileExistsError("Report directory exists; choose a new path before any provider request")
    if not output.parent.is_dir():
        raise FileNotFoundError("Create the report parent directory before authorizing provider requests")
    if not resolve_api_key(configured):
        return {**plan(selected, budget, output=output),
                "status": "not_executed_no_credentials",
                "online_status": "not_executed_no_credentials",
                "online_executed": False}
    output.mkdir(parents=True, exist_ok=False)
    traces = output / "traces"
    traces.mkdir()
    rows = []
    records = {record.evidence_id: record for record in data["records"]}
    with tempfile.TemporaryDirectory(prefix="cma-task-online-") as temporary:
        for index, task in enumerate(selected):
            label = tasks["labels"].labels[task.task_id]
            runtime = materialize(data, Path(temporary) / f"{task.task_id}-online")
            evidence_path, _ = runtime
            scope = EvidenceScope(project_id=task.project_id, dataset_kind=task.dataset_kind)
            started_at = datetime.now(timezone.utc).isoformat()
            start = time.perf_counter()
            try:
                agent = ReActAgent(scope=scope, db_path=evidence_path, budget=budget)
                agent._open_request = urllib.request.build_opener(_NoRedirect()).open
                if label.injected_failure:
                    unavailable_skill = agent.executor.registry.get(label.injected_failure.skill)
                    def unavailable(*args, **kwargs):
                        raise TimeoutError("Synthetic task-evaluation tool failure")
                    unavailable_skill.run = unavailable
                result = agent.run_result(task.question)
                if index == 0 and result.mode == "offline_fallback":
                    summary = {
                        "status": "not_executed_no_effective_credentials",
                        "online_status": "not_executed_no_effective_credentials",
                        "online_executed": False,
                        "message": "The runtime fell back offline; no result is reported as a real model result.",
                        "task_set_version": validation["task_set_version"],
                        "task_set_sha256": validation["task_set_sha256"],
                        "corpus_snapshot_id": validation["corpus_snapshot_id"],
                    }
                    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
                    return summary
                latency = (time.perf_counter() - start) * 1000
                raw = _raw_result(result, scope, EvidenceStore(evidence_path), started_at, latency)
                (traces / f"{task.task_id}.json").write_text(
                    json.dumps({"task_id": task.task_id, "started_at": started_at,
                                "latency_ms": latency, "result": raw["raw_investigation"]},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                row = score_task(raw, task, label, records, execution_mode="online_model",
                                 supported_capabilities=CAPABILITIES, manual_grade=None)
            except Exception as exc:
                latency = (time.perf_counter() - start) * 1000
                raw = _error_raw(started_at, latency, exc)
                (traces / f"{task.task_id}.json").write_text(
                    json.dumps({"task_id": task.task_id, "started_at": started_at, "latency_ms": latency,
                                "runtime_error": type(exc).__name__}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                row = score_task(raw, task, label, records, execution_mode="online_model",
                                 supported_capabilities=CAPABILITIES)
            row["diagnosis"] = diagnose(row)
            rows.append(row)
    runtime_errors = sum(row["status"] == "runtime_error" for row in rows)
    fallbacks = sum(row["status"] == "offline_fallback_not_real_model" for row in rows)
    summary = {
        "report_type": "online_task_eval_real_react",
        "status": ("completed_with_runtime_errors_pending_human_review" if runtime_errors
                   else "completed_pending_human_review"),
        "online_executed": True,
        "model_calls": sum(row["metrics"]["model_calls"] for row in rows),
        "manual_grades": 0,
        "config": configuration(data, validation, execution_mode="online_model", model=_model_name(),
                                provider=provider, provider_origin=origin,
                                params={"approved_origin": approved_origin, "max_tasks": max_tasks,
                                        "task_ids": [task.task_id for task in selected],
                                        "max_model_rounds": max_model_rounds, "max_tool_calls": max_tool_calls,
                                        "max_output_tokens": max_output_tokens,
                                        "max_truncation_retry_output_tokens": max_truncation_retry_output_tokens,
                                        "deadline_seconds": deadline_seconds,
                                        "temperature": 0.1,
                                        "max_normal_model_calls_upper_bound": len(selected) * max_model_rounds,
                                        "max_truncation_recovery_calls_upper_bound": len(selected),
                                        "max_model_calls_upper_bound": len(selected) * (max_model_rounds + 1),
                                        "max_tool_calls_upper_bound": len(selected) * max_tool_calls,
                                        "max_requested_output_tokens_per_call": max(
                                            max_output_tokens, max_truncation_retry_output_tokens),
                                        "max_requested_output_tokens_upper_bound": (
                                            len(selected) * (max_model_rounds + 1) * max(
                                                max_output_tokens, max_truncation_retry_output_tokens))}),
        "validation": validation,
        "execution": {"tasks": len(selected), "rows": len(rows), "runtime_errors": runtime_errors,
                      "offline_fallback_rows": fallbacks},
        "metrics": aggregate(rows),
        "diagnosis_counts": {
            hypothesis: sum(hypothesis in row["diagnosis"]["hypotheses"] for row in rows)
            for hypothesis in sorted({item for row in rows for item in row["diagnosis"]["hypotheses"]})
        },
        "warnings": [
            "Semantic task success is manual-only and remains unverified until a human grades every task.",
            "Deterministic fact coverage, citations and evidence counts do not constitute task success.",
            "offline_fallback results are never reported as real model results.",
            "Cost is unavailable because no provider pricing table is assumed.",
        ],
    }
    (output / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                          encoding="utf-8")
    (output / "pending_grading.json").write_text(
        json.dumps(pending_grading(rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="Explicitly authorize external requests with synthetic data")
    parser.add_argument("--approved-origin", help="Exact configured HTTPS service origin explicitly authorized by the operator")
    parser.add_argument("--output", type=Path, help="New report directory; existing paths are never replaced")
    parser.add_argument("--task", action="append", dest="task_ids", help="repeatable task_id")
    parser.add_argument("--max-tasks", type=int, help="optional cap on selected tasks")
    parser.add_argument("--max-model-rounds", type=int, default=5)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--max-truncation-retry-output-tokens", type=int, default=4096)
    parser.add_argument("--deadline-seconds", type=float, default=120)
    parser.add_argument("--root", type=Path, default=BENCHMARK_ROOT)
    args = parser.parse_args()
    summary = run(online=args.online, approved_origin=args.approved_origin, output=args.output,
                  task_ids=args.task_ids, max_tasks=args.max_tasks, max_model_rounds=args.max_model_rounds,
                  max_tool_calls=args.max_tool_calls, max_output_tokens=args.max_output_tokens,
                  max_truncation_retry_output_tokens=args.max_truncation_retry_output_tokens,
                  deadline_seconds=args.deadline_seconds, root=args.root)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary.get("status") in {"online_failed", "not_executed_no_effective_credentials"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
