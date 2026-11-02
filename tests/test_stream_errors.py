"""SSE error-path regressions: every failure ends with a terminal marker."""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from src.agents.coordinator_agent import CoordinatorAgent


def _events(text):
    return [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]


def _json_events(text):
    return [json.loads(value) for value in _events(text) if value != "[DONE]"]


class StreamErrorTests(unittest.TestCase):
    def test_react_exception_emits_structured_error_and_done(self):
        class Agent:
            def run_result(self, question):
                raise RuntimeError("boom")

        with patch("api._get_agent", return_value=Agent()):
            with TestClient(api.app) as client:
                response = client.post(
                    "/chat/stream", json={"question": "x", "backend": "react"})
        events = _events(response.text)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(events)
        self.assertEqual(events[-1], "[DONE]")
        errors = [event for event in _json_events(response.text) if event.get("type") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("boom", errors[0]["message"])

    def test_react_run_exception_emits_structured_error_and_done(self):
        class Agent:
            def run(self, question):
                raise RuntimeError("run failed")

        with patch("api._get_agent", return_value=Agent()):
            with TestClient(api.app) as client:
                response = client.post(
                    "/chat/stream", json={"question": "x", "backend": "react"})
        self.assertEqual(_events(response.text)[-1], "[DONE]")
        self.assertEqual(
            [event["type"] for event in _json_events(response.text)], ["error"])

    def test_langgraph_exception_after_node_emits_error_and_done(self):
        class Graph:
            async def astream(self, state):
                yield {"classify_intent": {"scores": {}}}
                raise RuntimeError("graph exploded")

        with patch("src.graph.get_graph", return_value=Graph()), \
                patch.object(CoordinatorAgent, "ask", side_effect=AssertionError("duplicate run")):
            with TestClient(api.app) as client:
                response = client.post(
                    "/chat/stream", json={"question": "x", "backend": "langgraph"})
        events = _events(response.text)
        self.assertEqual(events[0], json.dumps({"type": "node", "node": "classify_intent"}))
        self.assertEqual(events[-1], "[DONE]")
        payload = _json_events(response.text)
        self.assertEqual([event["type"] for event in payload], ["node", "error"])
        self.assertIn("graph exploded", payload[-1]["message"])

    def test_error_message_redacts_credentials(self):
        class Agent:
            def run_result(self, question):
                raise RuntimeError("Bearer sk-secret-value-123456 denied")

        with patch("api._get_agent", return_value=Agent()):
            with TestClient(api.app) as client:
                response = client.post(
                    "/chat/stream", json={"question": "x", "backend": "react"})
        self.assertNotIn("sk-secret-value-123456", response.text)
        self.assertIn("[REDACTED]", response.text)
        self.assertEqual(_events(response.text)[-1], "[DONE]")


if __name__ == "__main__":
    unittest.main()
