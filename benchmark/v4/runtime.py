"""Offline execution of the real ReAct / LangGraph Workflow chains.

No labels reach this module.  A task is reduced to its public question (or
public turns); Gold is joined later by the scorer.  Model credentials are
blanked and outbound HTTP is patched to fail closed, so an offline run cannot
silently become a paid experiment.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.agents.answer_agent import AnswerAgent
from src.agents.coordinator_agent import CoordinatorAgent
from src.agents.react_agent import ReActAgent
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.investigation import Budget
from src.skills.registry import ScopedSkill

from .dataset import Snapshot

CITATION_RE = re.compile(r"evidence_id=(ev_[0-9a-f]{64}) hash=([0-9a-f]{64})")
TOOL_NAMES = {"memory": "search_memory", "problem": "search_issues", "document": "search_docs",
              "dependency": "analyze_dependency", "entrypoint": "find_entrypoint"}
MODEL_ENV_KEYS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "MODEL_NAME",
)
CAPABILITY_NOTES = {
    "react": [
        "offline_fallback: no model decision loop; actions come from keyword routing",
        "claim ledger and citation validation remain active",
        "known_at and time filters require model-produced filters; not exercised offline",
    ],
    "workflow": [
        "fixed LangGraph pipeline: keyword classify -> parallel skills -> merge -> answer",
        "no claim ledger, no budget protocol, no multi-round investigation",
        "known_at decision filtering is not implemented by this chain",
    ],
}
ONLINE_CAPABILITY_NOTES = {
    "react": {
        "native_budget_protocol": True,
        "claim_ledger": True,
        "known_at_filters": True,
        "multi_round_tool_loop": True,
        "model_calls_per_turn": "1..max_model_rounds plus truncation recovery",
    },
    "workflow": {
        "native_budget_protocol": False,
        "external_hard_caps_enforced": True,
        "claim_ledger": False,
        "known_at_filters": False,
        "multi_round_tool_loop": False,
        "model_calls_per_turn": ("2 routing calls (classify + route) + 1 answer summary "
                                 "when the answer has evidence"),
        "fixed_pipeline": "classify_intent -> parallel skills -> merge_rerank -> generate_answer",
    },
}
COMPARABILITY_NOTES = {
    "shared_controls": ["frozen snapshot", "task set", "project scope", "evidence database",
                        "retrieval mode and index", "scorer", "provider/model",
                        "total model-call and token caps"],
    "incomparable_dimensions": [
        "budget exhaustion behavior: Workflow has no native budget protocol and is only stopped by external caps",
        "claim validation and citation ledger: ReAct validates claims against Evidence; Workflow has no Claim Ledger",
        "historical cutoff: ReAct can apply known_at filters; Workflow cannot",
        "investigation depth: ReAct has a multi-round tool loop; Workflow has one fixed retrieval pass",
    ],
    "interpretation": ("Any observed difference is an end-to-end architecture difference. "
                       "It must not be reported as a causal gain from ReAct reasoning."),
}


def _bounded_diagnostics(value: Any) -> dict[str, Any]:
    """Keep only the retrieval fields needed to prove which mode actually ran."""
    if not isinstance(value, dict):
        return {}
    keys = ("requested_mode", "effective_mode", "index_state", "index_version",
            "source_revision", "fallback_reason", "empty_reason",
            "candidate_count", "accepted_count")
    return {key: value.get(key) for key in keys if key in value}


def _retrieval_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    requested = sorted({str(event["retrieval_diagnostics"].get("requested_mode"))
                        for event in events
                        if event.get("retrieval_diagnostics", {}).get("requested_mode")})
    effective = sorted({str(event["retrieval_diagnostics"].get("effective_mode"))
                        for event in events
                        if event.get("retrieval_diagnostics", {}).get("effective_mode")})
    fallbacks = sorted({str(event["retrieval_diagnostics"].get("fallback_reason"))
                        for event in events
                        if event.get("retrieval_diagnostics", {}).get("fallback_reason")})
    index_versions = sorted({str(event["retrieval_diagnostics"].get("index_version"))
                             for event in events
                             if event.get("retrieval_diagnostics", {}).get("index_version")})
    source_revisions = sorted({str(event["retrieval_diagnostics"].get("source_revision"))
                               for event in events
                               if event.get("retrieval_diagnostics", {}).get("source_revision")})
    return {"requested_modes": requested, "effective_modes": effective,
            "fallback_reasons": fallbacks, "index_versions": index_versions,
            "source_revisions": source_revisions,
            "fallback_used": any(mode.endswith("fallback") for mode in effective)}


class OfflineGuard:
    """Context manager that blanks model config and forbids outbound HTTP."""

    def __init__(self, evidence_db: Path, decisions_db: Path):
        self.evidence_db = evidence_db
        self.decisions_db = decisions_db

    def __enter__(self):
        self._stack = patch.dict(os.environ, {
            **{key: "" for key in MODEL_ENV_KEYS},
            "EVIDENCE_DB_PATH": str(self.evidence_db),
            "DECISION_DB_PATH": str(self.decisions_db),
            "LIVE_RETRIEVAL_MODE": os.environ.get("LIVE_RETRIEVAL_MODE", "bm25"),
        })
        self._stack.start()
        self._network = patch("urllib.request.urlopen",
                              side_effect=AssertionError("Offline benchmark forbids network"))
        self._network.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._network.stop()
        self._stack.stop()
        return False


class SkillObserver:
    """Wrap ScopedSkill.run to record actual tool attempts.

    The wrapper records only delivered Evidence IDs and bounded errors, never
    query bodies or raw exceptions.  Failure injection is deliberately at the
    skill boundary so retry/recovery behavior is visible in the trace.
    """

    def __init__(self, injected_failure: dict[str, Any] | None = None):
        self.injected_failure = injected_failure or {}
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._original = None

    def __enter__(self):
        original = ScopedSkill.run
        self._original = original
        injected = self.injected_failure

        def observe(skill, question, *args, **kwargs):
            event = {
                "tool_name": TOOL_NAMES.get(skill.name, skill.name),
                "skill": skill.name,
                "evidence_ids": [],
                "error": None,
                "retrieval_diagnostics": {},
            }
            start = time.perf_counter()
            try:
                if injected and skill.name == injected.get("skill"):
                    event["error"] = "injected_" + str(injected.get("error", "failure"))
                    raise TimeoutError("Synthetic benchmark source unavailable")
                result = original(skill, question, *args, **kwargs)
                event["evidence_ids"] = [item["evidence_id"] for item in
                                         (result or {}).get("evidence", [])]
                event["retrieval_diagnostics"] = _bounded_diagnostics(
                    (result or {}).get("retrieval_diagnostics"))
                return result
            finally:
                event["elapsed_ms"] = (time.perf_counter() - start) * 1000
                with self._lock:
                    event["order"] = len(self.events) + 1
                    self.events.append(event)

        ScopedSkill.run = observe
        return self

    def __exit__(self, exc_type, exc, tb):
        ScopedSkill.run = self._original
        return False


class _ComposeCapture:
    """Capture Workflow-ranked evidence and Decision Memory candidates."""

    def __init__(self):
        self.captured = []
        self.memories = []
        self._original = None

    def __enter__(self):
        original = AnswerAgent.compose
        self._original = original
        captured, memories = self.captured, self.memories

        def compose(agent, question, skill_names, skill_results, route_scores=None):
            hits = [dict(item, skill=name) for name in skill_names
                    for item in skill_results.get(name, {}).get("evidence", [])]
            captured.extend(agent._top_relevant(question, hits)[:8])
            for name in skill_names:
                memories.extend(skill_results.get(name, {}).get("decision_memories", []))
            return original(agent, question, skill_names, skill_results, route_scores)

        AnswerAgent.compose = compose
        return self

    def __exit__(self, exc_type, exc, tb):
        AnswerAgent.compose = self._original
        return False


def _public_turns(task: dict[str, Any]) -> list[str]:
    if task.get("turns"):
        return list(task["turns"])
    return [task["question"]]


def _dedupe_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, output = set(), []
    for ref in refs:
        key = (ref.get("evidence_id"), ref.get("content_hash"))
        if key in seen:
            continue
        seen.add(key)
        output.append({"evidence_id": ref.get("evidence_id"),
                       "content_hash": ref.get("content_hash")})
    return output


def _dedupe_ids(ids: list[str]) -> list[str]:
    seen, output = set(), []
    for identifier in ids:
        if identifier and identifier not in seen:
            seen.add(identifier)
            output.append(identifier)
    return output


def _conversation_context(previous: list[dict[str, Any]], scope: EvidenceScope,
                          store: EvidenceStore) -> str | None:
    if not previous:
        return None
    history = []
    for run in previous[-3:]:
        refs = []
        for ref in run.get("citation_refs", [])[:10]:
            record = store.get(ref["evidence_id"], scope, ref["content_hash"])
            if record is not None:
                refs.append({"evidence_id": record.evidence_id,
                             "content_hash": record.content_hash,
                             "source_type": record.source_type})
        history.append({
            "question": run.get("question"),
            "answer_hint": str(run.get("answer", ""))[:700],
            "gaps": list(run.get("gaps", []))[:5],
            "conflicts": [c.get("description") for c in run.get("conflicts", [])[:3]
                          if isinstance(c, dict)],
            "verified_evidence_refs": refs,
        })
    return json.dumps({
        "note": "历史回答不是事实来源；仅用于理解追问。引用必须由本次运行重新取得并校验。",
        "previous_investigations": history,
    }, ensure_ascii=False)


def _react_budget(override: dict[str, Any] | None) -> Budget:
    values = dict(override or {})
    return Budget(**values) if values else Budget()


def _model_call_count(trace) -> int:
    """Count provider requests, not final trace action labels.

    A successful model decision is relabeled ``tool_call`` or ``finish`` after
    parsing, so ``action == "model_decision"`` undercounts calls that produced
    an action.  ``requested_output_tokens`` is set only when a request is about
    to be sent.
    """
    return sum(step.requested_output_tokens is not None for step in trace)


def run_react(task: dict[str, Any], databases: tuple[Path, Path], *,
              injected_failure: dict[str, Any] | None = None,
              budget_override: dict[str, Any] | None = None,
              online: bool = False) -> dict[str, Any]:
    evidence_db, decisions_db = databases
    scope = EvidenceScope(project_id=task["project_id"], dataset_kind=task["dataset_kind"])
    turns = _public_turns(task)
    started = time.perf_counter()
    output = {
        "task_id": task["task_id"], "backend": "react", "turn_count": len(turns),
        "run_id": None,
        "answer": "", "answers": [], "claims": [], "citation_refs": [],
        "retrieved_ids": [], "decision_memories": [], "events": [],
        "gaps": [], "conflicts": [], "termination_reason": None,
        "termination_detail": None, "mode": None, "runtime_error": None,
        "model_calls": 0, "provider_input_tokens": 0, "provider_output_tokens": 0,
        "estimated_tokens": 0, "estimated_usage": True, "tool_calls": 0,
        "injected_failure": injected_failure, "failure_triggered": False,
        "budget_override": budget_override,
        "capability_notes": list(CAPABILITY_NOTES["react"]),
        "trace": [], "online": online,
    }
    previous: list[dict[str, Any]] = []
    observer = None
    try:
        if online:
            guard = _online_guard(evidence_db, decisions_db)
        else:
            guard = OfflineGuard(evidence_db, decisions_db)
        with guard:
            with SkillObserver(injected_failure) as observer:
                store = EvidenceStore(str(evidence_db))
                for turn in turns:
                    agent = ReActAgent(scope=scope, db_path=str(evidence_db),
                                       budget=_react_budget(budget_override))
                    if online:
                        agent._open_request = urllib.request.build_opener(_NoRedirect()).open
                    context = _conversation_context(previous, scope, store)
                    result = agent.run_result(turn, conversation_context=context)
                    output["run_id"] = result.run_id
                    output["answers"].append(result.answer)
                    output["claims"].extend(claim.model_dump(mode="json")
                                            for claim in result.claims)
                    output["gaps"].extend(result.gaps)
                    output["conflicts"].extend(conflict.model_dump(mode="json")
                                               for conflict in result.conflicts)
                    output["decision_memories"].extend(result.decision_memories)
                    output["retrieved_ids"].extend(
                        identifier for step in result.trace
                        if step.action == "tool_attempt" for identifier in step.evidence_ids)
                    output["termination_reason"] = result.termination_reason
                    output["termination_detail"] = result.termination_detail
                    output["mode"] = result.mode
                    output["trace"].append({
                        "turn": len(previous) + 1,
                        "termination_reason": result.termination_reason,
                        "mode": result.mode,
                        "steps": [step.model_dump(mode="json") for step in result.trace],
                    })
                    if result.mode == "react":
                        output["model_calls"] += _model_call_count(result.trace)
                    output["provider_input_tokens"] += result.usage.input_tokens
                    output["provider_output_tokens"] += result.usage.output_tokens
                    output["estimated_usage"] = output["estimated_usage"] and result.usage.estimated
                    for claim in result.claims:
                        output["citation_refs"].extend(ref.model_dump() for ref in claim.refs)
                    previous.append({"question": turn, "answer": result.answer,
                                     "gaps": result.gaps, "conflicts": output["conflicts"],
                                     "citation_refs": [ref.model_dump() for claim in result.claims
                                                       for ref in claim.refs]})
    except Exception as exc:  # bounded diagnostic, no raw payload
        output["runtime_error"] = type(exc).__name__
    finally:
        if observer is not None:
            # Keep partial tool events even when a backend aborts mid-run.
            output["events"] = list(observer.events)
    output["answer"] = "\n\n".join(output["answers"])
    output["citation_refs"] = _dedupe_refs(output["citation_refs"])
    output["retrieved_ids"] = _dedupe_ids(output["retrieved_ids"])
    output["tool_calls"] = len(output["events"])
    output["failure_triggered"] = any(str(event.get("error", "")).startswith("injected_")
                                      for event in output["events"])
    output["latency_ms"] = (time.perf_counter() - started) * 1000
    output["retrieval"] = _retrieval_summary(output["events"])
    output["estimated_tokens"] = output["provider_input_tokens"] + output["provider_output_tokens"]
    output["claim_source_checks"] = _claim_source_checks(output["claims"], scope, evidence_db)
    return output


class OnlineGuard:
    """Set DB paths for a caller-authorized online run; never blanks credentials."""
    def __init__(self, evidence_db: Path, decisions_db: Path):
        self.evidence_db = evidence_db
        self.decisions_db = decisions_db

    def __enter__(self):
        self._stack = patch.dict(os.environ, {
            "EVIDENCE_DB_PATH": str(self.evidence_db),
            "DECISION_DB_PATH": str(self.decisions_db),
        })
        self._stack.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stack.stop()
        return False


def _online_guard(evidence_db: Path, decisions_db: Path):
    return OnlineGuard(evidence_db, decisions_db)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward an Authorization header to a second origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ModelCallStop(RuntimeError):
    """A hard external cap stopped the current run."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _CapturedResponse:
    """Replay a response body already read for usage accounting."""

    def __init__(self, response, body: bytes):
        self._response = response
        self._body = body

    def read(self, amount: int | None = None) -> bytes:
        if amount is None or amount < 0:
            body, self._body = self._body, b""
            return body
        body, self._body = self._body[:amount], self._body[amount:]
        return body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        return self._response.close()

    def __getattr__(self, name):
        return getattr(self._response, name)


def _request_text(request) -> str:
    data = getattr(request, "data", None)
    if not data:
        return ""
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _usage_tokens(data: Any) -> tuple[int, int, bool]:
    if not isinstance(data, dict):
        return 0, 0, False
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, False
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    try:
        return int(prompt), int(completion), True
    except (TypeError, ValueError):
        return 0, 0, False


class ModelHttpGuard:
    """External call/token caps around one Workflow task.

    ReAct enforces its own budget.  The fixed Workflow has no budget protocol,
    so the online runner installs this guard around *every* provider request:
    it refuses calls past the remaining cap, reads provider usage when present,
    records HTTP statuses, stops immediately on 402 and refuses redirects.
    """

    def __init__(self, *, max_calls: int, max_tokens: int,
                 default_timeout: float = 30.0):
        self.max_calls = max(1, int(max_calls))
        self.max_tokens = max(1, int(max_tokens))
        self.default_timeout = default_timeout
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.statuses: list[int] = []
        self.stop_reason: str | None = None
        self.usage_reported = True
        self._original = None
        self._opener = None

    def __enter__(self):
        self._original = urllib.request.urlopen
        self._opener = urllib.request.build_opener(_NoRedirect())

        def guarded(request, *args, **kwargs):
            return self._call(request, *args, **kwargs)

        urllib.request.urlopen = guarded
        return self

    def __exit__(self, exc_type, exc, tb):
        urllib.request.urlopen = self._original
        return False

    def _call(self, request, *args, **kwargs):
        if self.calls >= self.max_calls:
            self.stop_reason = "max_model_calls"
            raise ModelCallStop("max_model_calls")
        if self.prompt_tokens + self.completion_tokens >= self.max_tokens:
            self.stop_reason = "max_budget_tokens"
            raise ModelCallStop("max_budget_tokens")
        request_text = _request_text(request)
        timeout = kwargs.pop("timeout", None) or self.default_timeout
        self.calls += 1
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            self.statuses.append(int(exc.code))
            if int(exc.code) == 402:
                self.stop_reason = "http_402"
                raise ModelCallStop("http_402", 402) from exc
            raise
        try:
            body = response.read()
        finally:
            response.close()
        parsed = None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            pass
        prompt, completion, reported = _usage_tokens(parsed)
        if not reported:
            self.usage_reported = False
            prompt = _estimate_tokens(request_text)
            completion = _estimate_tokens(body.decode("utf-8", errors="replace"))
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        return _CapturedResponse(response, body)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "model_calls": self.calls,
            "provider_input_tokens": self.prompt_tokens,
            "provider_output_tokens": self.completion_tokens,
            "usage_reported_by_provider": self.usage_reported,
            "http_statuses": list(self.statuses),
            "stop_reason": self.stop_reason,
            "caps": {"max_model_calls": self.max_calls,
                     "max_budget_tokens": self.max_tokens},
        }


def _claim_source_checks(claims: list[dict[str, Any]], scope: EvidenceScope,
                         evidence_db: Path) -> list[dict[str, Any]]:
    store = EvidenceStore(str(evidence_db))
    checks = []
    for index, claim in enumerate(claims):
        refs = claim.get("refs", [])
        resolved = []
        for ref in refs:
            record = store.get(ref["evidence_id"], scope, ref["content_hash"])
            resolved.append(record)
        exact = [record for record in resolved if record is not None
                 and claim.get("text", "") and claim["text"] in record.content]
        checks.append({
            "claim_index": index,
            "ref_count": len(refs),
            "all_refs_resolve": bool(refs) and all(record is not None for record in resolved),
            "source_text_exact_in_cited_evidence": bool(exact),
            "verification_status": claim.get("verification_status"),
        })
    return checks


def run_workflow(task: dict[str, Any], databases: tuple[Path, Path], *,
                 injected_failure: dict[str, Any] | None = None,
                 budget_override: dict[str, Any] | None = None,
                 online: bool = False,
                 online_limits: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence_db, decisions_db = databases
    scope = EvidenceScope(project_id=task["project_id"], dataset_kind=task["dataset_kind"])
    turns = _public_turns(task)
    started = time.perf_counter()
    output = {
        "task_id": task["task_id"], "backend": "workflow", "turn_count": len(turns),
        "answer": "", "answers": [], "claims": [], "citation_refs": [],
        "retrieved_ids": [], "decision_memories": [], "events": [],
        "gaps": [], "conflicts": [], "termination_reason": "not_reported_by_workflow",
        "termination_detail": None,
        "mode": "online_model" if online else "offline_fallback", "runtime_error": None,
        "model_calls": 0, "provider_input_tokens": 0, "provider_output_tokens": 0,
        "estimated_tokens": 0, "estimated_usage": True, "tool_calls": 0,
        "injected_failure": injected_failure, "failure_triggered": False,
        "budget_override": budget_override,
        "budget_supported": False,
        "capability_notes": list(CAPABILITY_NOTES["workflow"]),
        "online_capabilities": (ONLINE_CAPABILITY_NOTES["workflow"] if online else None),
        "model_error_http_statuses": [],
        "external_caps": dict(online_limits or {}) if online else None,
        "trace": [], "online": online,
    }
    if budget_override:
        output["capability_notes"].append(
            "budget_override ignored: Workflow has no native budget protocol; "
            "the online runner enforces external call/token caps")
    observer = None
    capture = None
    call_guard = None
    try:
        if online:
            guard = _online_guard(evidence_db, decisions_db)
        else:
            guard = OfflineGuard(evidence_db, decisions_db)
        limits = online_limits or {}
        call_guard_context = (ModelHttpGuard(
            max_calls=int(limits.get("max_model_calls") or 1),
            max_tokens=int(limits.get("max_budget_tokens") or 1),
            default_timeout=float(limits.get("timeout_seconds") or 30.0))
            if online else nullcontext())
        with guard:
            with call_guard_context as call_guard:
                with SkillObserver(injected_failure) as observer, _ComposeCapture() as capture:
                    agent = CoordinatorAgent(scope=scope, db_path=str(evidence_db),
                                             use_llm_routing=online,
                                             enable_conversation_memory=len(turns) > 1)
                    for turn in turns:
                        output["answers"].append(agent.ask(turn))
    except Exception as exc:
        output["runtime_error"] = type(exc).__name__
    finally:
        if observer is not None:
            output["events"] = list(observer.events)
        if capture is not None:
            output["decision_memories"] = list(capture.memories)
            output["retrieved_ids"].extend(
                hit["evidence_id"] for hit in capture.captured)
        if observer is not None:
            output["retrieved_ids"].extend(
                identifier for event in observer.events
                for identifier in event.get("evidence_ids", []))
        if call_guard is not None:
            diagnostics = call_guard.diagnostics()
            output["model_calls"] = diagnostics["model_calls"]
            output["provider_input_tokens"] = diagnostics["provider_input_tokens"]
            output["provider_output_tokens"] = diagnostics["provider_output_tokens"]
            output["estimated_usage"] = not diagnostics["usage_reported_by_provider"]
            output["model_error_http_statuses"] = diagnostics["http_statuses"]
            output["external_cap_diagnostics"] = diagnostics
            if diagnostics["stop_reason"]:
                output["termination_reason"] = diagnostics["stop_reason"]
    output["answer"] = "\n\n".join(output["answers"])
    output["citation_refs"] = _dedupe_refs([
        {"evidence_id": identifier, "content_hash": digest}
        for identifier, digest in CITATION_RE.findall(output["answer"])])
    output["retrieved_ids"] = _dedupe_ids(output["retrieved_ids"])
    output["tool_calls"] = len(output["events"])
    output["failure_triggered"] = any(str(event.get("error", "")).startswith("injected_")
                                      for event in output["events"])
    output["latency_ms"] = (time.perf_counter() - started) * 1000
    output["retrieval"] = _retrieval_summary(output["events"])
    output["estimated_tokens"] = output["provider_input_tokens"] + output["provider_output_tokens"]
    output["claim_source_checks"] = []
    return output


def run_task(snapshot: Snapshot, task: dict[str, Any], backend: str,
             databases: tuple[Path, Path], *, injected_failure: dict[str, Any] | None = None,
             budget_override: dict[str, Any] | None = None,
             online: bool = False, online_limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one public task against a fresh snapshot clone.

    Failure and budget fixtures are explicit execution configuration; Gold
    facts, evidence groups and expected answers never enter this call.
    """
    runner = run_react if backend == "react" else run_workflow
    return runner(task, databases, injected_failure=injected_failure,
                  budget_override=budget_override, online=online,
                  **({"online_limits": online_limits} if backend == "workflow" else {}))
