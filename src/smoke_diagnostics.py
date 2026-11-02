"""Offline analysis of saved synthetic smoke reports. No client, credentials or network."""
from collections import Counter
import hashlib
import json
from pathlib import Path

from src.investigation import parse_action, protocol_diagnostic


def diagnose(report, *, legacy_output_limit=None):
    scenarios = []
    for result in report.get("results", []):
        decisions = [s for s in result.get("trace", []) if s["action"] != "tool_attempt"]
        rows = []
        for index, text in enumerate(result.get("actions", [])):
            step = decisions[index] if index < len(decisions) else {}
            metadata = result.get("model_responses", [])
            metadata = metadata[index] if index < len(metadata) else {}
            finish_reason = metadata.get("finish_reason", step.get("finish_reason"))
            limit = metadata.get("requested_output_tokens", step.get("requested_output_tokens")) or legacy_output_limit
            usage = metadata.get("usage") or step.get("usage", {})
            row = {"call_index": index + 1, "response_chars": len(text), "finish_reason": finish_reason,
                   "requested_output_tokens": limit, "output_tokens": usage.get("output_tokens"),
                   "category": "schema_valid", "validation_issues": []}
            try:
                if finish_reason not in {None, "stop"}: raise ValueError("Incomplete provider response")
                row["action"] = parse_action(text).action
            except (ValueError, TypeError) as error:
                row.update(protocol_diagnostic(error, finish_reason=finish_reason))
                if isinstance(error, json.JSONDecodeError):
                    row["json_error"] = error.msg
                    row["json_position"] = error.pos
            row["truncation_confirmed"] = row["category"] == "output_truncated"
            row["output_limit_suspected"] = (finish_reason is None and limit is not None and
                usage.get("output_tokens") == limit and row["category"] in {"json_syntax", "empty_output"})
            rows.append(row)
        scenarios.append({"scenario": result["scenario"], "model": result.get("model"),
            "termination_reason": result.get("termination_reason"), "termination_detail": result.get("termination_detail"),
            "model_calls": result.get("model_calls"), "category_counts": dict(Counter(r["category"] for r in rows)),
            "provider_stop_metadata_missing": sum(r["finish_reason"] is None for r in rows),
            "truncation_confirmed": sum(r["truncation_confirmed"] for r in rows),
            "output_limit_suspected": sum(r["output_limit_suspected"] for r in rows),
            "citation_errors": sum(e["error_code"] == "citation_error" for s in result.get("trace", []) for e in s.get("errors", [])),
            "tool_order": result.get("tool_order", []), "actions": rows})
    return {"analysis_mode": "offline_saved_report", "legacy_output_limit": legacy_output_limit,
        "notice": "Token count is not proof of truncation. Missing finish_reason cannot be reconstructed.",
        "scenarios": scenarios,
        "category_counts": dict(Counter(r["category"] for s in scenarios for r in s["actions"]))}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--legacy-output-limit", type=int, help="Explicit historical cap established from source code")
    parser.add_argument("--output", type=Path, help="New analysis report; never replaces the input or an existing file")
    args = parser.parse_args()
    payload = args.report.read_bytes()
    analysis = diagnose(json.loads(payload), legacy_output_limit=args.legacy_output_limit)
    analysis["source_sha256"] = hashlib.sha256(payload).hexdigest()
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(analysis, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"source_sha256": analysis["source_sha256"], "category_counts": analysis["category_counts"],
        "scenarios": [{k: v for k, v in s.items() if k != "actions"} for s in analysis["scenarios"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
