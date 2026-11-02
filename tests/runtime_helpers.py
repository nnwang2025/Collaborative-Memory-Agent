"""Shared fakes for Phase 2 runtime tests (offline, no provider calls)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore
from src.investigation import (
    Claim, EvidenceRef, InvestigationResult, Synthesis, TokenUsage, TraceStep,
)


def make_evidence(db, scope: EvidenceScope, content="runtime evidence", record_id="record-1"):
    record = Evidence.create(
        project_id=scope.project_id,
        dataset_kind=scope.dataset_kind,
        source_type="document",
        source_uri="fixture://runtime/record-1",
        original_record_id=record_id,
        content=content,
        ingested_at=datetime.now(timezone.utc),
    )
    EvidenceStore(db).append([record])
    return record


def make_result(scope, *, reason="sufficient", answer="runtime answer",
                refs=(), gaps=None, conflicts=None, trace=None,
                decision_memories=None, usage=None):
    evidence_refs = [EvidenceRef(evidence_id=item.evidence_id,
                                 content_hash=item.content_hash)
                     for item in refs]
    if trace is None:
        if evidence_refs:
            trace = [
                TraceStep(step_index=1, action="tool_attempt", tool_name="search_docs",
                          query="q", evidence_ids=[ref.evidence_id for ref in evidence_refs],
                          retrieval_diagnostics={
                              "effective_mode": "hybrid", "index_version": "index-1",
                              "source_revision": 1}),
                TraceStep(step_index=2, action="terminate",
                          termination_reason=reason),
            ]
        else:
            trace = [TraceStep(step_index=1, action="terminate",
                               termination_reason=reason)]
    return InvestigationResult(
        run_id="internal-result-run",
        scope=scope,
        answer=answer,
        termination_reason=reason,
        claims=[Claim(text="verbatim", refs=evidence_refs)] if evidence_refs else [],
        evidence_refs=evidence_refs,
        gaps=list(gaps or []),
        conflicts=list(conflicts or []),
        trace=list(trace),
        usage=usage or TokenUsage(input_tokens=1, output_tokens=1, estimated=False),
        decision_memories=list(decision_memories or []),
        synthesis=Synthesis(question="q"),
    )


class FakeAgent:
    """Scripted agent with observer/cancel hooks; never performs network I/O."""

    model = "fake-runtime-model"
    base_url = "https://fake.invalid"

    def __init__(self, scope, observer, cancel_check, budget, behaviour):
        self.scope = scope
        self.observer = observer
        self.cancel_check = cancel_check
        self.budget = budget
        self.behaviour = behaviour
        self.contexts = []

    def run_result(self, question, conversation_context=None):
        self.contexts.append(conversation_context)
        return self.behaviour(self, question, conversation_context)


def agent_factory(behaviour):
    def factory(*, scope, observer, cancel_check, budget):
        return FakeAgent(scope, observer, cancel_check, budget, behaviour)
    return factory


def simple_behaviour(*, reason="sufficient", delay=0.0, answer="runtime answer",
                     refs_factory=None):
    def behaviour(agent, question, context):
        if delay:
            time.sleep(delay)
        if agent.cancel_check():
            from src.runtime.errors import RunCancelled
            raise RunCancelled()
        refs = refs_factory() if refs_factory else []
        if refs:
            agent.observer.emit("evidence_observed", {
                "tool_name": "search_docs",
                "refs": [{"evidence_id": ref.evidence_id,
                          "content_hash": ref.content_hash} for ref in refs],
                "retrieval_mode": "hybrid",
                "index_version": "index-1",
                "source_revision": 1,
            })
        agent.observer.emit("model_requested", {"round": 1, "model": agent.model})
        agent.observer.emit("model_completed", {
            "round": 1, "status": "ok",
            "usage": {"input_tokens": 1, "output_tokens": 1, "estimated": False}})
        return make_result(agent.scope, reason=reason, answer=answer, refs=refs,
                           gaps=[] if reason == "sufficient" else ["not complete"])
    return behaviour


def wait_status(service, run_id, statuses, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = service.store.get_run(run_id)
        if row and row["status"] in statuses:
            return row
        time.sleep(0.01)
    return service.store.get_run(run_id)


def event_types(service, run_id, project_id="runtime-project"):
    return [event["event_type"] for event in
            service.events(run_id, project_id=project_id, limit=500)]
