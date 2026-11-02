"""Session/Run REST and SSE endpoints.

Single-user local deployment boundary: there is no multi-tenant authentication.
Run-specific endpoints nevertheless require the owning ``project_id`` so one
project's identifiers cannot silently read another project's run.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from src.evidence import EvidenceScope
from src.runtime.errors import (
    IdempotencyConflict, RunNotFound, ScopeMismatch, SessionBusy, SessionClosed,
    SessionNotFound,
)
from src.runtime.service import RunService
from src.runtime.store import TERMINAL_STATUSES, sanitize


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreate(StrictBody):
    project_id: str = Field(min_length=1, max_length=200)
    dataset_kind: Literal["fixture", "live"] = "live"


class RunCreate(StrictBody):
    session_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    idempotency_key: str | None = Field(default=None, max_length=200)
    budget: dict | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    project_id: str | None = Field(default=None, max_length=200)
    dataset_kind: Literal["fixture", "live"] | None = None


def _detail(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _runtime(request: Request) -> RunService:
    service = request.app.state.runtime_service
    if service is not None:
        return service
    lock = request.app.state.runtime_lock
    with lock:
        if request.app.state.runtime_service is None:
            request.app.state.runtime_service = RunService(
                config=request.app.state.runtime_config)
        return request.app.state.runtime_service


def _session_public(row):
    return sanitize(dict(row))


def _raise_known(exc):
    if isinstance(exc, (SessionNotFound, RunNotFound)):
        raise HTTPException(status_code=404, detail=_detail(exc.code, str(exc)))
    if isinstance(exc, (IdempotencyConflict, SessionBusy, SessionClosed, ScopeMismatch)):
        raise HTTPException(status_code=409, detail=_detail(exc.code, str(exc)))
    raise exc


async def iter_run_stream(runtime: RunService, run_id: str, project_id: str,
                          after_seq: int, is_disconnected,
                          *, heartbeat_seconds=None, poll_seconds=None):
    """Yield persisted SSE frames; closing the iterator never cancels the run."""
    last_seq = int(after_seq)
    heartbeat = max(0.05, float(
        runtime.config.sse_heartbeat_seconds if heartbeat_seconds is None
        else heartbeat_seconds))
    poll = max(0.02, float(
        runtime.config.sse_poll_seconds if poll_seconds is None else poll_seconds))
    last_activity = time.monotonic()
    while True:
        if await is_disconnected():
            break
        events = await run_in_threadpool(
            runtime.events, run_id, after_seq=last_seq,
            limit=runtime.config.max_events_per_page, project_id=project_id)
        for event in events:
            public = runtime.public_event(event)
            if public["event_seq"] <= last_seq:
                continue
            last_seq = public["event_seq"]
            payload = json.dumps(public, ensure_ascii=False)
            yield (f"id: {last_seq}\nevent: {public['event_type']}\n"
                   f"data: {payload}\n\n")
            last_activity = time.monotonic()
        row = await run_in_threadpool(runtime.get_run, run_id, project_id)
        if row["status"] in TERMINAL_STATUSES and not events:
            terminal = {
                "run_id": run_id,
                "status": row["status"],
                "termination_reason": row["termination_reason"],
                "last_event_seq": last_seq,
            }
            yield ("event: stream_end\n"
                   f"data: {json.dumps(terminal, ensure_ascii=False)}\n\n")
            break
        if row["status"] in TERMINAL_STATUSES:
            continue  # drain the last page before closing
        now = time.monotonic()
        if now - last_activity >= heartbeat:
            yield ": heartbeat\n\n"
            last_activity = now
        await asyncio.sleep(poll)


def build_runtime_router() -> APIRouter:
    router = APIRouter()

    @router.post("/sessions", status_code=201)
    def create_session(body: SessionCreate, request: Request):
        try:
            EvidenceScope(project_id=body.project_id, dataset_kind=body.dataset_kind)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_detail(
                "invalid_scope", str(exc)))
        session = _runtime(request).sessions.create(
            body.project_id, body.dataset_kind)
        return {"session": _session_public(session)}

    @router.get("/sessions/{session_id}")
    def get_session(
        session_id: str,
        request: Request,
        message_limit: int = Query(default=20, ge=1, le=100),
        run_limit: int = Query(default=20, ge=1, le=100),
    ):
        runtime = _runtime(request)
        try:
            session = runtime.sessions.get(session_id)
            messages = runtime.sessions.messages(
                session_id, limit=message_limit)
            runs = runtime.sessions.runs(session_id, limit=run_limit)
        except SessionNotFound as exc:
            _raise_known(exc)
        return {
            "session": _session_public(session),
            "messages": [runtime.public_message(item) for item in messages],
            "runs": [runtime.public_run(item) for item in runs],
        }

    @router.post("/sessions/{session_id}/close")
    def close_session(session_id: str, request: Request):
        try:
            session = _runtime(request).sessions.close(session_id)
        except SessionNotFound as exc:
            _raise_known(exc)
        return {"session": _session_public(session)}

    @router.get("/sessions/{session_id}/runs")
    def list_session_runs(
        session_id: str,
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        runtime = _runtime(request)
        try:
            runtime.sessions.get(session_id)
            rows = runtime.sessions.runs(session_id, limit=limit, offset=offset)
        except SessionNotFound as exc:
            _raise_known(exc)
        return {"session_id": session_id,
                "runs": [runtime.public_run(item) for item in rows],
                "limit": limit, "offset": offset}

    @router.post("/runs", status_code=202)
    def submit_run(body: RunCreate, request: Request):
        runtime = _runtime(request)
        key = request.headers.get("Idempotency-Key") or body.idempotency_key
        try:
            run, created = runtime.submit(
                body.session_id, body.question,
                idempotency_key=key,
                budget=body.budget,
                timeout_seconds=body.timeout_seconds,
                project_id=body.project_id,
                dataset_kind=body.dataset_kind,
            )
        except (SessionNotFound, RunNotFound, IdempotencyConflict, SessionBusy,
                SessionClosed, ScopeMismatch) as exc:
            _raise_known(exc)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=_detail(
                "invalid_run_request", str(exc)))
        response = {"run": runtime.public_run(run), "created": created}
        if not created:
            response["idempotent_replay"] = True
        return response

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request,
                project_id: str = Query(min_length=1)):
        runtime = _runtime(request)
        try:
            row = runtime.get_run(run_id, project_id=project_id)
        except RunNotFound as exc:
            _raise_known(exc)
        return {"run": runtime.public_run(row)}

    @router.get("/runs/{run_id}/events")
    def get_events(
        run_id: str,
        request: Request,
        project_id: str = Query(min_length=1),
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        runtime = _runtime(request)
        try:
            events = runtime.events(
                run_id, after_seq=after_seq, limit=limit, project_id=project_id)
            row = runtime.get_run(run_id, project_id=project_id)
        except RunNotFound as exc:
            _raise_known(exc)
        public = [runtime.public_event(event) for event in events]
        next_seq = public[-1]["event_seq"] if public else after_seq
        return {"run_id": run_id, "events": public, "next_after_seq": next_seq,
                "run_status": row["status"]}

    @router.get("/runs/{run_id}/result")
    def get_result(run_id: str, request: Request,
                   project_id: str = Query(min_length=1)):
        runtime = _runtime(request)
        try:
            row, stored = runtime.result(run_id, project_id=project_id)
        except RunNotFound as exc:
            _raise_known(exc)
        if row["status"] not in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail=_detail(
                "run_not_terminal", "run has not reached a terminal state"))
        if stored is None:
            return {"run": runtime.public_run(row), "result": None,
                    "answer": None, "result_available": False}
        return {
            "run": runtime.public_run(row),
            "result": stored["result"],
            "answer": stored["answer"],
            "result_available": True,
            "status_at_result": stored["status"],
            "termination_reason": stored["termination_reason"],
            "termination_detail": stored["termination_detail"],
        }

    @router.get("/runs/{run_id}/tool-calls")
    def get_tool_calls(run_id: str, request: Request,
                       project_id: str = Query(min_length=1)):
        runtime = _runtime(request)
        try:
            rows = runtime.tool_calls(run_id, project_id=project_id)
        except RunNotFound as exc:
            _raise_known(exc)
        return {"run_id": run_id,
                "tool_calls": [runtime.public_tool_call(item) for item in rows]}

    @router.get("/runs/{run_id}/evidence-refs")
    def get_evidence_refs(run_id: str, request: Request,
                          project_id: str = Query(min_length=1)):
        runtime = _runtime(request)
        try:
            rows = runtime.evidence_refs(run_id, project_id=project_id)
        except RunNotFound as exc:
            _raise_known(exc)
        return {"run_id": run_id, "evidence_refs": sanitize(rows)}

    @router.get("/runs/{run_id}/context-trace")
    def get_context_trace(run_id: str, request: Request,
                          project_id: str = Query(min_length=1),
                          after_seq: int = Query(default=0, ge=0),
                          limit: int = Query(default=200, ge=1, le=500)):
        """Sanitized per-round context statistics from the persisted event log."""
        runtime = _runtime(request)
        try:
            events = runtime.events(
                run_id, after_seq=after_seq, limit=limit, project_id=project_id)
            row = runtime.get_run(run_id, project_id=project_id)
        except RunNotFound as exc:
            _raise_known(exc)
        trace = [runtime.public_event(event) for event in events
                 if event["event_type"] in {"context_selected", "context_limit"}]
        return {"run_id": run_id, "run_status": row["status"],
                "context_trace": trace,
                "rounds": [event["payload"] for event in trace],
                "unit_note": "token fields are estimates unless tokenizer_mode is exact"}

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request,
                   project_id: str = Query(min_length=1)):
        runtime = _runtime(request)
        try:
            row, changed = runtime.cancel(run_id, project_id=project_id)
        except RunNotFound as exc:
            _raise_known(exc)
        return {"run": runtime.public_run(row), "cancel_requested": changed,
                "terminal": row["status"] in TERMINAL_STATUSES}

    @router.get("/runs/{run_id}/stream")
    async def stream_run(
        run_id: str,
        request: Request,
        project_id: str = Query(min_length=1),
        after_seq: int = Query(default=0, ge=0),
    ):
        runtime = _runtime(request)
        try:
            runtime.get_run(run_id, project_id=project_id)
        except RunNotFound as exc:
            _raise_known(exc)
        header_cursor = request.headers.get("last-event-id")
        if header_cursor:
            try:
                after_seq = max(after_seq, int(header_cursor))
            except ValueError:
                raise HTTPException(status_code=400, detail=_detail(
                    "invalid_cursor", "Last-Event-ID must be an integer"))

        return StreamingResponse(
            iter_run_stream(runtime, run_id, project_id, after_seq,
                            request.is_disconnected),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
