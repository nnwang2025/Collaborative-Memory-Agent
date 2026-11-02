"""Task-level scoring for the Phase 4 benchmark.

Citation integrity, evidence recall, structured facts, abstention signals,
conflict signals and decision snapshots are deterministic checks.  Semantic
support and Task Success are *not*: they stay null until an independent human
grade is applied.  A deterministic signal never becomes a semantic score.
"""
from __future__ import annotations

import math
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore

SCORING_VERSION = "cma-v4-scoring-v1"
TOP_K = 10
STRUCTURED_FACT_TYPES = {"component", "version", "commit", "issue_id", "date",
                         "decision_state", "person"}
CONFLICT_WORDS = ("冲突", "不一致", "矛盾", "两份", "双方", "保留双方")
ABSTAIN_WORDS = ("证据不足", "无法确认", "不能确认", "未找到", "没有足够", "不足以",
                 "未确认", "待审核", "没有正式批准", "尚无确认版本", "缺少")
REFUSAL_TERMINATIONS = {"insufficient", "conflict", "tool_failure", "budget"}
SECTION_BREAKS = ("【关键证据】", "【证据", "【来源文件】", "【任务覆盖", "【未解决的缺口】",
                  "【关联实体】", "【置信度】", "【路由结果】")
CONFIRMATION_RE = re.compile(r"(?:已|正式)(?:确认|批准)|当前确认版本|当前版本为")
NEGATIONS = ("未", "不", "没", "无", "尚")


def _normalise(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).lower()


def answer_body(answer: str) -> str:
    """Remove echoed questions and evidence listings from the judged answer.

    An answer that merely repeats the question or pastes a retrieved snippet
    has not answered it.  The full answer is kept for manual review.
    """
    lines = []
    for line in str(answer or "").splitlines():
        if line.startswith("针对问题"):
            continue
        lines.append(line)
    body = "\n".join(lines)
    for marker in SECTION_BREAKS:
        position = body.find(marker)
        if position >= 0:
            body = body[:position]
    return body.strip()


def _contains(answer: str, value: str, fact_type: str) -> bool:
    text, needle = _normalise(answer), _normalise(value)
    if not needle:
        return False
    if fact_type == "commit" and re.fullmatch(r"[0-9a-f]{40,64}", needle):
        prefix = needle[:12]
        return re.search(r"(?<![0-9a-f])" + re.escape(prefix) + r"(?![0-9a-f])", text) is not None
    if re.fullmatch(r"[a-z0-9_.:/-]+", needle):
        return re.search(r"(?<![a-z0-9_])" + re.escape(needle) + r"(?![a-z0-9_])", text) is not None
    return needle in text


def _fact_result(fact: dict[str, Any], body: str, full_answer: str) -> dict[str, Any]:
    fact_type = fact.get("fact_type")
    required = bool(fact.get("required", True))
    values = [str(item) for item in fact.get("values", [])]
    if values:
        matched = [value for value in values if _contains(body, value, fact_type)]
        return {"fact_id": fact.get("fact_id"), "type": fact_type, "required": required,
                "checked": True, "present": bool(matched),
                "correct": (bool(matched) if required else (True if matched else None)),
                "matched_values": matched, "expected_values": values,
                "method": "structured_value"}
    groups = fact.get("marker_groups", [])
    if groups:
        matched_groups = [group for group in groups
                          if any(_normalise(marker) in _normalise(body) for marker in group)]
        return {"fact_id": fact.get("fact_id"), "type": "text", "required": required,
                "checked": True, "present": bool(matched_groups),
                "correct": ((len(matched_groups) == len(groups)) if required
                            else (True if matched_groups else None)),
                "matched_groups": len(matched_groups), "expected_groups": len(groups),
                "method": "marker_groups_pending_manual_semantic_review"}
    return {"fact_id": fact.get("fact_id"), "type": fact_type, "required": required,
            "checked": False, "present": False, "correct": None, "method": "manual_only"}


def _forbidden_signal(label: dict[str, Any], body: str) -> dict[str, Any] | None:
    groups = label.get("forbidden_inferences") or []
    if not groups:
        return None
    text = _normalise(body)
    hits = [marker for group in groups for marker in group
            if _normalise(marker) in text]
    return {"detected": bool(hits), "hits": hits, "group_count": len(groups),
            "method": "explicit_forbidden_phrase_detection"}


def _group_hits(groups: list[dict[str, Any]], ids: set[str]) -> dict[str, Any]:
    details, matched = [], 0
    for group in groups:
        alternatives = [ref["evidence_id"] for ref in group.get("any_of", [])]
        hit = sorted(ids.intersection(alternatives))
        if hit:
            matched += 1
        details.append({"group_id": group.get("group_id"), "matched": bool(hit),
                        "matched_ids": hit, "alternatives": alternatives})
    return {"matched": matched, "total": len(groups),
            "score": (matched / len(groups)) if groups else None, "groups": details}


def _positive_confirmation(body: str) -> bool:
    for match in CONFIRMATION_RE.finditer(body):
        prefix = body[max(0, match.start() - 4):match.start()]
        if not any(negation in prefix for negation in NEGATIONS):
            return True
    return False


def _abstention_signal(task: dict[str, Any], label: dict[str, Any], raw: dict[str, Any],
                       body: str, recall: dict[str, Any]) -> dict[str, Any]:
    expected = label.get("expected_abstention") or {}
    required = bool(expected.get("required"))
    disclosure_groups = expected.get("must_disclose") or []
    disclosed = any(any(_normalise(marker) in _normalise(body) for marker in group)
                    for group in disclosure_groups)
    confirms = _positive_confirmation(body)
    allowed = expected.get("expected_termination") or list(REFUSAL_TERMINATIONS)
    termination = raw.get("termination_reason")
    termination_known = termination != "not_reported_by_workflow"
    allowed_match = (termination in allowed) if termination_known else None
    if required:
        proxy = bool(allowed_match) and disclosed and not confirms
        if not termination_known:
            proxy = None
        signal = {"required": True, "appropriate_abstention_proxy": proxy,
                  "disclosed_limitation": disclosed, "termination_matches": allowed_match,
                  "positive_confirmation": confirms,
                  "unsupported_answer_signal": confirms or (not disclosed and bool(body))}
        if not termination_known:
            signal["not_evaluated_reason"] = "workflow_does_not_report_termination"
        return signal
    refusal = (termination in REFUSAL_TERMINATIONS and
               any(word in _normalise(body) for word in ABSTAIN_WORDS))
    sufficient_evidence = recall.get("score") == 1
    return {"required": False, "false_refusal_signal": bool(refusal and sufficient_evidence),
            "refusal_observed": refusal,
            "evidence_recall": recall.get("score"), "positive_confirmation": confirms,
            "inappropriate_refusal_reason": ("refused_despite_required_evidence_retrieved"
                                             if refusal and sufficient_evidence else None)}


def _conflict_signal(label: dict[str, Any], body: str) -> dict[str, Any] | None:
    expected = label.get("expected_conflicts") or []
    if not expected:
        return None
    details = []
    for conflict in expected:
        values = [str(item) for item in conflict.get("values", [])]
        present = [value for value in values if _contains(body, value, "version")]
        vocabulary = any(word in _normalise(body) for word in CONFLICT_WORDS)
        details.append({"field": conflict.get("field"), "values_present": present,
                        "all_values_present": len(present) == len(values),
                        "conflict_vocabulary": vocabulary,
                        "detected": len(present) == len(values) and vocabulary})
    return {"detected": all(item["detected"] for item in details), "details": details}


def _decision_signal(task: dict[str, Any], label: dict[str, Any], raw: dict[str, Any],
                     body: str) -> dict[str, Any] | None:
    state = label.get("expected_decision_state")
    if not state:
        return None
    target_decision = state.get("decision_id")
    memories = [item for item in raw.get("decision_memories", [])
                if isinstance(item, dict)
                and (target_decision is None or item.get("decision_id") == target_decision)]
    current = {item.get("version_id") for item in memories if item.get("system_current")}
    expected = state.get("expected_current_version_id")
    memory_queried = any(event.get("tool_name") == "search_memory" and not event.get("error")
                         for event in raw.get("events", []))
    if expected is None:
        correct = not current if memory_queried else None
        return {"expected_current_version_id": None, "current_version_ids": sorted(current),
                "decision_state_correct": correct, "memory_queried": memory_queried,
                "pending_disclosed": any(marker in _normalise(body)
                                         for marker in ("未确认", "待审核", "没有正式批准",
                                                        "尚无确认版本")),
                "method": "decision_memory_snapshot"}
    correct = expected in current and not (current - {expected})
    future = [item for item in state.get("expected_version_ids", []) if item not in current]
    return {"expected_current_version_id": expected,
            "current_version_ids": sorted(item for item in current if item),
            "decision_state_correct": correct if memory_queried else None,
            "memory_queried": memory_queried,
            "versions_not_visible": future,
            "known_at": label.get("known_at"),
            "method": "decision_memory_snapshot"}


def _citation_signal(raw: dict[str, Any], scope: EvidenceScope, store: EvidenceStore,
                     claim_checks: list[dict[str, Any]]) -> dict[str, Any]:
    refs = raw.get("citation_refs", [])
    retrieved = set(raw.get("retrieved_ids", []))
    details = []
    for ref in refs:
        identifier, digest = ref.get("evidence_id"), ref.get("content_hash")
        record = store.get(identifier, scope, digest)
        current = store.current_state(identifier, scope, digest)
        details.append({
            "evidence_id": identifier,
            "exists": record is not None,
            "scope_ok": record is not None and record.project_id == scope.project_id,
            "hash_ok": record is not None and record.content_hash == digest,
            "from_this_run": identifier in retrieved,
            "current": bool(current and current.get("state") == "active" and current.get("matches")),
            "valid": bool(record is not None and identifier in retrieved and current
                          and current.get("state") == "active" and current.get("matches")),
        })
    valid = sum(item["valid"] for item in details)
    exact_claims = [item for item in claim_checks if item.get("all_refs_resolve")]
    exact_sources = sum(item.get("source_text_exact_in_cited_evidence") for item in exact_claims)
    return {"citation_count": len(details), "valid_citations": valid,
            "citation_validity": (valid / len(details)) if details else None,
            "details": details,
            "claims_with_resolved_refs": len(exact_claims),
            "claims_with_exact_source_text": exact_sources,
            "claim_source_exactness": (exact_sources / len(exact_claims)) if exact_claims else None}


def _recovery_signal(label: dict[str, Any], raw: dict[str, Any], required_ids: set[str]) -> dict[str, Any] | None:
    if not label.get("injected_failure"):
        return None
    events = raw.get("events", [])
    failures = [event for event in events if str(event.get("error", "")).startswith("injected_")]
    recovered = False
    for failure in failures:
        later = [event for event in events
                 if event.get("order", 0) > failure.get("order", 0) and not event.get("error")]
        if any(set(event.get("evidence_ids", [])) & required_ids for event in later):
            recovered = True
            break
    return {"failure_observed": bool(failures), "recovered": recovered if failures else None,
            "failure_events": len(failures), "method": "tool_event_order_and_required_evidence"}


def _termination_signal(label: dict[str, Any], raw: dict[str, Any]) -> bool | None:
    expected = label.get("expected_termination")
    if not expected:
        return None
    termination = raw.get("termination_reason")
    if termination == "not_reported_by_workflow":
        return None
    return termination in expected


def score_run(raw: dict[str, Any], task: dict[str, Any], label: dict[str, Any],
              *, evidence_db: Path) -> dict[str, Any]:
    scope = EvidenceScope(project_id=task["project_id"], dataset_kind=task["dataset_kind"])
    store = EvidenceStore(str(evidence_db))
    delivered = list(raw.get("retrieved_ids", []))
    top_k_ids = set(delivered[:TOP_K])
    recall = _group_hits(label.get("required_evidence_groups", []), top_k_ids)
    recall_all = _group_hits(label.get("required_evidence_groups", []), set(delivered))
    body = answer_body(raw.get("answer", ""))
    full_answer = raw.get("answer", "")
    facts = [_fact_result(fact, body, full_answer) for fact in label.get("expected_facts", [])]
    required_facts = [fact for fact in facts if fact["checked"] and fact["required"]]
    optional_facts = [fact for fact in facts if fact["checked"] and not fact["required"]
                      and fact["present"]]
    fact_accuracy = (sum(fact["correct"] for fact in required_facts) / len(required_facts)
                     if required_facts else None)
    optional_fact_accuracy = (sum(fact["correct"] for fact in optional_facts)
                              / len(optional_facts) if optional_facts else None)
    forbidden = _forbidden_signal(label, body)
    citations = _citation_signal(raw, scope, store, raw.get("claim_source_checks", []))
    abstention = _abstention_signal(task, label, raw, body, recall)
    conflict = _conflict_signal(label, body)
    decision = _decision_signal(task, label, raw, body)
    required_ids = {ref["evidence_id"] for group in label.get("required_evidence_groups", [])
                    for ref in group.get("any_of", [])}
    recovery = _recovery_signal(label, raw, required_ids)
    failure_reasons = []
    if raw.get("runtime_error"):
        failure_reasons.append("runtime_error:" + str(raw["runtime_error"]))
    if recall["score"] == 0:
        failure_reasons.append("no_required_evidence_delivered")
    if citations["citation_count"] == 0:
        failure_reasons.append("no_citations")
    elif citations["valid_citations"] < citations["citation_count"]:
        failure_reasons.append("invalid_citations")
    if fact_accuracy is not None and fact_accuracy < 1:
        failure_reasons.append("structured_fact_mismatch")
    if forbidden and forbidden.get("detected"):
        failure_reasons.append("forbidden_inference")
    if decision and decision.get("decision_state_correct") is False:
        failure_reasons.append("decision_state_mismatch")
    if conflict and not conflict.get("detected"):
        failure_reasons.append("expected_conflict_not_disclosed")
    if recovery and recovery.get("failure_observed") and not recovery.get("recovered"):
        failure_reasons.append("tool_failure_not_recovered")
    if abstention.get("required") and abstention.get("appropriate_abstention_proxy") is False:
        failure_reasons.append("abstention_signal_failed")
    manual_reasons = ["task_success", "semantic_support_of_claims"]
    if conflict:
        manual_reasons.append("conflict_interpretation")
    if decision:
        manual_reasons.append("decision_state_interpretation")
    if abstention.get("required"):
        manual_reasons.append("abstention_appropriateness")
    return {
        "task_id": task["task_id"], "category": task["category"],
        "difficulty": task["difficulty"], "project_id": task["project_id"],
        "backend": raw.get("backend"), "task_success": None,
        "needs_manual_review": True, "manual_review_reasons": manual_reasons,
        "manual": None,
        "metrics": {
            "evidence_recall_at_k": recall["score"],
            "evidence_recall_all_delivered": recall_all["score"],
            "citation_validity": citations["citation_validity"],
            "fact_accuracy": fact_accuracy,
            "optional_fact_accuracy": optional_fact_accuracy,
            "forbidden_inference_detected": (forbidden.get("detected") if forbidden else None),
            "appropriate_abstention_proxy": abstention.get("appropriate_abstention_proxy"),
            "false_refusal_signal": abstention.get("false_refusal_signal", False),
            "unsupported_answer_signal": abstention.get("unsupported_answer_signal", False),
            "conflict_detection": conflict.get("detected") if conflict else None,
            "decision_state_correct": decision.get("decision_state_correct") if decision else None,
            "tool_failure_recovered": recovery.get("recovered") if recovery else None,
            "termination_matches_expected": _termination_signal(label, raw),
        },
        "details": {"evidence": recall, "facts": facts, "citations": citations,
                    "abstention": abstention, "conflict": conflict, "decision": decision,
                    "forbidden": forbidden, "recovery": recovery},
        "failure_reasons": failure_reasons,
        "raw": raw,
        "gold_ref": {"task_id": label["task_id"], "task_set_version": label["task_set_version"]},
    }


def _mean(values: Iterable[Any]) -> float | None:
    numeric = [value for value in values if isinstance(value, (int, float))]
    return (sum(numeric) / len(numeric)) if numeric else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"count": 0, "TaskSuccessRate": None, "not_evaluated": {"all": "no tasks"}}
    metric = lambda name: _mean(row["metrics"].get(name) for row in rows)
    latencies = [row["raw"].get("latency_ms", 0.0) for row in rows]
    manual = [row.get("manual") for row in rows]
    manual_complete = all(item is not None and type(item.get("task_success")) is bool
                          for item in manual)
    task_success = (_mean(item["task_success"] for item in manual)
                    if manual_complete else None)
    graded = sum(1 for item in manual if item is not None)
    pending = len(rows) - graded
    decision_rows = [row for row in rows if row["details"].get("decision")]
    conflict_rows = [row for row in rows if row["details"].get("conflict")]
    abstention_rows = [row for row in rows if row["details"].get("abstention", {}).get("required")]
    non_abstention_rows = [row for row in rows if not row["details"].get("abstention", {}).get("required")]
    failure_rows = [row for row in rows if row["details"].get("recovery")]
    metric_names = ("evidence_recall_at_k", "citation_validity", "fact_accuracy",
                    "optional_fact_accuracy", "forbidden_inference_detected",
                    "appropriate_abstention_proxy", "conflict_detection",
                    "decision_state_correct", "tool_failure_recovered")
    metric_coverage = {
        name: {"measured": sum(row["metrics"].get(name) is not None for row in rows),
               "total": len(rows)}
        for name in metric_names}
    metric_coverage["task_success"] = {"measured": graded, "total": len(rows)}
    not_evaluated = {}
    for key, value in (("TaskSuccessRate", task_success),
                       ("AppropriateAbstentionRate", metric("appropriate_abstention_proxy")),
                       ("ConflictDetectionRate", metric("conflict_detection")),
                       ("DecisionStateAccuracy", metric("decision_state_correct")),
                       ("ToolFailureRecoveryRate", metric("tool_failure_recovered")),
                       ("CitationValidity", metric("citation_validity")),
                       ("FactAccuracy", metric("fact_accuracy")),
                       ("OptionalFactAccuracy", metric("optional_fact_accuracy")),
                       ("ForbiddenInferenceRate", metric("forbidden_inference_detected"))):
        if value is None:
            not_evaluated[key] = "insufficient_observations_or_manual_grade_required"
    if not manual_complete:
        not_evaluated["TaskSuccessRate"] = "pending_independent_human_review"
    return {
        "count": len(rows),
        "TaskSuccessRate": task_success,
        "TaskSuccessRate_status": ("human_graded" if manual_complete else "pending_human_review"),
        "EvidenceRecall@K": metric("evidence_recall_at_k"),
        "EvidenceRecall_all_delivered": metric("evidence_recall_all_delivered"),
        "EvidenceRecall@K_policy": f"first {TOP_K} delivered Evidence IDs per task",
        "CitationValidity": metric("citation_validity"),
        "FactAccuracy": metric("fact_accuracy"),
        "OptionalFactAccuracy": metric("optional_fact_accuracy"),
        "ForbiddenInferenceRate": metric("forbidden_inference_detected"),
        "AppropriateAbstentionRate": metric("appropriate_abstention_proxy"),
        "AppropriateAbstentionRate_status": "automatic_signal_not_semantic_grade",
        "ConflictDetectionRate": metric("conflict_detection"),
        "DecisionStateAccuracy": metric("decision_state_correct"),
        "ToolFailureRecoveryRate": metric("tool_failure_recovered"),
        "false_refusal_rate": _mean(row["metrics"].get("false_refusal_signal") for row in non_abstention_rows),
        "unsupported_answer_rate": _mean(row["metrics"].get("unsupported_answer_signal")
                                          for row in abstention_rows),
        "average_tool_calls": _mean(row["raw"].get("tool_calls") for row in rows),
        "average_model_calls": _mean(row["raw"].get("model_calls") for row in rows),
        "p50_latency_ms": _percentile(latencies, 0.5),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "estimated_tokens": sum(row["raw"].get("estimated_tokens", 0) for row in rows),
        "provider_input_tokens": sum(row["raw"].get("provider_input_tokens", 0) for row in rows),
        "provider_output_tokens": sum(row["raw"].get("provider_output_tokens", 0) for row in rows),
        "actual_cost_usd": None,
        "actual_cost_status": "not_measured",
        "termination_reason_distribution": _termination_counts(rows),
        "model_calls_total": sum(row["raw"].get("model_calls", 0) for row in rows),
        "manual_grading": {"graded": graded, "pending": pending,
                           "complete": manual_complete},
        "metric_coverage": metric_coverage,
        "decision_tasks": len(decision_rows),
        "conflict_tasks": len(conflict_rows),
        "abstention_tasks": len(abstention_rows),
        "tool_failure_tasks": len(failure_rows),
        "not_evaluated": not_evaluated,
    }


def _termination_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = row["raw"].get("termination_reason") or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def aggregate_by(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    values = sorted({str(row.get(field)) for row in rows})
    return {value: aggregate([row for row in rows if str(row.get(field)) == value])
            for value in values}


def pending_grading(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = []
    for row in rows:
        if row.get("manual") is None:
            tasks.append({
                "task_id": row["task_id"], "backend": row["backend"],
                "category": row["category"], "difficulty": row["difficulty"],
                "required_fields": ["reviewer", "task_success", "notes"],
                "optional_fields": ["appropriate_abstention", "semantic_support",
                                    "conflict_interpretation", "decision_interpretation"],
                "automatic_signals": row["metrics"],
                "failure_reasons": row["failure_reasons"],
            })
    return {"scoring_version": SCORING_VERSION, "pending": len(tasks),
            "instructions": ("TaskSuccessRate stays null until every executed task has an explicit "
                             "boolean task_success from a named reviewer. Semantic support cannot be "
                             "inferred from citation integrity."),
            "tasks": tasks}


def apply_manual_grades(rows: list[dict[str, Any]],
                        grades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach explicit human grades; never fabricate missing task success."""
    index = {(row["backend"], row["task_id"]): row for row in rows}
    for grade in grades:
        key = (grade.get("backend"), grade.get("task_id"))
        if key not in index:
            raise ValueError(f"Grade references an unknown run: {key}")
        if not grade.get("reviewer"):
            raise ValueError(f"{key}: reviewer is required")
        if type(grade.get("task_success")) is not bool:
            raise ValueError(f"{key}: task_success must be an explicit boolean")
        index[key]["manual"] = {
            "reviewer": grade["reviewer"],
            "task_success": grade["task_success"],
            "notes": grade.get("notes", ""),
            "appropriate_abstention": grade.get("appropriate_abstention"),
            "semantic_support": grade.get("semantic_support"),
            "conflict_interpretation": grade.get("conflict_interpretation"),
            "decision_interpretation": grade.get("decision_interpretation"),
        }
        index[key]["task_success"] = grade["task_success"]
        index[key]["needs_manual_review"] = False
    return rows


def diagnose(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = []
    for row in rows:
        if row["failure_reasons"]:
            cases.append({"task_id": row["task_id"], "backend": row["backend"],
                          "category": row["category"], "difficulty": row["difficulty"],
                          "reasons": row["failure_reasons"],
                          "termination_reason": row["raw"].get("termination_reason"),
                          "model_calls": row["raw"].get("model_calls"),
                          "tool_calls": row["raw"].get("tool_calls")})
    return cases
