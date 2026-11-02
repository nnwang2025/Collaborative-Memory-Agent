"""Synthetic-only smoke runner. Network/model calls require explicit CLI authorization."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from collections import Counter
from urllib.parse import urlsplit
import urllib.request

from src.evidence import EvidenceScope
from src.ingest import ingest_all, normalize_record
from src.evidence_store import EvidenceStore
from src.llm_config import resolve_base_url


SCENARIOS = [
    ("single_source", "Look up the recorded Cedar technical decision and quote its reason."),
    ("cross_source", "Investigate Cedar decision reasons in commits and its later review in documents; cite both."),
    ("multi_round", "First find the Cedar decision commit, then use its date to investigate subsequent adjustments and review documents."),
    ("insufficient", "Is Cedar currently in actual production use? Explain what cannot be established."),
    ("tool_recovery", "Call search_issues first for Cedar. If unavailable, recover with another source and report the failure."),
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # an approved origin must not redirect credentials elsewhere


def prepare(root):
    root = Path(root)
    repo = root / "synthetic-repo"
    repo.mkdir()
    def git(*args, env=None):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, env=env, timeout=10)
    git("init")
    git("config", "user.name", "Synthetic Smoke")
    git("config", "user.email", "synthetic@example.invalid")
    for date, text, message in [
        ("2025-01-01T00:00:00+00:00", "Cedar options A and B.", "Cedar initial design"),
        ("2025-02-01T00:00:00+00:00", "Cedar chose B due to bounded latency.", "Cedar chose B due to bounded latency"),
        ("2025-03-01T00:00:00+00:00", "Cedar review proposes adjusted timeout. Production status unknown.", "Cedar later timeout proposal")]:
        (repo / "cedar.md").write_text(text, encoding="utf-8")
        git("add", "cedar.md")
        git("commit", "-m", message, env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    db = root / "synthetic.sqlite3"
    scope = EvidenceScope(dataset_kind="live", project_id="synthetic-smoke")
    ingest_all(repo_path=repo, docs_dir=repo, project_id=scope.project_id, db_path=db)
    return db, scope


def check_cross_source_evidence(result, store, scope):
    return _check_evidence_roles(result, store, scope,
        {"decision_reason": ("git_commit", "bounded latency"), "later_review": ("document", "adjusted timeout")},
        "cross_source_v2")


def _check_evidence_roles(result, store, scope, expected, version):
    """Synthetic fixture coverage, not semantic task grading or a tool-count test.

    Roles stay evaluator-only. Count accepted historical claims, never merely
    retrieved IDs or the names/number of tools. Re-fetch exact scoped versions.
    """
    observed = {identifier for step in result.trace if step.action == "tool_attempt" and not step.errors
                for identifier in step.evidence_ids}
    records, unavailable = {}, 0
    for ref in result.evidence_refs:
        record = store.get(ref.evidence_id, scope, ref.content_hash)
        if record is None or record.metadata.get("legacy") or ref.evidence_id not in observed:
            unavailable += 1
        else:
            records[(ref.evidence_id, ref.content_hash)] = record
    covered, cited, invalid_claims = {role: False for role in expected}, {}, 0
    for claim in result.claims:
        linked = [records.get((ref.evidence_id, ref.content_hash)) for ref in claim.refs]
        if claim.kind != "historical" or not linked or any(r is None or claim.text not in r.content for r in linked):
            invalid_claims += 1
            continue
        for record in linked:
            cited[(record.evidence_id, record.content_hash)] = record
            for role, (source_type, excerpt) in expected.items():
                if record.source_type == source_type and excerpt in claim.text:
                    covered[role] = True
    missing = [role for role, present in covered.items() if not present]
    scope_matches = result.scope == scope
    return {"check_level": "synthetic_fixture_evidence_coverage", "rule_version": version,
        "passed": scope_matches and not missing and not unavailable and not invalid_claims and not result.conflicts,
        "required_roles": {role: {"source_type": kind, "required_excerpt": text} for role, (kind, text) in expected.items()},
        "role_coverage": covered, "missing_roles": missing, "scope_matches": scope_matches,
        "unverifiable_evidence_count": unavailable, "invalid_claim_count": invalid_claims,
        "retrieved_source_types": sorted({r.source_type for r in records.values()}),
        "cited_source_types": sorted({r.source_type for r in cited.values()}),
        "cited_evidence": [{"evidence_id": r.evidence_id, "content_hash": r.content_hash,
            "project_id": r.project_id, "dataset_kind": r.dataset_kind, "source_type": r.source_type,
            "source_uri": r.source_uri, "event_time": r.model_dump(mode="json")["event_time"]} for r in cited.values()]}


def check_multi_round_evidence(result, store, scope):
    """Evaluator-only facts and temporal links; never imported by the Runtime."""
    from datetime import datetime
    check = _check_evidence_roles(result, store, scope, {"decision_reason": ("git_commit", "bounded latency"),
        "later_adjustment": ("git_commit", "later timeout proposal"), "review_document": ("document", "adjusted timeout")}, "multi_round_v2")
    cited = [store.get(r["evidence_id"], scope, r["content_hash"]) for r in check["cited_evidence"]]
    decisions = [r for r in cited if r.source_type == "git_commit" and "bounded latency" in r.content]
    later = [r for r in cited if r.source_type == "git_commit" and "later timeout proposal" in r.content]
    chronology = any(a.event_time < b.event_time for a in decisions for b in later)
    followup = False
    for decision in decisions:
        seen = False
        for step in result.trace:
            if step.action != "tool_attempt": continue
            after = step.filters.get("after")
            if seen and after and datetime.fromisoformat(after.replace("Z", "+00:00")) == decision.event_time:
                followup = True
            seen = seen or decision.evidence_id in step.evidence_ids
    undated = any(r.source_type == "document" and r.event_time is None for r in cited)
    disclosed = not undated or any(re.search(r"(日期|时间|date|time).*(未知|unknown|undated)", gap, re.I) for gap in result.gaps)
    check.update(chronology_verified=chronology, decision_time_followup=followup, unknown_review_time_disclosed=disclosed,
        passed=check["passed"] and chronology and followup and disclosed)
    return check


def run_suite(*, online=False, approved_origin=None, output=None, max_output_tokens=2048, max_protocol_retries=2):
    # Do not inspect keys or even construct the provider client without authorization.
    if not online:
        with tempfile.TemporaryDirectory(prefix="cma-smoke-prepare-") as directory:
            db, scope = prepare(directory)
            count = len(EvidenceStore(db).list(scope))
        return {"status": "pending_authorization", "online_status": "pending_authorization", "online_executed": False,
                "synthetic_evidence_count": count, "scenarios": [s[0] for s in SCENARIOS]}
    if output and Path(output).exists():
        raise FileExistsError("Smoke report exists; choose a new report path before any provider request")
    if output and not Path(output).parent.is_dir():
        raise FileNotFoundError("Create the report directory before authorizing provider requests")
    configured = resolve_base_url()
    parsed = urlsplit(configured)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.scheme != "https" or parsed.username or parsed.password or approved_origin != origin:
        raise ValueError("Explicit authorization must match the configured HTTPS service origin")
    # Caller explicitly authorizes this configured service for synthetic data only.
    from src.agents.react_agent import ReActAgent
    from src.investigation import Budget
    records = []
    with tempfile.TemporaryDirectory(prefix="cma-smoke-online-") as directory:
        db, scope = prepare(directory)
        for name, question in SCENARIOS:
            started_at = datetime.now(timezone.utc).isoformat()
            started = time.perf_counter()
            agent = ReActAgent(scope=scope, db_path=db, budget=Budget(max_model_rounds=7, max_tokens=90000,
                max_output_tokens=max_output_tokens, max_protocol_retries=max_protocol_retries))
            if not agent.api_key:
                return {"status": "not_executed_no_credentials", "online_status": "not_executed_no_credentials", "online_executed": False}
            agent._open_request = urllib.request.build_opener(_NoRedirect()).open
            actions = []
            requests = []
            responses = []
            original = agent._call_llm
            def observed(messages):
                requests.append(1)
                reply = original(messages)
                # Explicit online smoke retains provider Action text on synthetic data.
                text = reply.text if hasattr(reply, "text") else reply
                actions.append(text.replace(agent.api_key, "[REDACTED]"))
                responses.append({"call_index": len(requests), "finish_reason": getattr(reply, "finish_reason", None),
                    "requested_output_tokens": getattr(reply, "requested_output_tokens", None),
                    "reasoning_tokens": getattr(reply, "reasoning_tokens", None), "response_chars": len(text),
                    "usage": reply.usage.model_dump() if getattr(reply, "usage", None) is not None else None})
                return reply
            agent._call_llm = observed
            if name == "tool_recovery":
                def unavailable(*args, **kwargs):
                    raise TimeoutError("Synthetic tool failure")
                agent.executor.registry.get("problem").run = unavailable
            result = agent.run_result(question)
            errors = [e.error_code for step in result.trace for e in step.errors]
            order = [s.tool_name for s in result.trace if s.action == "tool_attempt"]
            finish_accepted = result.trace[-1].action == "finish" and result.termination_reason != "budget" and not any(
                e.error_code == "citation_error" for e in result.trace[-1].errors)
            valid = bool(actions) and finish_accepted and not any(
                e in errors for e in ("model_error", "protocol_error", "citation_error"))
            cross_source_check = check_cross_source_evidence(result, EvidenceStore(db), scope) if name == "cross_source" else None
            multi_round_check = check_multi_round_evidence(result, EvidenceStore(db), scope) if name == "multi_round" else None
            if name == "single_source": valid = valid and bool(result.claims)
            if name == "cross_source": valid = valid and cross_source_check["passed"]
            if name == "multi_round": valid = valid and multi_round_check["passed"]
            if name == "insufficient": valid = valid and result.termination_reason != "sufficient" and bool(result.gaps)
            if name == "tool_recovery": valid = valid and "temporary_tool_error" in errors and bool(result.evidence_refs)
            record = {"scenario": name, "status": "passed" if valid else "failed", "model": agent.model,
                "check_level": "protocol_and_trace_only", "task_success": None, "task_grading": "pending_human_review",
                "started_at": started_at, "latency_ms": (time.perf_counter() - started) * 1000,
                "model_calls": len(requests), "configuration": {"temperature": 0.1, "budget": agent.budget.model_dump()},
                "action_protocol_errors": errors.count("protocol_error"),
                "protocol_error_categories": dict(Counter(s.protocol_category for s in result.trace if s.protocol_category)),
                "finish_accepted": finish_accepted, "protocol_recovered": finish_accepted and "protocol_error" in errors,
                "cross_source_check": cross_source_check,
                "multi_round_check": multi_round_check,
                "model_responses": responses,
                "invalid_tool_calls": sum(_invalid_tool_action(action) for action in actions),
                "failure_reasons": (errors or ["scenario_protocol_or_trace_expectation_not_met"]) if not valid else [],
                "origin": origin, "actions": actions, "tool_order": order, "errors": errors,
                "duplicate_calls": errors.count("duplicate_call"), "invalid_citations": errors.count("citation_error"),
                **result.model_dump(mode="json")}
            records.append(json.loads(json.dumps(record, ensure_ascii=False).replace(agent.api_key, "[REDACTED]")))
    summary = {"status": "online_protocol_passed" if all(r["status"] == "passed" for r in records) else "online_failed",
               "online_status": "executed", "task_success_rate": None,
               "online_executed": True, "results": records}
    if output:
        # Exclusive creation: never replace an existing report, even on explicit opt-in.
        with Path(output).open("x", encoding="utf-8") as stream:
            json.dump(summary, stream, ensure_ascii=False, indent=2)
    return summary


def _invalid_tool_action(text):
    from src.investigation import TOOLS
    try:
        clean = text.strip()
        if clean.startswith("```") and clean.endswith("```"):
            clean = "\n".join(clean.splitlines()[1:-1])
        value = json.loads(clean)
        return int(isinstance(value, dict) and value.get("action") in ("tool", "tool_call") and value.get("tool") not in TOOLS)
    except (ValueError, TypeError, AttributeError):
        return 0  # malformed JSON is counted as a protocol error instead


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="Explicitly authorize external requests with synthetic data")
    parser.add_argument("--approved-origin", help="Exact configured HTTPS service origin explicitly authorized by the operator")
    parser.add_argument("--output", help="New report path; existing files are never replaced")
    parser.add_argument("--max-output-tokens", type=int, default=2048, help="Per-response output cap, still bounded by total Budget")
    parser.add_argument("--max-protocol-retries", type=int, default=2, help="Consecutive format repairs, within model-round budget")
    args = parser.parse_args()
    summary = run_suite(online=args.online, approved_origin=args.approved_origin, output=args.output,
                        max_output_tokens=args.max_output_tokens, max_protocol_retries=args.max_protocol_retries)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["status"] == "online_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
