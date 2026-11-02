"""Validated ReAct protocol and request-local, observable investigation state."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.evidence import Evidence, EvidenceScope, legacy_evidence

ToolName = Literal["search_memory", "search_issues", "search_docs", "analyze_dependency", "find_entrypoint"]
TOOLS = ("search_memory", "search_issues", "search_docs", "analyze_dependency", "find_entrypoint")
Reason = Literal["sufficient", "insufficient", "conflict", "budget", "tool_failure"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Filters(StrictModel):
    source_types: list[Literal["git_commit", "issue", "document", "meeting", "chat", "log", "code", "memory", "unknown"]] = Field(default_factory=list, max_length=8)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    after: str | None = Field(default=None, max_length=64)
    before: str | None = Field(default=None, max_length=64)
    decision_id: str | None = Field(default=None, pattern=r"^dec_[0-9a-f]{32}$")
    memory_view: Literal["current", "history"] = "current"
    known_at: str | None = Field(default=None, max_length=64)
    undated_only: bool = False

    @model_validator(mode="after")
    def validate_dates_and_ids(self):
        if self.undated_only and (self.after or self.before):
            raise ValueError("undated_only cannot assert a date range")
        if self.known_at:
            date = datetime.fromisoformat(self.known_at.replace("Z", "+00:00"))
            if date.utcoffset() is None:
                raise ValueError("known_at requires timezone")
        for value in self.evidence_ids:
            if not re.fullmatch(r"ev_[0-9a-f]{64}", value):
                raise ValueError("Invalid evidence ID")
        dates = []
        for value in (self.after, self.before):
            date = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
            if date and date.utcoffset() is None:
                raise ValueError("Filter dates require timezone")
            dates.append(date)
        if all(dates) and dates[0] > dates[1]:
            raise ValueError("after is later than before")
        return self


class EvidenceRef(StrictModel):
    evidence_id: str = Field(pattern=r"^ev_[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=1500)
    kind: Literal["historical", "current", "inference", "unknown"] = "historical"
    refs: list[EvidenceRef] = Field(default_factory=list, max_length=10)
    # Runtime annotation only. compose_investigation overwrites any submitted value.
    verification_status: Literal["supported_extract", "pending_unattributable_conflict"] = "supported_extract"


ConflictRelation = Literal["unattributable", "independent_of_submitted_claims", "affects_submitted_claim"]


class Conflict(StrictModel):
    description: str = Field(min_length=1, max_length=500)
    refs: list[EvidenceRef] = Field(min_length=2, max_length=10)
    # Attribution evidence is runtime-derived, never trusted from model input.
    origin: Literal["evidence_version", "structured_assertion", "model_reported"] = "model_reported"
    fields: list[str] = Field(default_factory=list, max_length=20)
    subject: str | None = Field(default=None, max_length=200)
    relation: ConflictRelation = "unattributable"
    affected_claim_indexes: list[int] = Field(default_factory=list, max_length=20)


class ToolAction(StrictModel):
    action: Literal["tool_call"] = "tool_call"
    tool: ToolName
    query: str = Field(min_length=1, max_length=2000)
    filters: Filters = Field(default_factory=Filters)

    @model_validator(mode="after")
    def nonblank_query(self):
        if not self.query.strip():
            raise ValueError("Query cannot be blank")
        return self


class FinishAction(StrictModel):
    action: Literal["finish"] = "finish"
    claims: list[Claim] = Field(default_factory=list, max_length=20)
    gaps: list[str] = Field(default_factory=list, max_length=20)
    conflicts: list[Conflict] = Field(default_factory=list, max_length=10)
    possible_sources: list[ToolName] = Field(default_factory=list, max_length=5)
    requires_current_status: bool = False
    # Compatibility only. Free-form summaries are NEVER treated as verified claims.
    summary: str = Field(default="", max_length=3000)

    @model_validator(mode="after")
    def bounded_gaps(self):
        if any(not g.strip() or len(g) > 500 for g in self.gaps):
            raise ValueError("Invalid gap")
        return self


def parse_action(text: str) -> ToolAction | FinishAction:
    if not isinstance(text, str) or len(text) > 32000:
        raise ValueError("Invalid action size/type")
    clean = text.strip()
    if clean.startswith("```") and clean.endswith("```"):
        clean = "\n".join(clean.splitlines()[1:-1])
    value = json.loads(clean)
    if not isinstance(value, dict):
        raise ValueError("Action must be an object")
    if value.get("action") == "answer":
        value["action"] = "finish"
    if value.get("action") == "tool":
        value["action"] = "tool_call"
    if value.get("action") == "tool_call":
        return ToolAction.model_validate(value)
    if value.get("action") == "finish":
        return FinishAction.model_validate(value)
    raise ValueError("Unknown action")


class Budget(StrictModel):
    max_model_rounds: int = Field(default=5, ge=1, le=100)
    max_tool_calls: int = Field(default=8, ge=1, le=100)
    max_tokens: int = Field(default=64000, ge=1, le=1000000)
    max_output_tokens: int = Field(default=2048, ge=128, le=16384)
    # One immediate retry may use this larger budget after a verified
    # finish_reason=length/max_tokens. It is not applied to ordinary calls.
    max_truncation_retry_output_tokens: int = Field(default=4096, ge=128, le=16384)
    max_protocol_retries: int = Field(default=2, ge=0, le=5)
    max_time_relaxations: int = Field(default=1, ge=0, le=3)
    # Auxiliary byte ceiling retained from Phase 2.
    max_context_bytes: int = Field(default=48000, ge=1, le=1000000)
    # Phase 3 model-aware context ceiling. None resolves from the model name.
    context_window_tokens: int | None = Field(default=None, ge=1024, le=2000000)
    context_safety_margin_tokens: int = Field(default=512, ge=0, le=100000)
    context_tokenizer: Literal["estimate", "tiktoken"] = "estimate"
    context_max_excerpts: int = Field(default=8, ge=1, le=40)
    context_max_excerpts_per_evidence: int = Field(default=1, ge=1, le=8)
    deadline_seconds: float = Field(default=120.0, gt=0, le=3600)
    model_timeout: float = Field(default=30.0, gt=0, le=300)
    tool_timeout: float = Field(default=10.0, gt=0, le=300)
    max_retries: int = Field(default=1, ge=0, le=3)


class ToolError(StrictModel):
    error_code: str
    retryable: bool
    message: str
    tool_name: str
    elapsed_ms: float
    # Safe classification only: never request bodies, headers, credentials or
    # raw provider response text.
    diagnostic: dict = Field(default_factory=dict)


class TokenUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated: bool = True


class ModelReply(StrictModel):
    text: str
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    requested_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class TraceStep(StrictModel):
    step_index: int
    action: str
    tool_name: str | None = None
    query: str | None = None
    filters: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    errors: list[ToolError] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    new_evidence: bool = False
    termination_reason: Reason | None = None
    finish_reason: str | None = None  # provider stop metadata, never inferred from token count
    requested_output_tokens: int | None = None
    response_chars: int | None = None
    reasoning_tokens: int | None = None
    protocol_category: str | None = None
    validation_issues: list[dict] = Field(default_factory=list)
    recovery_kind: str | None = None
    context_usage: dict = Field(default_factory=dict)
    # Compact audit of evidence kept/dropped for this request; no excerpt bodies.
    context_selection: dict = Field(default_factory=dict)
    # Empty-result/fallback diagnostics from live retrieval; not Evidence.
    retrieval_diagnostics: dict = Field(default_factory=dict)
    # Full scoped evidence snapshot on the terminal trace step; the user-facing
    # answer only cites relevant items.
    evidence_records: list[dict] = Field(default_factory=list)


def protocol_diagnostic(error=None, *, finish_reason=None):
    """Classify without echoing input, provider text, validation context or secrets."""
    issues = []
    if isinstance(error, ValidationError):
        known = {"action", "tool", "query", "filters", "source_types", "evidence_ids", "after", "before",
                 "decision_id", "memory_view", "known_at", "undated_only", "claims", "text", "kind", "refs", "evidence_id",
                 "content_hash", "gaps", "conflicts", "description", "possible_sources", "requires_current_status", "summary"}
        for item in error.errors(include_input=False, include_context=False, include_url=False)[:5]:
            path = [str(part) if isinstance(part, int) or part in known else "<extra_field>" for part in item["loc"]]
            issues.append({"path": ".".join(path), "type": item["type"]})
    if finish_reason in {"length", "max_tokens"}:
        category = "output_truncated"
    elif finish_reason not in {None, "stop"}:
        category = "unexpected_finish_reason"
    elif isinstance(error, json.JSONDecodeError):
        category = "empty_output" if not error.doc.strip() else "json_extra_data" if error.msg == "Extra data" else "json_syntax"
    elif isinstance(error, ValidationError):
        category = "invalid_filter" if any(e["loc"] and e["loc"][0] == "filters" for e in error.errors()) else "schema_mismatch"
    else:
        category = "schema_mismatch"
    return {"category": category, "validation_issues": issues}


def protocol_feedback(diagnostic):
    """Small, actionable repair feedback; never append the rejected raw model reply."""
    instruction = "Return exactly one complete JSON object. No explanation, trailing text, or partial continuation. Use only the short verbatim claims needed to cover the user's task, or claims=[] with concise gaps. Do not omit required sources to reduce claim count. Omit unused optional fields. Preserve full observed evidence_id/content_hash and scope; never invent references."
    if diagnostic.get("category") == "output_truncated":
        instruction = "The previous reply reached the output limit. Return one compact, complete JSON object now: no explanation, no trailing text, short verbatim claims only, and omit optional fields. The same protocol validation still applies."
    return {"error_code": "protocol_error", **diagnostic,
        "instruction": instruction,
        "allowed_filters": {"source_types": ["git_commit", "issue", "document", "meeting", "chat", "log", "code", "memory", "unknown"],
            "evidence_ids": "observed ev_ IDs", "after": "ISO8601 with timezone", "before": "ISO8601 with timezone",
            "decision_id": "observed dec_ ID", "memory_view": ["current", "history"], "known_at": "ISO8601 with timezone",
            "undated_only": "boolean; only event_time=null, incompatible with after/before"},
        "minimal_finish": {"action": "finish", "claims": [], "gaps": ["Evidence is insufficient."]}}


def _shared_subject(left, right):
    """Only return a subject both records explicitly carry and agree on."""
    for key in ("subject", "component", "service", "feature", "topic"):
        first = left.metadata.get(key)
        second = right.metadata.get(key)
        if isinstance(first, str) and isinstance(second, str):
            if first.strip() and first.strip() == second.strip():
                return first.strip()[:200]
    return None


def structured_assertion_conflicts(entries):
    """Find only machine-readable assertion contradictions on the same event day.

    This is deliberately narrower than semantic entailment: prose is never judged.
    Unknown timestamps do not participate, because "same day" cannot be inferred.
    """
    items = []
    for entry in entries:
        item = entry.evidence if hasattr(entry, "evidence") else entry
        items.append(item if hasattr(item, "metadata") else Evidence.model_validate(item))
    conflicts = []
    for index, left in enumerate(items):
        left_assertions = left.metadata.get("assertions")
        if not isinstance(left_assertions, dict) or left.event_time is None:
            continue
        for right in items[index + 1:]:
            right_assertions = right.metadata.get("assertions")
            if (not isinstance(right_assertions, dict) or right.event_time is None
                    or left.evidence_id == right.evidence_id
                    or left.event_time.date() != right.event_time.date()):
                continue
            left_env = left_assertions.get("environment")
            right_env = right_assertions.get("environment")
            if left_env and right_env and left_env != right_env:
                continue
            differing = sorted(
                key for key in set(left_assertions) & set(right_assertions)
                if key not in {"environment", "env", "project_id", "dataset_kind"}
                and isinstance(left_assertions[key], (str, int, float, bool))
                and isinstance(right_assertions[key], (str, int, float, bool))
                and left_assertions[key] != right_assertions[key]
            )
            if differing:
                conflicts.append(Conflict(
                    description="结构化断言在同一事件日不一致：" + ", ".join(differing) + "；需人工核对，系统不择一裁决。",
                    refs=[EvidenceRef(evidence_id=left.evidence_id, content_hash=left.content_hash),
                          EvidenceRef(evidence_id=right.evidence_id, content_hash=right.content_hash)],
                    origin="structured_assertion", fields=differing[:20],
                    subject=_shared_subject(left, right)))
    return conflicts


class LedgerEntry(StrictModel):
    evidence: Evidence
    tools: list[str]
    order: int


class EvidenceLedger:
    """No global state or writes; only validated actual tool results enter this ledger."""

    def __init__(self, scope, run_id=None):
        self.run_id = run_id or str(uuid4())
        self.scope = EvidenceScope.model_validate(scope)
        self._entries = {}
        self.duplicate_observations = 0
        self.gaps = ["尚未取得足以支持结论的证据。"]
        self.conflicts = []

    @property
    def entries(self):
        # Defensive reconstruction: callers cannot mutate the original snapshot.
        return [LedgerEntry.model_validate(e.model_dump()) for e in self._entries.values()]

    def register(self, items, tool):
        pending = []
        for item in items:
            evidence = legacy_evidence(item)
            if (evidence.project_id, evidence.dataset_kind) != (self.scope.project_id, self.scope.dataset_kind):
                raise ValueError("Evidence scope mismatch")
            if "excerpt" in item:
                from src.investigation_context import Excerpt
                Excerpt.model_validate(item["excerpt"]).verify(evidence)
            pending.append(evidence)
        added = []
        for evidence in pending:
            key = (evidence.evidence_id, evidence.content_hash)
            if key in self._entries:
                self.duplicate_observations += 1
                if tool not in self._entries[key].tools:
                    self._entries[key].tools.append(tool)
                continue
            previous = [k for k in self._entries if k[0] == evidence.evidence_id and k[1] != evidence.content_hash]
            self._entries[key] = LedgerEntry(evidence=evidence, tools=[tool], order=len(self._entries) + 1)
            added.append(EvidenceRef(evidence_id=key[0], content_hash=key[1]))
            if previous:
                self.conflicts.append(Conflict(description="同一证据出现多个内容版本；差异尚未核实。",
                    refs=[EvidenceRef(evidence_id=previous[0][0], content_hash=previous[0][1]), added[-1]],
                    origin="evidence_version"))
        known = {tuple(sorted((r.evidence_id, r.content_hash) for r in conflict.refs))
                 for conflict in self.conflicts}
        for conflict in structured_assertion_conflicts(self._entries.values()):
            signature = tuple(sorted((r.evidence_id, r.content_hash) for r in conflict.refs))
            if signature not in known:
                self.conflicts.append(conflict)
                known.add(signature)
        if added:
            self.gaps = ["证据已取得，问题覆盖与语义支持仍待核实。"]
        return added

    def resolve(self, ref, getter):
        entry = self._entries.get((ref.evidence_id, ref.content_hash))
        if entry is None:
            raise ValueError("Reference is not in this run's ledger")
        fetched = getter(ref.evidence_id, ref.content_hash)
        if fetched is None:
            raise ValueError("Reference cannot be fetched")
        record = Evidence.model_validate(fetched.model_dump() if isinstance(fetched, Evidence) else fetched)
        if record.model_dump() != entry.evidence.model_dump():
            raise ValueError("Reference snapshot changed")
        if (record.project_id, record.dataset_kind) != (self.scope.project_id, self.scope.dataset_kind):
            raise ValueError("Reference scope changed")
        return record

    def validate_all(self, getter):
        rejected = []
        for key in list(self._entries):
            try:
                self.resolve(EvidenceRef(evidence_id=key[0], content_hash=key[1]), getter)
            except Exception:
                rejected.append(key)
                del self._entries[key]
        self.conflicts = [c for c in self.conflicts if all((r.evidence_id, r.content_hash) in self._entries for r in c.refs)]
        return rejected


class Observation(StrictModel):
    tool_name: str
    new_evidence: list[EvidenceRef] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)  # bounded model views only
    decision_memories: list[dict] = Field(default_factory=list)
    error: ToolError | None = None
    gaps: list[str] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    possible_sources: list[ToolName] = Field(default_factory=lambda: list(TOOLS))
    time_relaxation: dict | None = None
    retrieval_diagnostics: dict = Field(default_factory=dict)


class SynthesisStatement(StrictModel):
    """Deterministic prose assembled only from validated claim extracts."""
    text: str = Field(min_length=1, max_length=4000)
    claim_indexes: list[int] = Field(default_factory=list, max_length=20)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    source_types: list[str] = Field(default_factory=list, max_length=10)
    basis: Literal["validated_claim", "validated_cross_source",
                   "validated_conflict_disclosure",
                   "validated_decision_snapshot"] = "validated_claim"


class Synthesis(StrictModel):
    """User-facing synthesis layer; original Evidence and claims remain separate."""
    question: str = Field(default="", max_length=4000)
    statements: list[SynthesisStatement] = Field(default_factory=list, max_length=50)
    unconfirmed: list[str] = Field(default_factory=list, max_length=20)
    caveats: list[str] = Field(default_factory=list, max_length=20)
    conflict_count: int = Field(default=0, ge=0)


class InvestigationResult(StrictModel):
    run_id: str
    scope: EvidenceScope
    answer: str
    termination_reason: Reason
    termination_detail: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    synthesis: Synthesis | None = None
    # Runtime counters, filled only from an actual finish/composition path.
    claim_counts: dict = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    possible_sources: list[ToolName] = Field(default_factory=lambda: list(TOOLS))
    trace: list[TraceStep] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    mode: Literal["react", "offline_fallback"] = "react"
    validation_level: Literal["identity_and_extract_only"] = "identity_and_extract_only"
    decision_memories: list[dict] = Field(default_factory=list)
    task_coverage: dict = Field(default_factory=dict)
    context_usage: dict = Field(default_factory=dict)
    # Per-model-round context statistics; no prompt, excerpt body or secret.
    context_trace: list[dict] = Field(default_factory=list)

    def export_jsonl(self, trusted_path):
        """Opt-in caller-controlled export. Never exports full evidence or model replies."""
        path = Path(trusted_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            for step in self.trace:
                # Queries and filters are private by default, even in opt-in export.
                payload = step.model_dump(exclude={"query", "filters", "evidence_records"})
                # Keep the trace self-contained without exporting private evidence bodies.
                payload["evidence_records"] = [
                    {key: record.get(key) for key in
                     ("evidence_id", "content_hash", "source_type", "source_uri",
                      "original_record_id", "event_time", "dataset_kind")}
                    for record in step.evidence_records]
                stream.write(json.dumps({"run_id": self.run_id, "scope": self.scope.model_dump(),
                                         **payload}, ensure_ascii=False) + "\n")
