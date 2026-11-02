"""Session and run services over the persistent runtime store.

Execution is deliberately asynchronous with respect to HTTP: a worker thread
owns the synchronous Agent.  Cancellation and timeouts are cooperative
boundaries (the Agent checks before model/tool calls); an in-flight blocking
provider call can continue until it returns, but its late result is discarded.
No checkpoint/resume of a partial investigation is claimed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from src.evidence import EvidenceScope
from src.investigation import Budget
from src.runtime.config import RuntimeConfig
from src.runtime.conversation import ConversationContextBuilder
from src.runtime.errors import (
    InvalidRunTransition, RunCancelled, RunNotFound, ScopeMismatch,
    SessionNotFound,
)
from src.runtime.observer import RunObserver
from src.runtime.store import (
    RuntimeStore, TERMINAL_STATUSES, _json, _loads, sanitize, utcnow,
)

logger = logging.getLogger("cma.runtime.service")

REASON_TO_STATUS = {
    "sufficient": "succeeded",
    "insufficient": "insufficient",
    "conflict": "conflict",
    "tool_failure": "failed",
    "budget": "failed",
}


class SessionService:
    def __init__(self, store: RuntimeStore):
        self.store = store

    def create(self, project_id: str, dataset_kind: str = "live"):
        return self.store.create_session(project_id, dataset_kind)

    def get(self, session_id):
        session = self.store.get_session(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def close(self, session_id):
        session = self.store.close_session(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def messages(self, session_id, *, limit=50, after_seq=0):
        self.get(session_id)
        return self.store.list_session_messages(
            session_id, limit=limit, after_seq=after_seq)

    def runs(self, session_id, *, limit=20, offset=0):
        self.get(session_id)
        return self.store.list_runs(session_id, limit=limit, offset=offset)


class RunService:
    def __init__(self, db_path=None, *, config=None, store=None, agent_factory=None,
                 conversation_builder=None, recover=True):
        self.config = config or RuntimeConfig.from_env()
        self.store = store or RuntimeStore(db_path or self.config.resolved_db_path())
        self.sessions = SessionService(self.store)
        self.conversation = conversation_builder or ConversationContextBuilder(
            self.store,
            max_runs=self.config.session_history_runs,
            max_bytes=self.config.session_history_bytes,
        )
        self.agent_factory = agent_factory or self._default_agent
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(self.config.max_workers)),
            thread_name_prefix="agent-run",
        )
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._started = False
        self._recover = recover
        self._slot = threading.BoundedSemaphore(max(1, int(self.config.max_concurrent_runs)))

    @staticmethod
    def _log(level, event, **fields):
        record = sanitize(fields)
        logger.log(level, "%s %s", event,
                   json.dumps(record, ensure_ascii=False, sort_keys=True))

    # -- lifecycle -------------------------------------------------------
    def _ensure_started(self):
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            if self._recover:
                # Only mark; no idempotent agent step exists, so no rescheduling.
                recovered = self.store.recover_interrupted()
                if recovered:
                    self._log(logging.WARNING, "runs_marked_interrupted",
                              run_ids=recovered, count=len(recovered))
            self._started = True

    def close(self):
        for timer in list(self._timers.values()):
            timer.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _default_agent(self, *, scope, observer, cancel_check, budget):
        from src.agents.react_agent import ReActAgent
        return ReActAgent(scope=scope, db_path=self.store.path, budget=budget,
                          observer=observer, cancel_check=cancel_check)

    # -- submission ------------------------------------------------------
    @staticmethod
    def _request_hash(*, session_id, project_id, dataset_kind, question,
                      retrieval_mode, budget):
        payload = {
            "session_id": session_id,
            "project_id": project_id,
            "dataset_kind": dataset_kind,
            "question": question,
            "retrieval_mode": retrieval_mode,
            "budget": budget,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                              .encode("utf-8")).hexdigest()

    def submit(self, session_id: str, question: str, *, idempotency_key=None,
               retrieval_mode=None, budget=None, timeout_seconds=None,
               project_id=None, dataset_kind=None):
        self._ensure_started()
        question = str(question or "").strip()
        if not question:
            raise ValueError("question is required")
        session = self.store.get_session(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        if project_id is not None and project_id != session["project_id"]:
            raise ScopeMismatch("project_id does not match the session")
        if dataset_kind is not None and dataset_kind != session["dataset_kind"]:
            raise ScopeMismatch("dataset_kind does not match the session")
        budget_values = dict(budget or {})
        if timeout_seconds is not None:
            budget_values["deadline_seconds"] = float(timeout_seconds)
        budget_values.setdefault("deadline_seconds", self.config.run_timeout_seconds)
        run_budget = Budget.model_validate(budget_values)
        request_hash = self._request_hash(
            session_id=session_id, project_id=session["project_id"],
            dataset_kind=session["dataset_kind"], question=question,
            retrieval_mode=retrieval_mode, budget=run_budget.model_dump())
        model_config = {"backend": "react", "retrieval_mode": retrieval_mode}
        run, created = self.store.create_run(
            session_id=session_id,
            project_id=session["project_id"],
            dataset_kind=session["dataset_kind"],
            question=question,
            request_hash=request_hash,
            backend="react",
            model_name=None,
            model_config=model_config,
            budget=run_budget.model_dump(),
            retrieval_mode=retrieval_mode,
            idempotency_key=idempotency_key,
        )
        if not created:
            self._log(logging.INFO, "run_idempotent_replay",
                      run_id=run["run_id"], session_id=session_id,
                      project_id=session["project_id"])
            return run, False
        self._log(logging.INFO, "run_submitted", run_id=run["run_id"],
                  session_id=session_id, project_id=session["project_id"],
                  dataset_kind=session["dataset_kind"], status=run["status"])
        event = threading.Event()
        self._cancel_events[run["run_id"]] = event
        self.store.append_session_message(
            session_id, "user", question, run_id=run["run_id"])
        # A queued run may be cancelled before a worker starts.
        if self.store.cancel_requested(run["run_id"]):
            event.set()
        timer = threading.Timer(
            float(run_budget.deadline_seconds),
            self._on_timeout, args=(run["run_id"], event))
        timer.daemon = True
        self._timers[run["run_id"]] = timer
        timer.start()
        self._executor.submit(
            self._execute, run["run_id"], session_id, question,
            EvidenceScope(project_id=session["project_id"],
                          dataset_kind=session["dataset_kind"]),
            run_budget, retrieval_mode, event)
        return run, True

    # -- execution -------------------------------------------------------
    def _on_timeout(self, run_id, event):
        event.set()
        row = self.store.get_run(run_id)
        if row is None or row["status"] in TERMINAL_STATUSES:
            return
        self.store.finish_run(
            run_id, status="timed_out", termination_reason="timeout",
            termination_detail="run_deadline_exceeded", error_code="timeout",
            error_message="Run deadline expired; the synchronous call was not force-killed.",
        )
        self._log(logging.WARNING, "run_timed_out", run_id=run_id,
                  session_id=row["session_id"], error_code="timeout",
                  termination_detail="run_deadline_exceeded")

    def _execute(self, run_id, session_id, question, scope, budget, retrieval_mode, event):
        acquired = self._slot.acquire(timeout=max(0.1, budget.deadline_seconds))
        if not acquired:
            if self.store.get_run(run_id)["status"] not in TERMINAL_STATUSES:
                self.store.finish_run(
                    run_id, status="timed_out", termination_reason="timeout",
                    termination_detail="concurrency_slot_wait_exceeded",
                    error_code="timeout")
                self._log(logging.WARNING, "run_timed_out", run_id=run_id,
                          session_id=session_id, error_code="timeout",
                          termination_detail="concurrency_slot_wait_exceeded")
            return
        try:
            row = self.store.get_run(run_id)
            if row is None or row["status"] in TERMINAL_STATUSES:
                return
            if event.is_set() or self.store.cancel_requested(run_id):
                self._finish_cooperative_stop(
                    run_id, session_id, event,
                    detail="cancelled_before_start")
                return
            try:
                row = self.store.start_run(run_id)
            except InvalidRunTransition:
                self._log(logging.INFO, "run_start_skipped", run_id=run_id,
                          session_id=session_id, reason="invalid_transition")
                return
            if row is None or row["status"] in TERMINAL_STATUSES:
                return
            self._log(logging.INFO, "run_started", run_id=run_id,
                      session_id=session_id)
            observer = RunObserver(self.store, run_id, session_id)
            context = None
            try:
                context = self.conversation.build(session_id, current_run_id=run_id)
            except Exception as exc:
                self._log(logging.WARNING, "conversation_context_failed",
                          run_id=run_id, session_id=session_id,
                          error_type=type(exc).__name__)
            agent = self.agent_factory(
                scope=scope, observer=observer, cancel_check=event.is_set, budget=budget)
            model_config = self._model_config(agent)
            self.store.update_run_snapshot(
                run_id, model_name=model_config.get("model_name"),
                model_config=model_config, retrieval_mode=retrieval_mode)
            try:
                result = self._invoke_agent(agent, question, context)
            except RunCancelled:
                self._finish_cooperative_stop(
                    run_id, session_id, event,
                    detail="cooperative_cancel_boundary")
                return
            current = self.store.get_run(run_id)
            if current is None or current["status"] in TERMINAL_STATUSES:
                return  # timer or a competing cancel already closed this run
            if event.is_set() or current["cancel_requested"]:
                self._finish_cooperative_stop(
                    run_id, session_id, event,
                    detail="cancelled_during_execution")
                return
            self._persist_result(run_id, result, model_config)
        except Exception as exc:
            current = self.store.get_run(run_id)
            if current is not None and current["status"] not in TERMINAL_STATUSES:
                self.store.finish_run(
                    run_id, status="failed", termination_reason="system_error",
                    termination_detail="unhandled_runtime_error",
                    error_code="system_error",
                    error_message=f"{type(exc).__name__}: {exc}")
                category = ("database_error" if isinstance(exc, sqlite3.Error)
                            else "timeout" if isinstance(exc, TimeoutError)
                            else "runtime_error")
                self._log(logging.ERROR, "run_failed", run_id=run_id,
                          session_id=session_id, error_code="system_error",
                          error_category=category,
                          error_type=type(exc).__name__)
        finally:
            timer = self._timers.pop(run_id, None)
            if timer is not None:
                timer.cancel()
            self._cancel_events.pop(run_id, None)
            self._slot.release()

    def _finish_cooperative_stop(self, run_id, session_id, event, *, detail):
        """Close a run stopped at a cooperative boundary.

        ``cancel_requested`` is written before the cancel event is set, while a
        timeout only sets the event.  That ordering keeps the two terminal
        reasons distinct even when the timer and the worker race.
        """
        row = self.store.get_run(run_id)
        if row is None or row["status"] in TERMINAL_STATUSES:
            return row
        if row["cancel_requested"]:
            self.store.finish_run(
                run_id, status="cancelled", termination_reason="cancelled",
                termination_detail=detail)
            self._log(logging.INFO, "run_cancelled", run_id=run_id,
                      session_id=session_id, termination_detail=detail)
        else:
            self.store.finish_run(
                run_id, status="timed_out", termination_reason="timeout",
                termination_detail="run_deadline_exceeded",
                error_code="timeout",
                error_message="Run deadline expired at a cooperative boundary.")
            self._log(logging.WARNING, "run_timed_out", run_id=run_id,
                      session_id=session_id, error_code="timeout",
                      termination_detail="run_deadline_exceeded")
        return self.store.get_run(run_id)

    @staticmethod
    def _invoke_agent(agent, question, context):
        if context is None:
            return agent.run_result(question)
        parameters = inspect.signature(agent.run_result).parameters
        accepts = ("conversation_context" in parameters
                   or any(item.kind == inspect.Parameter.VAR_KEYWORD
                          for item in parameters.values()))
        if accepts:
            return agent.run_result(question, conversation_context=context)
        return agent.run_result(question)

    @staticmethod
    def _model_config(agent):
        base_url = str(getattr(agent, "base_url", "") or "")
        host = urlsplit(base_url).hostname if base_url else None
        return {
            "backend": "react",
            "model_name": getattr(agent, "model", None),
            "provider_host": host,
        }

    def _persist_result(self, run_id, result, model_config):
        reason = getattr(result, "termination_reason", "insufficient")
        status = REASON_TO_STATUS.get(reason, "failed")
        tool_calls = sum(1 for step in getattr(result, "trace", [])
                         if getattr(step, "action", "") == "tool_attempt")
        retrieval_mode = fallback_reason = index_version = source_revision = None
        for step in reversed(getattr(result, "trace", [])):
            diagnostics = getattr(step, "retrieval_diagnostics", {}) or {}
            if not retrieval_mode:
                retrieval_mode = diagnostics.get("effective_mode")
            if fallback_reason is None:
                fallback_reason = diagnostics.get("fallback_reason")
            if index_version is None:
                index_version = diagnostics.get("index_version")
            if source_revision is None:
                source_revision = diagnostics.get("source_revision")
        usage = getattr(result, "usage", None)
        usage_payload = usage.model_dump() if hasattr(usage, "model_dump") else {}
        result_payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        answer = getattr(result, "answer", "")
        self.store.complete_run(
            run_id, status=status, termination_reason=reason,
            termination_detail=getattr(result, "termination_detail", None),
            result=result_payload, answer=answer,
            tool_call_count=tool_calls, usage=usage_payload,
            retrieval_mode=retrieval_mode, fallback_reason=fallback_reason,
            index_version=index_version,
            source_revision=source_revision, model_config=model_config)
        row = self.store.get_run(run_id)
        self._log(logging.INFO, "run_finished", run_id=run_id,
                  session_id=row["session_id"] if row else None,
                  status=row["status"] if row else status,
                  termination_reason=(row["termination_reason"] if row else reason),
                  tool_call_count=tool_calls,
                  retrieval_mode=retrieval_mode,
                  fallback_reason=fallback_reason,
                  index_version=index_version,
                  source_revision=source_revision)

    # -- reads / control -------------------------------------------------
    def get_run(self, run_id, project_id=None):
        self._ensure_started()
        row = self.store.get_run(run_id)
        if row is None:
            raise RunNotFound(run_id)
        if project_id is not None and row["project_id"] != project_id:
            raise RunNotFound(run_id)
        return row

    def list_runs(self, session_id=None, *, limit=20, offset=0, project_id=None):
        self._ensure_started()
        return self.store.list_runs(
            session_id, limit=limit, offset=offset, project_id=project_id)

    def events(self, run_id, *, after_seq=0, limit=100, project_id=None):
        self.get_run(run_id, project_id)
        return self.store.list_events(run_id, after_seq=after_seq, limit=limit)

    def result(self, run_id, *, project_id=None):
        row = self.get_run(run_id, project_id)
        return row, self.store.get_result(run_id)

    def tool_calls(self, run_id, *, project_id=None):
        self.get_run(run_id, project_id)
        return self.store.list_tool_calls(run_id)

    def evidence_refs(self, run_id, *, project_id=None):
        self.get_run(run_id, project_id)
        return self.store.list_evidence_refs(run_id)

    def cancel(self, run_id, *, project_id=None):
        self._ensure_started()
        row = self.get_run(run_id, project_id)
        row, changed = self.store.request_cancel(run_id)
        if changed:
            self._log(logging.INFO, "run_cancel_requested", run_id=run_id,
                      session_id=row["session_id"], status=row["status"])
            event = self._cancel_events.get(run_id)
            if event is not None:
                event.set()
            if row["status"] in TERMINAL_STATUSES:
                timer = self._timers.pop(run_id, None)
                if timer is not None:
                    timer.cancel()
        return row, changed

    def wait(self, run_id, timeout=10.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            row = self.store.get_run(run_id)
            if row is None or row["status"] in TERMINAL_STATUSES:
                return row
            if time.monotonic() >= deadline:
                return row
            time.sleep(0.02)

    # -- API serialization ----------------------------------------------
    @staticmethod
    def public_run(row):
        if row is None:
            return None
        value = dict(row)
        value["model_config"] = _loads(value.pop("model_config_json", None), {})
        value["budget"] = _loads(value.pop("budget_json", None), {})
        value["token_usage"] = _loads(value.pop("token_usage_json", None), {})
        value["result_available"] = bool(value.get("result_available"))
        value["cancel_requested"] = bool(value.get("cancel_requested"))
        return sanitize(value)

    @staticmethod
    def public_event(row):
        value = dict(row)
        value["payload"] = _loads(value.pop("payload_json", None), {})
        value["schema_version"] = int(value.get("schema_version") or 1)
        return sanitize(value)

    @staticmethod
    def public_message(row):
        value = dict(row)
        value["metadata"] = _loads(value.pop("metadata_json", None), {})
        return sanitize(value)

    @staticmethod
    def public_tool_call(row):
        value = dict(row)
        value["filters"] = _loads(value.pop("filters_json", None), {})
        value["result_summary"] = _loads(value.pop("result_summary_json", None), {})
        value["retryable"] = (None if value.get("retryable") is None
                              else bool(value["retryable"]))
        return sanitize(value)
