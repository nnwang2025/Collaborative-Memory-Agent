"""Explicit metric denominators. Semantic task success is never guessed."""
import math
from statistics import mean


def fraction(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def retrieval_metrics(ranked_ids, gold_ids, k=5):
    gold = set(gold_ids)
    complete = list(dict.fromkeys(ranked_ids))
    ranked = complete[:k]
    if not gold:
        return {"hit": None, "recall": None, "mrr": None}
    positions = [i + 1 for i, identifier in enumerate(complete) if identifier in gold]
    return {"hit": int(bool(set(ranked) & gold)), "recall": len(set(ranked) & gold) / len(gold),
            "mrr": 1 / positions[0] if positions else 0.0}


def aggregate(rows, k=5):
    metrics = [retrieval_metrics(r["retrieved_ids"], r["gold_evidence_ids"], k) for r in rows]
    eligible = [m for m in metrics if m["hit"] is not None]
    graded = [r for r in rows if r["status"] not in {"unsupported", "ungraded"} and r.get("human_task_success") is not None]
    abstentions = [r for r in rows if r["category"] == "insufficient" and r.get("human_appropriate_abstention") is not None and r["status"] != "unsupported"]
    recovery = [r for r in rows if r["failure_triggered"] and r["status"] != "unsupported"]
    memory = [r for r in rows if r.get("decision_checks") is not None]
    def mean_field(name):
        values = [m[name] for m in eligible]
        return {"query_count": len(values), "value": mean(values) if values else None}
    latencies = sorted(r["latency_ms"] for r in rows)
    attempts = sum(r["tool_attempts"] for r in rows)
    model_calls = sum(r["model_calls"] for r in rows)
    return {
        f"Hit@{k}": mean_field("hit"), f"Recall@{k}": mean_field("recall"), "MRR": mean_field("mrr"),
        "TaskSuccessRate": fraction(sum(bool(r["human_task_success"]) for r in graded), len(graded)),
        "CitationValidityRate": fraction(sum(r["valid_citations"] for r in rows), sum(r["citation_count"] for r in rows)),
        "EvidenceCoverage": {"query_count": len(eligible), "value": mean(
            len(set(r["valid_cited_ids"]) & set(r["gold_evidence_ids"])) / len(set(r["gold_evidence_ids"]))
            for r in rows if r["gold_evidence_ids"]) if eligible else None},
        "AppropriateAbstentionRate": fraction(sum(bool(r["human_appropriate_abstention"]) for r in abstentions), len(abstentions)),
        "ToolFailureRecoveryRate": fraction(sum(r["operational_recovery"] for r in recovery), len(recovery)),
        "InvalidActionRate": fraction(sum(r["invalid_actions"] for r in rows), model_calls),
        "DuplicateToolCallRate": fraction(sum(r["duplicate_calls"] for r in rows), sum(r["action_tool_requests"] for r in rows)),
        "latency_mean_ms": mean(latencies) if latencies else None,
        "latency_p95_ms": latencies[max(0, math.ceil(len(latencies) * .95) - 1)] if latencies else None,
        "model_calls": model_calls, "tool_attempts": attempts,
        "provider_input_tokens": sum(r["provider_input_tokens"] for r in rows),
        "provider_output_tokens": sum(r["provider_output_tokens"] for r in rows),
        "estimated_tokens": sum(r["estimated_tokens"] for r in rows),
        "BudgetExhaustionRate": fraction(sum(r["termination_reason"] == "budget" for r in rows), len(rows)),
        "unsupported": sum(r["status"] == "unsupported" for r in rows),
        "ungraded": sum(r["status"] == "ungraded" for r in rows),
        "runtime_errors": sum(bool(r.get("runtime_error")) for r in rows),
        "decision_checks": {name: fraction(sum(bool(r["decision_checks"].get(name)) for r in memory if name in r["decision_checks"]),
            sum(name in r["decision_checks"] for r in memory)) for name in
            ("current_version", "historical_versions", "supersedes", "pending_not_confirmed", "world_unknown")},
    }
