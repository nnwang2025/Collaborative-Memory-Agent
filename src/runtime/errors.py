"""Runtime control-flow and API-facing errors.

These exceptions describe control decisions (cancel, idempotency, scope) and
never carry raw provider payloads or credentials.
"""

from __future__ import annotations


class RuntimeConflict(Exception):
    """A request conflicts with persisted runtime state."""

    code = "runtime_conflict"


class IdempotencyConflict(RuntimeConflict):
    code = "idempotency_key_reused_with_different_request"


class SessionBusy(RuntimeConflict):
    code = "session_run_in_progress"


class InvalidRunTransition(RuntimeConflict):
    code = "invalid_run_transition"


class SessionNotFound(LookupError):
    code = "session_not_found"


class RunNotFound(LookupError):
    code = "run_not_found"


class SessionClosed(RuntimeConflict):
    code = "session_closed"


class ScopeMismatch(RuntimeConflict):
    code = "scope_mismatch"


class RunCancelled(Exception):
    """Cooperative cancellation signal raised at an Agent boundary."""


class RunTimedOut(Exception):
    """Cooperative timeout signal; the worker may still unwind in the background."""
