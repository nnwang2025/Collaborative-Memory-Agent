"""Runtime configuration without import-time secret loading.

``load_env_file`` is an explicit server/CLI action.  Importing the application
must never read a developer's ``.env``: tests and embedded callers can inject a
database path, agent factory and budget directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from src.evidence_store import default_db_path


def load_env_file(path=None, *, override: bool = False) -> None:
    """Load a dotenv file only when the caller explicitly asks for it."""
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv(dotenv_path=path, override=override)


@dataclass(frozen=True)
class RuntimeConfig:
    db_path: Path | None = None
    max_workers: int = 4
    run_timeout_seconds: float = 120.0
    sse_heartbeat_seconds: float = 15.0
    sse_poll_seconds: float = 0.2
    max_concurrent_runs: int = 8
    session_history_runs: int = 3
    session_history_bytes: int = 6000
    max_message_chars: int = 4000
    max_events_per_page: int = 500
    max_runs_per_page: int = 100
    extra: dict = field(default_factory=dict)

    def resolved_db_path(self) -> Path:
        return Path(self.db_path) if self.db_path is not None else default_db_path()

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        def number(name, default, cast=float):
            try:
                return cast(os.getenv(name, "").strip() or default)
            except ValueError:
                return default

        return cls(
            db_path=Path(os.getenv("EVIDENCE_DB_PATH")) if os.getenv("EVIDENCE_DB_PATH") else None,
            max_workers=max(1, number("AGENT_RUNTIME_WORKERS", 4, int)),
            run_timeout_seconds=max(0.1, number("AGENT_RUN_TIMEOUT_SECONDS", 120.0)),
            sse_heartbeat_seconds=max(0.1, number("AGENT_SSE_HEARTBEAT_SECONDS", 15.0)),
            max_concurrent_runs=max(1, number("AGENT_MAX_CONCURRENT_RUNS", 8, int)),
        )
