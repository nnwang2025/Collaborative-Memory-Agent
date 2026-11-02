"""Bridge Agent lifecycle callbacks into the append-only runtime stores.

The observer receives only small, already-computed payloads.  It never sees the
model request messages, provider response body, credentials or full Evidence
content, so none of those can reach ``run_events`` or ``run_tool_calls``.
"""

from __future__ import annotations

import json
import logging

from src.runtime.store import RuntimeStore, sanitize, utcnow

logger = logging.getLogger("cma.runtime.observer")


class RunObserver:
    def __init__(self, store: RuntimeStore, run_id: str, session_id: str | None = None):
        self.store = store
        self.run_id = run_id
        self.session_id = session_id
        self.errors = 0
        self.tool_names = {}

    def _log(self, event_type: str, payload: dict) -> None:
        error = payload.get("error") or {}
        if (event_type in {"tool_failed", "retry_scheduled", "conflict_detected"}
                or (event_type == "model_completed" and payload.get("status") == "error")):
            error_code = (error.get("error_code") or payload.get("reason")
                          or error.get("provider_error_code"))
            if event_type == "model_completed":
                category = ("model_timeout" if error.get("transport_reason") == "timeout"
                            else "model_error")
            elif event_type == "conflict_detected":
                category = "conflict"
            elif "retrieval" in str(error_code or "").lower():
                category = "retrieval_error"
            else:
                category = "tool_error"
            record = sanitize({
                "event_type": event_type,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "error_category": category,
                "error_code": error_code,
                "diagnostic_phase": error.get("phase"),
                "tool_name": payload.get("tool_name"),
                "kind": payload.get("kind"),
            })
            logger.warning("agent_event %s", json.dumps(record, ensure_ascii=False,
                                                        sort_keys=True))

    def emit(self, event_type: str, payload=None) -> None:
        payload = dict(payload or {})
        try:
            self._log(event_type, payload)
            if event_type == "tool_started":
                self.tool_names[payload.get("tool_call_id")] = payload.get("tool_name")
                self.store.start_tool_call(
                    tool_call_id=payload.get("tool_call_id"),
                    run_id=self.run_id,
                    tool_name=str(payload.get("tool_name") or "unknown"),
                    query=str(payload.get("query") or ""),
                    filters=payload.get("filters") or {},
                    attempt=int(payload.get("attempt") or 0),
                    trace_step_index=payload.get("trace_step_index"),
                )
            elif event_type in {"tool_completed", "tool_failed"}:
                error = payload.get("error") or {}
                self.store.finish_tool_call(
                    payload.get("tool_call_id"),
                    run_id=self.run_id,
                    status="completed" if event_type == "tool_completed" else "failed",
                    elapsed_ms=payload.get("elapsed_ms"),
                    error_code=error.get("error_code"),
                    error_message=error.get("message"),
                    retryable=error.get("retryable"),
                    result_summary=payload.get("result_summary") or {},
                    trace_step_index=payload.get("trace_step_index"),
                )
            elif event_type == "evidence_observed":
                for ref in payload.get("refs") or []:
                    self.store.record_evidence_ref(
                        self.run_id,
                        str(ref.get("evidence_id") or ""),
                        str(ref.get("content_hash") or ""),
                        tool_name=payload.get("tool_name"),
                        metadata={
                            "retrieval_mode": payload.get("retrieval_mode"),
                            "index_version": payload.get("index_version"),
                            "source_revision": payload.get("source_revision"),
                            "fallback_reason": payload.get("fallback_reason"),
                        },
                    )
            self.store.append_event(self.run_id, event_type, sanitize(payload))
        except Exception:
            # Observation must never change investigation semantics.  A failed
            # event write is counted for diagnostics, not raised into the Agent.
            self.errors += 1

    def snapshot(self) -> dict:
        return {"observer_errors": self.errors, "observed_at": utcnow()}
