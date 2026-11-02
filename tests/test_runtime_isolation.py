"""Configuration isolation and offline execution for the runtime."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.evidence import EvidenceScope
from src.runtime.config import RuntimeConfig
from src.runtime.service import RunService

from tests.runtime_helpers import agent_factory, wait_status

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "isolation-project"
SENTINEL = "CMA_TEST_DOTENV_SENTINEL"


class RuntimeIsolationTests(unittest.TestCase):
    def test_importing_app_does_not_read_a_dotenv_file(self):
        with tempfile.TemporaryDirectory(prefix="cma-dotenv-") as temp:
            Path(temp, ".env").write_text(
                f"{SENTINEL}=loaded-from-file\nDEEPSEEK_API_KEY=not-a-real-key\n",
                encoding="utf-8")
            script = (
                "import os, sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "import api\n"
                "print(os.getenv(%r, 'NOT_SET'))\n" % SENTINEL
            )
            environment = {key: value for key, value in os.environ.items()
                           if key != SENTINEL}
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=temp, env=environment,
                capture_output=True, text=True, timeout=180)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "NOT_SET")

    def test_offline_react_run_makes_no_network_connections(self):
        from src.agents.react_agent import ReActAgent

        with tempfile.TemporaryDirectory(prefix="cma-offline-") as temp:
            db = Path(temp) / "runtime.sqlite3"

            def factory(*, scope, observer, cancel_check, budget):
                with patch("src.agents.react_agent._resolve_api_key", return_value=""), \
                        patch("src.agents.answer_agent.resolve_api_key", return_value=""):
                    agent = ReActAgent(scope=scope, db_path=db, budget=budget,
                                       observer=observer, cancel_check=cancel_check)
                self.assertEqual(agent.api_key, "")
                agent.executor.execute = lambda tool, query, filters=None, timeout=None: {
                    "tool": tool, "skill": "document", "query": query, "evidence": [],
                    "sources": [], "decision_memories": [],
                    "retrieval_diagnostics": {"effective_mode": "bm25"},
                }
                return agent

            service = RunService(
                db, agent_factory=factory,
                config=RuntimeConfig(db_path=db, run_timeout_seconds=10))
            self.addCleanup(service.close)
            session = service.sessions.create(PROJECT, "live")
            blocked = AssertionError("network access is not allowed in offline tests")
            with patch("socket.socket.connect", side_effect=blocked), \
                    patch("socket.create_connection", side_effect=blocked):
                run, _ = service.submit(session["session_id"], "offline question")
                done = wait_status(service, run["run_id"], {"insufficient", "failed"},
                                   timeout=10)
            self.assertEqual(done["status"], "insufficient", done)
            events = [event["event_type"] for event in service.events(
                run["run_id"], project_id=PROJECT)]
            self.assertIn("model_skipped", events)
            self.assertNotIn("model_requested", events)


if __name__ == "__main__":
    unittest.main()
