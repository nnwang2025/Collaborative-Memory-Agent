"""Deterministic Phase 3 context-engineering benchmark.

No provider calls, no API keys and no hidden Gold labels are read.  The
"Phase 2" arm reconstructs the pre-Phase-3 request shape (all observed excerpt
bodies, only the latest observation, byte ceiling, no token budget).  It is an
explicit emulation for comparison, not a re-run of a frozen report.

Usage:
    python -m benchmark.v3.runner --output benchmark/v3/reports/<new-dir>
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from src.context_budget import (
    BUDGET_POLICY_VERSION, CONTEXT_SCHEMA_VERSION, ContextBudget,
    TokenCounter, build_policy, resolve_model_context_window,
)
from src.evidence import Evidence, EvidenceScope
from src.investigation import EvidenceLedger, ToolError, TraceStep
from src.investigation_context import (
    ContextLimitError, InvestigationContext, select_excerpt,
)

DEFAULT_OUTPUT = "benchmark/v3/reports/phase3-context-20261102"
MODEL = "deepseek-chat"
WINDOW = resolve_model_context_window(MODEL)


def make_record(content, record_id, *, source_type="document",
                source_uri=None, event_time=None):
    return Evidence.create(
        project_id="phase3-bench", dataset_kind="live",
        source_type=source_type,
        source_uri=source_uri or f"benchmark://phase3/{record_id}",
        original_record_id=record_id, content=content, event_time=event_time,
        ingested_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        locator={"path": f"{record_id}.md"}).model_dump(mode="json")


def view(record, query, score=0.5, *, basis="verified_excerpt"):
    parent = Evidence.model_validate(record)
    excerpt = select_excerpt(parent, query)
    return {**parent.model_dump(mode="json"), "score": score,
            "score_kind": "retrieval_relevance",
            "excerpt_basis": basis,
            "excerpt": excerpt.model_dump(mode="json")}


def ref(record):
    return {"evidence_id": record["evidence_id"],
            "content_hash": record["content_hash"]}


def messages_for(views, question, *, error=None):
    payload = {"evidence": views}
    if error:
        payload["error"] = error
    return [{"role": "system", "content": "System rules: cite only verified evidence."},
            {"role": "user", "content": question},
            {"role": "assistant", "content": '{"action":"tool_call"}'},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def budget_for(remaining=10**9, *, max_excerpts=8, max_per=1,
               max_bytes=48000, window=None):
    policy = build_policy(
        model=MODEL, max_tokens=64000, max_output_tokens=2048,
        context_window_tokens=window, safety_margin_tokens=512,
        max_context_bytes=max_bytes, max_excerpts=max_excerpts,
        max_excerpts_per_evidence=max_per)
    return ContextBudget(policy, TokenCounter(MODEL, "estimate"),
                         remaining_total_tokens=remaining)


# ---------------------------------------------------------------------------
# Phase 2-shaped baseline (explicit emulation of the replaced request builder)
# ---------------------------------------------------------------------------

def _key(value):
    excerpt = value.get("excerpt", {}) or {}
    return (value.get("evidence_id"), value.get("content_hash"),
            excerpt.get("char_start"), excerpt.get("char_end"))


def legacy_request(views, messages, ledger, trace, coverage, gaps, max_bytes=48000):
    recent = copy.deepcopy(
        messages[-2:] if len(messages) > 3 and messages[-2].get("role") == "assistant"
        else messages[2:][-1:])
    latest = []
    if recent:
        try:
            latest = json.loads(recent[-1].get("content") or "").get("evidence", [])
        except (ValueError, TypeError):
            latest = []
    latest_keys = {_key(item) for item in latest if item.get("content")}
    earlier = [copy.deepcopy(item) for item in views if _key(item) not in latest_keys]
    index = []
    for entry in ledger.entries:
        parent = entry.evidence
        index.append({
            **parent.model_dump(mode="json",
                                exclude={"content", "metadata", "ingested_at"}),
            "time_status": "unknown" if parent.event_time is None else "recorded",
            "excerpts": [copy.deepcopy(item["excerpt"]) for item in views
                         if (item.get("evidence_id"), item.get("content_hash"))
                         == (parent.evidence_id, parent.content_hash)]})
    state = {
        "evidence_index": index,
        "retained_excerpts": earlier,
        "task_coverage": coverage,
        "gaps": list(dict.fromkeys(list(ledger.gaps) + list(gaps))),
        "conflicts": [conflict.model_dump() for conflict in ledger.conflicts],
        "decision_memories": [] if latest else [],
        "tool_history": [{
            "tool": step.tool_name, "query": step.query,
            "filters": step.filters, "evidence_ids": step.evidence_ids,
            "errors": [error.model_dump() for error in step.errors],
            "recovery": step.recovery_kind}
            for step in trace if step.tool_name or step.errors],
    }

    def render():
        return (messages[:2]
                + [{"role": "user", "content": json.dumps(state, ensure_ascii=False)}]
                + recent)

    def size(value):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    request = render()
    omitted = 0
    for item in earlier + latest:
        if size(request) <= max_bytes:
            break
        if item.get("content"):
            item["content"] = ""
            item["body_omitted"] = "request_context_byte_budget"
            omitted += 1
        if latest:
            payload = json.loads(recent[-1]["content"])
            payload["evidence"] = latest
            recent[-1]["content"] = json.dumps(payload, ensure_ascii=False)
        request = render()
    if size(request) > max_bytes:
        raise ContextLimitError("legacy byte budget exceeded")
    return request, {"omitted_excerpt_bodies": omitted}


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------

def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_dicts(item)


def metrics(request, ledger, claims, trace, window, latency_ms):
    counter = TokenCounter(MODEL, "estimate")
    tokens = counter.count_messages(request)
    serialized = json.dumps(request, ensure_ascii=False)
    payloads = []
    for message in request:
        content = message.get("content")
        try:
            payloads.append(json.loads(content))
        except (ValueError, TypeError):
            payloads.append(content)
    dicts = list(walk_dicts(payloads))
    identities = {(item.get("evidence_id"), item.get("content_hash"))
                  for item in dicts
                  if item.get("evidence_id") and item.get("content_hash")}
    body_identities = {
        (item.get("evidence_id"), item.get("content_hash"))
        for item in dicts
        if item.get("content") and item.get("evidence_id")
        and item.get("content_hash")}
    bodies = [str(item.get("content")) for item in dicts if item.get("content")]
    unique_bodies = set(bodies)
    duplicate_ratio = ((len(bodies) - len(unique_bodies)) / len(bodies)
                       if bodies else 0.0)
    ledger_refs = {(entry.evidence.evidence_id, entry.evidence.content_hash)
                   for entry in ledger.entries}
    conflict_refs = {(item.evidence_id, item.content_hash)
                     for conflict in ledger.conflicts for item in conflict.refs}
    claim_refs = {(item["evidence_id"], item["content_hash"])
                  for claim in claims for item in claim.get("refs", [])}
    state = {}
    try:
        state = json.loads(request[2].get("content") or "")
    except (ValueError, TypeError, IndexError):
        state = {}
    history = state.get("tool_history") or []
    expected_attempts = {(step.tool_name, step.query) for step in trace
                         if step.action == "tool_attempt" and step.tool_name}
    reported_attempts = {(item.get("tool"), item.get("query")) for item in history}
    expected_gaps = set(ledger.gaps)
    gap_values = set(state.get("gaps") or [])
    session_meta = state.get("session_context_meta") or {}
    return {
        "estimated_prompt_tokens": tokens,
        "context_occupancy": tokens / window,
        "input_bytes": len(serialized.encode("utf-8")),
        "body_count": len(bodies),
        "distinct_body_count": len(unique_bodies),
        "duplicate_content_ratio": duplicate_ratio,
        "evidence_ref_retention": fraction(len(ledger_refs & identities),
                                           len(ledger_refs)),
        "critical_conflict_retention": fraction(len(conflict_refs & body_identities),
                                                len(conflict_refs)),
        "claim_support_retention": fraction(len(claim_refs & body_identities),
                                            len(claim_refs)),
        "tool_history_fidelity": fraction(len(expected_attempts & reported_attempts),
                                          len(expected_attempts)),
        "gap_retention": fraction(len(expected_gaps & gap_values),
                                  len(expected_gaps)),
        "context_limit": False,
        "latency_ms": latency_ms,
        "has_verified_claims_state": bool(state.get("verified_claims")),
        "verified_claims_marked_run_local": all(
            item.get("source_kind") == "validated_claim_from_this_run"
            for item in state.get("verified_claims") or []),
        "session_marked_non_factual": (bool(session_meta)
                                       and not session_meta.get("fact_source")),
        "state_keys": sorted(state),
    }


def fraction(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def limit_metrics(error, window, latency_ms):
    detail = getattr(error, "detail", {}) or {}
    reason = (detail.get("context_limit") or {}).get("reason")
    required = detail.get("estimated_input_tokens")
    return {
        "estimated_prompt_tokens": required,
        "context_occupancy": (required / window) if required else None,
        "input_bytes": detail.get("input_bytes"),
        "body_count": None,
        "distinct_body_count": None,
        "duplicate_content_ratio": None,
        "evidence_ref_retention": None,
        "critical_conflict_retention": None,
        "claim_support_retention": None,
        "tool_history_fidelity": None,
        "gap_retention": None,
        "context_limit": True,
        "context_limit_reason": reason or "mandatory_state_exceeds_budget",
        "latency_ms": latency_ms,
        "has_verified_claims_state": None,
        "verified_claims_marked_run_local": None,
        "session_marked_non_factual": None,
        "state_keys": [],
    }


# ---------------------------------------------------------------------------
# Scenario construction
# ---------------------------------------------------------------------------

FAILURE = ToolError(error_code="tool_timeout", retryable=True,
                    message="Synthetic source timed out.", tool_name="search_docs",
                    elapsed_ms=0.0)


def scenario_short_few():
    first = make_record("The team chose bounded latency to keep p95 stable.", "short-1")
    second = make_record("A review confirmed the bounded-latency decision.", "short-2")
    return dict(question="Why was bounded latency chosen?", records=[first, second],
                views=[view(first, "bounded latency", 0.9),
                       view(second, "review confirmed", 0.7)],
                claims=[], trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_docs", query="bounded latency",
                              evidence_ids=[first["evidence_id"], second["evidence_id"]])])


def scenario_long_multisource():
    records = []
    for index in range(12):
        source_type = ("document", "issue", "meeting", "chat")[index % 4]
        records.append(make_record(
            f"Source {index}: decision evidence about rollout and latency.",
            f"multi-{index:02d}", source_type=source_type,
            event_time=f"2025-{index % 9 + 1:02d}-01T00:00:00+00:00"))
    return dict(question="Trace the whole rollout decision end-to-end.",
                records=records,
                views=[view(record, "rollout decision", 0.9 - index * 0.02)
                       for index, record in enumerate(records)],
                claims=[], trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_memory", query="rollout decision",
                              evidence_ids=[record["evidence_id"] for record in records])])


def scenario_duplicate_evidence():
    content = "The same reviewed decision text appears in two export copies."
    first = make_record(content, "dup-1")
    second = make_record(content, "dup-2")
    same_view = view(first, "reviewed decision", 0.8)
    return dict(question="What did the reviewed decision say?",
                records=[first, second],
                views=[same_view, dict(same_view, score=0.6),
                       view(second, "reviewed decision", 0.7)],
                claims=[], trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_docs", query="reviewed decision",
                              evidence_ids=[first["evidence_id"],
                                            second["evidence_id"]]),
                    TraceStep(step_index=2, action="tool_attempt",
                              tool_name="search_docs", query="reviewed decision",
                              evidence_ids=[first["evidence_id"]])])


def scenario_long_doc_multichunk():
    content = ("HEAD: the original decision used bounded latency.\n"
               + "Background material without the key fact. " * 180
               + "\nMIDDLE: a later review proposed a different timeout.\n"
               + "More background without the key fact. " * 180
               + "\nTAIL: the final record kept bounded latency.")
    record = make_record(content, "long-doc")
    return dict(question="What changed across the long review document?",
                records=[record],
                views=[view(record, "original decision", 0.8),
                       view(record, "later review", 0.75),
                       view(record, "final record", 0.7)],
                claims=[], trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_docs", query="long review",
                              evidence_ids=[record["evidence_id"]])])


def scenario_critical_conflict():
    original = make_record("The approved timeout is thirty seconds.", "conflict")
    revised = make_record("The approved timeout is now sixty seconds.", "conflict")
    ledger_safe = [original, revised]
    return dict(question="What is the approved timeout?", records=ledger_safe,
                views=[view(original, "approved timeout", 0.02),
                       view(revised, "approved timeout", 0.01)],
                claims=[], conflict=True, trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_docs", query="approved timeout",
                              evidence_ids=[original["evidence_id"],
                                            revised["evidence_id"]])])


def scenario_verified_claims():
    record = make_record("Bounded latency was the recorded reason for the choice.",
                         "claim")
    history = make_record("An older note recommended a different reason.",
                          "claim-history")
    claims = [{"text": "Bounded latency was the reason.",
               "kind": "historical", "refs": [ref(record)]}]
    return dict(question="Why was the choice made?",
                records=[record, history],
                views=[view(record, "recorded reason", 0.05),
                       view(history, "older note", 0.9)],
                claims=claims, trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_memory", query="recorded reason",
                              evidence_ids=[record["evidence_id"],
                                            history["evidence_id"]])])


def scenario_multiturn_followup():
    record = make_record("A later review adjusted the timeout after the first decision.",
                         "follow-up")
    history = {"previous_investigations": [{
        "run_id": "prior-run-1",
        "question": "Why was bounded latency chosen?",
        "status": "succeeded",
        "answer": "Historical answer: bounded latency was chosen.",
        "verified_evidence_refs": [{"evidence_id": "ev_old",
                                    "content_hash": "0" * 64}],
        "unresolved_gaps": ["No later review was found."],
        "conflicts": []}]}
    return dict(question="那后来呢？为什么又改了？",
                records=[record], views=[view(record, "later review", 0.6)],
                claims=[], session_context=json.dumps(history, ensure_ascii=False),
                trace=[TraceStep(step_index=1, action="tool_attempt",
                                 tool_name="search_docs", query="later review",
                                 evidence_ids=[record["evidence_id"]])])


def scenario_irrelevant_session():
    record = make_record("Current-run evidence says the timeout stayed unchanged.",
                         "irrelevant-session")
    history = {"previous_investigations": [
        {"run_id": f"old-{index}",
         "question": "Unrelated payroll question " * 20,
         "status": "succeeded",
         "answer": "IRRELEVANT-HISTORY " * 200,
         "verified_evidence_refs": [], "unresolved_gaps": [], "conflicts": []}
        for index in range(10)]}
    return dict(question="Was the timeout changed?",
                records=[record], views=[view(record, "timeout", 0.5)],
                claims=[], session_context=json.dumps(history, ensure_ascii=False),
                trace=[TraceStep(step_index=1, action="tool_attempt",
                                 tool_name="search_docs", query="timeout",
                                 evidence_ids=[record["evidence_id"]])])


def scenario_tool_failure_retry():
    record = make_record("Evidence obtained after one failed retrieval.", "retry")
    failed = TraceStep(step_index=1, action="tool_attempt",
                       tool_name="search_docs", query="timeout decision",
                       errors=[FAILURE])
    retried = TraceStep(step_index=2, action="tool_attempt",
                        tool_name="search_docs", query="timeout decision",
                        evidence_ids=[record["evidence_id"]])
    empty = TraceStep(step_index=3, action="tool_attempt",
                      tool_name="search_issues", query="no matching issue")
    return dict(question="What happened after the retrieval failure?",
                records=[record], views=[view(record, "failed retrieval", 0.4)],
                claims=[], trace=[failed, retried, empty])


def scenario_extreme_low_budget():
    record = make_record("Evidence that cannot fit a mandatory prompt.", "tiny")
    return dict(question="A question whose mandatory context exceeds a tiny budget.",
                records=[record], views=[view(record, "evidence", 0.5)],
                claims=[], trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_docs", query="evidence",
                              evidence_ids=[record["evidence_id"]])],
                budget_remaining=150)


def scenario_different_budget():
    records = [make_record(
        f"Budget record {index}: " + f"rollout evidence item {index} " * 40,
        f"budget-{index}") for index in range(6)]
    return dict(question="Explain the rollout decision.",
                records=records,
                views=[view(record, "rollout evidence", 0.6)
                       for record in records],
                claims=[], trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_docs", query="rollout",
                              evidence_ids=[record["evidence_id"]
                                            for record in records])],
                budget_variants=[("roomy", 10**9), ("tight", 7000)])


def scenario_inexact_locator():
    record = make_record(
        "A parent record contains the tail fact about the final review. "
        + "Unrelated body text. " * 60
        + "\nTAIL-LOCATOR-FACT: the final review kept the timeout.",
        "inexact")
    return dict(question="What did the final review keep?",
                records=[record],
                views=[view(record, "TAIL-LOCATOR-FACT", 0.5,
                            basis="parent_evidence_fallback")],
                claims=[], trace=[
                    TraceStep(step_index=1, action="tool_attempt",
                              tool_name="search_docs", query="final review",
                              evidence_ids=[record["evidence_id"]])])


SCENARIOS = [
    ("short_few", "少量短证据", scenario_short_few),
    ("long_multisource", "长上下文多来源", scenario_long_multisource),
    ("duplicate_evidence", "重复证据/重复工具结果", scenario_duplicate_evidence),
    ("long_doc_multichunk", "长文档多分块", scenario_long_doc_multichunk),
    ("critical_conflict", "关键冲突低分证据", scenario_critical_conflict),
    ("verified_claims", "已验证 Claim 与历史证据", scenario_verified_claims),
    ("multiturn_followup", "多轮追问", scenario_multiturn_followup),
    ("irrelevant_session", "无关会话历史干扰", scenario_irrelevant_session),
    ("tool_failure_retry", "工具失败/重试信息", scenario_tool_failure_retry),
    ("extreme_low_budget", "极低预算", scenario_extreme_low_budget),
    ("different_budget", "不同预算配置", scenario_different_budget),
    ("inexact_locator", "非精确 locator 摘录", scenario_inexact_locator),
]


def run_case(case_id, title, builder, config_name, remaining, max_bytes=48000):
    data = builder()
    scope = EvidenceScope(project_id="phase3-bench", dataset_kind="live")
    ledger = EvidenceLedger(scope)
    records = data["records"]
    # Conflict cases intentionally register two content versions of one identity.
    ledger.register(data["views"], "search_docs")
    for record in records:
        if not any(entry.evidence.evidence_id == record["evidence_id"]
                   and entry.evidence.content_hash == record["content_hash"]
                   for entry in ledger.entries):
            ledger.register([view(record, data["question"], 0.0)], "search_docs")
    coverage = {"required_source_types": sorted({record["source_type"]
                                                 for record in records}),
                "observed_source_types": sorted({record["source_type"]
                                                 for record in records}),
                "missing_source_types": [], "targets": [],
                "semantic_completeness": "unverified"}
    context = InvestigationContext()
    views = context.observe(data["views"])
    messages = messages_for(views, data["question"])
    claims = data.get("claims", [])
    trace = data.get("trace", [])

    start = time.perf_counter()
    try:
        phase2_request, phase2_extra = legacy_request(
            list(context.views.values()), messages, ledger, trace, coverage,
            list(ledger.gaps), max_bytes=max_bytes)
        phase2 = metrics(phase2_request, ledger, claims, trace, WINDOW,
                         (time.perf_counter() - start) * 1000)
        phase2["omitted_excerpt_bodies"] = phase2_extra["omitted_excerpt_bodies"]
        phase2["tokenizer_mode"] = "estimated"
    except ContextLimitError as error:
        phase2 = limit_metrics(error, WINDOW,
                               (time.perf_counter() - start) * 1000)
        phase2["tokenizer_mode"] = "estimated"

    start = time.perf_counter()
    try:
        phase3_request, usage = context.request(
            messages, ledger, trace, {}, coverage, max_bytes,
            gaps=list(ledger.gaps), claims=claims,
            question=data["question"],
            session_context=data.get("session_context"),
            context_budget=budget_for(remaining, max_bytes=max_bytes),
            model=MODEL)
        phase3 = metrics(phase3_request, ledger, claims, trace, WINDOW,
                         (time.perf_counter() - start) * 1000)
        for key in ("candidate_evidence_count", "selected_evidence_count",
                    "unique_evidence_count", "deduped_evidence_count",
                    "deduped_excerpt_count", "pruned_count", "gap_count",
                    "important_conflicts_retained", "conflict_bodies_retained",
                    "estimated_input_tokens", "tokenizer_mode",
                    "context_schema_version", "budget_policy_version",
                    "reproducibility"):
            phase3[key] = usage.get(key)
        phase3["pruning_reasons"] = usage.get("pruning_reasons", {})
        phase3["category_tokens"] = usage.get("category_tokens", {})
        phase3.pop("input_bytes", None)
        phase3["input_bytes"] = usage.get("input_bytes")
    except ContextLimitError as error:
        phase3 = limit_metrics(error, WINDOW,
                               (time.perf_counter() - start) * 1000)
        phase3["tokenizer_mode"] = "estimated"
        phase3["context_schema_version"] = CONTEXT_SCHEMA_VERSION

    return {
        "case_id": case_id,
        "title": title,
        "config": config_name,
        "budget_remaining_tokens": remaining,
        "phase2_emulation": phase2,
        "phase3": phase3,
        "delta": compare(phase2, phase3),
        "note": "Scripted/offline construction only; no LLM call, no semantic score.",
    }


def compare(phase2, phase3):
    rows = {}
    for key in ("estimated_prompt_tokens", "context_occupancy",
                "input_bytes", "body_count", "distinct_body_count",
                "duplicate_content_ratio", "context_limit", "latency_ms"):
        left, right = phase2.get(key), phase3.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) \
                and not isinstance(left, bool) and not isinstance(right, bool):
            rows[key] = right - left
        else:
            rows[key] = {"phase2": left, "phase3": right}
    for key in ("evidence_ref_retention", "critical_conflict_retention",
                "claim_support_retention", "tool_history_fidelity",
                "gap_retention"):
        left = (phase2.get(key) or {}).get("value")
        right = (phase3.get(key) or {}).get("value")
        rows[key] = (right - left) if left is not None and right is not None \
            else {"phase2": left, "phase3": right}
    return rows


def aggregate_rows(rows, arm):
    values = {}
    for row in rows:
        for key, value in row[arm].items():
            if isinstance(value, bool):
                values.setdefault(key, []).append(value)
            elif isinstance(value, (int, float)):
                values.setdefault(key, []).append(value)
    result = {}
    for key, items in values.items():
        if all(isinstance(item, bool) for item in items):
            result[key] = {"count": len(items), "true": sum(items)}
        elif items:
            result[key] = {"count": len(items),
                           "mean": statistics.mean(items),
                           "p95": sorted(items)[max(0, math.ceil(len(items) * .95) - 1)]}
    for key in ("evidence_ref_retention", "critical_conflict_retention",
                "claim_support_retention", "tool_history_fidelity",
                "gap_retention"):
        scored = [row[arm][key]["value"] for row in rows
                  if (row[arm].get(key) or {}).get("value") is not None]
        result[key] = {"count": len(scored),
                       "mean": statistics.mean(scored) if scored else None}
    return result


def run(output):
    rows = []
    for case_id, title, builder in SCENARIOS:
        data = builder()
        variants = data.get("budget_variants") or [
            ("default", data.get("budget_remaining", 10**9))]
        for config, remaining in variants:
            rows.append(run_case(case_id, title, builder, config, remaining))
    phase2 = aggregate_rows(rows, "phase2_emulation")
    phase3 = aggregate_rows(rows, "phase3")
    report = {
        "benchmark": "phase3-context-engineering",
        "version": "v3",
        "mode": "offline_deterministic_scripted",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "model_context_window": WINDOW,
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "budget_policy_version": BUDGET_POLICY_VERSION,
        "tokenizer": TokenCounter(MODEL, "estimate").trace(),
        "constraints": {
            "real_llm_calls": 0,
            "api_keys_read": False,
            "v1_v2_artifacts_modified": False,
            "phase2_arm": "explicit_emulation_of_replaced_request_builder",
        },
        "interfaces": {
            "task_success_rate": None,
            "semantic_correctness": None,
            "provider_token_consumption": None,
            "note": ("Scripted construction cannot measure task success, semantic "
                     "correctness or billed tokens; these fields stay null rather "
                     "than impersonating a real model run."),
        },
        "rows": rows,
        "aggregate": {"phase2_emulation": phase2, "phase3": phase3},
        "limitations": [
            "Token counts are conservative heuristic estimates, not billed tokens.",
            "The Phase 2 arm is a reconstruction of the replaced request shape.",
            "No semantic judge or human grading is applied to scripted rows.",
            "Latency is single-run construction time on this machine.",
        ],
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "summary.md").write_text(render_summary(report), encoding="utf-8")
    return output, report


def render_summary(report):
    lines = [
        "# Phase 3 Context Engineering Benchmark", "",
        f"- mode: `{report['mode']}`",
        f"- model: `{report['model']}` (window {report['model_context_window']})",
        f"- schema: `{report['context_schema_version']}`",
        f"- policy: `{report['budget_policy_version']}`",
        "- real LLM calls: 0; task success / semantic correctness: not measured", "",
        "| case | config | P2 tokens | P3 tokens | P2 occupancy | P3 occupancy "
        "| conflict P2/P3 | claim P2/P3 | dup ratio P2/P3 | P3 limit |",
        "|---|---|---:|---:|---:|---:|---|---|---|---|",
    ]
    def rate(row, key):
        value = (row.get(key) or {}).get("value")
        return "n/a" if value is None else f"{value:.2f}"
    for row in report["rows"]:
        p2, p3 = row["phase2_emulation"], row["phase3"]
        def num(value, digits=0):
            if value is None:
                return "n/a"
            return f"{value:.{digits}f}"
        lines.append(
            f"| {row['case_id']} | {row['config']} "
            f"| {num(p2.get('estimated_prompt_tokens'))} "
            f"| {num(p3.get('estimated_prompt_tokens'))} "
            f"| {num(p2.get('context_occupancy'), 3)} "
            f"| {num(p3.get('context_occupancy'), 3)} "
            f"| {rate(p2, 'critical_conflict_retention')}/"
            f"{rate(p3, 'critical_conflict_retention')} "
            f"| {rate(p2, 'claim_support_retention')}/"
            f"{rate(p3, 'claim_support_retention')} "
            f"| {num(p2.get('duplicate_content_ratio'), 2)}/"
            f"{num(p3.get('duplicate_content_ratio'), 2)} "
            f"| {p3.get('context_limit')} |")
    lines += ["", "## Aggregate", ""]
    for arm in ("phase2_emulation", "phase3"):
        agg = report["aggregate"][arm]
        lines.append(f"### {arm}")
        for key in ("estimated_prompt_tokens", "context_occupancy",
                    "duplicate_content_ratio", "latency_ms"):
            value = agg.get(key, {})
            lines.append(f"- {key}: {value.get('mean')} (n={value.get('count')})")
        for key in ("evidence_ref_retention", "critical_conflict_retention",
                    "claim_support_retention", "tool_history_fidelity",
                    "gap_retention"):
            lines.append(f"- {key}: {agg.get(key, {}).get('mean')}")
        lines.append(f"- context_limit true: {agg.get('context_limit', {}).get('true')}")
        lines.append("")
    lines += ["## Limitations", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output, report = run(args.output)
    print(json.dumps({"output": str(output),
                      "rows": len(report["rows"]),
                      "real_llm_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
