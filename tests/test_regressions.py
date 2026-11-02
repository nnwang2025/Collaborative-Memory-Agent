import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from src.agents.answer_agent import AnswerAgent
from src.agents.coordinator_agent import CoordinatorAgent
from src.agents.react_agent import ReActAgent
from src.chunking import RecursiveSplitter
from src import evaluate, retrieval

from offline_env import OFFLINE_PROVIDER_ENV


class APIRegressionTests(unittest.TestCase):
    def test_factory_isolates_history(self):
        for backend in ("linear", "langgraph"):
            first = api._get_agent(backend)
            second = api._get_agent(backend)
            first.conversation.add("private", "secret")
            self.assertIsNot(first, second)
            self.assertFalse(second.conversation.has_history())

    def test_concurrent_requests_do_not_share_followup_context(self):
        from concurrent.futures import ThreadPoolExecutor

        def ask(question):
            with TestClient(api.app) as client:
                return client.post("/chat", json={"question": question, "backend": "linear"}).json()

        with patch.object(CoordinatorAgent, "_ask_linear", side_effect=lambda q: q):
            with ThreadPoolExecutor(max_workers=2) as pool:
                answers = list(pool.map(ask, ["private information", "那后来呢？"]))
        self.assertEqual(answers[1]["answer"], "那后来呢？")

    def test_invalid_backend_and_empty_question(self):
        with TestClient(api.app) as client:
            self.assertEqual(client.post("/chat", json={"question": "x", "backend": "typo"}).status_code, 422)
            self.assertEqual(client.post("/chat", json={"question": " "}).status_code, 400)

    def test_stream_executes_graph_once(self):
        class Graph:
            calls = 0

            async def astream(self, state):
                self.calls += 1
                yield {"classify_intent": {"scores": {}}}
                yield {"generate_answer": {"answer": "verified result"}}

        graph = Graph()
        with patch("src.graph.get_graph", return_value=graph), patch.object(CoordinatorAgent, "ask", side_effect=AssertionError("duplicate run")):
            with TestClient(api.app) as client:
                response = client.post("/chat/stream", json={"question": "x"})
        self.assertIn("verified result", response.text)
        self.assertIn("[DONE]", response.text)
        self.assertNotIn('"type": "error"', response.text)
        self.assertEqual(graph.calls, 1)

    def test_nonstream_backends(self):
        class Agent:
            def ask(self, q): return "ask:" + q
            def run(self, q): return "run:" + q

        with patch("api._get_agent", return_value=Agent()), TestClient(api.app) as client:
            for backend in ("linear", "langgraph", "react"):
                result = client.post("/chat", json={"question": "q", "backend": backend})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["answer"], ("run:" if backend == "react" else "ask:") + "q")


class EvidenceRegressionTests(unittest.TestCase):
    def test_no_evidence_no_claims_or_orphan_sources(self):
        result = AnswerAgent().compose("Rerank问题怎么修复？", ["problem"],
                                       {"problem": {"evidence": [], "sources": ["nonexistent"]}}, {"problem": 10})
        self.assertIn("证据不足", result)
        self.assertNotIn("修复方式：", result)
        self.assertNotIn("nonexistent", result)
        self.assertNotIn("\n高\n", result)

    def test_offline_does_not_invent_fix(self):
        agent = AnswerAgent()
        agent.siliconflow_api_key = ""
        result = agent.compose("Rerank问题怎么修复？", ["problem"],
                               {"problem": {"evidence": [{"source": "issue.json", "content": "Rerank fails; root cause unknown"}]}})
        self.assertNotIn("提高 Decision Memory", result)
        self.assertIn("root cause unknown", result)

    def test_react_rejects_unobserved_answer(self):
        with patch.object(CoordinatorAgent, "_warm_hybrid_retriever"):
            agent = ReActAgent()
        agent.api_key = "fake"
        with patch.object(agent, "_call_llm", return_value='{"action":"answer","summary":"invented fact"}'):
            result = agent.run("why")
        self.assertNotIn("invented fact", result)
        self.assertIn("未检索到", result)

    def test_react_nonobject_json_does_not_crash(self):
        for raw in ("[]", "null", "1", '"text"'):
            self.assertEqual(ReActAgent._parse_action(raw)["action"], "error")

    def test_subset_retrieval_excludes_foreign_evidence_and_preserves_records(self):
        class Retriever:
            def dense_search(self, *args, **kwargs):
                return [{"id": "foreign", "content": "outside corpus"}]

        docs = [{"source": "a", "record": 1, "content": "target alpha"},
                {"source": "a", "record": 2, "content": "target beta"}]
        with patch.object(retrieval, "get_hybrid_retriever", return_value=Retriever()):
            results = retrieval.hybrid_rank("target", docs)
            self.assertEqual({r["record"] for r in results}, {1, 2})
            self.assertEqual(retrieval.hybrid_rank("target", []), [])
        self.assertNotIn("id", docs[0])


class ChunkingAndEvaluationTests(unittest.TestCase):
    def test_leading_separator_terminates(self):
        splitter = RecursiveSplitter(["。", " "], chunk_size=32, chunk_overlap=4)
        chunks = splitter.split("。" + "x" * 100)
        self.assertTrue(chunks)
        self.assertIn("x" * 10, "".join(chunks))

    def test_invalid_overlap_rejected(self):
        with self.assertRaises(ValueError):
            RecursiveSplitter([], chunk_size=4, chunk_overlap=4)

    def test_metric_is_hit_not_recall(self):
        class Retriever:
            docs = [{"id": str(i), "content": "alpha beta"} for i in range(10)]
            def _ensure_loaded(self): pass
            def retrieve(self, *args, **kwargs): return self.docs[:1]

        with patch.object(evaluate, "TEST_SET", [("alpha", "memory", ["alpha", "beta"])]), patch.object(retrieval, "HybridRetriever", Retriever):
            result = evaluate.evaluate_retrieval()
        self.assertEqual(result["hybrid_hit@5"], 1.0)
        self.assertNotIn("hybrid_recall@5", result)
        self.assertEqual(result["skipped_queries"], 0)

    def test_actual_graph_offline(self):
        # Runs real routing, fan-out, retrieval and composition; no model/API required.
        with patch.dict("os.environ", OFFLINE_PROVIDER_ENV), patch.object(CoordinatorAgent, "_warm_hybrid_retriever"):
            agent = CoordinatorAgent()
            answer = agent.ask("项目架构文档")
        self.assertIn("【回答】", answer)
        self.assertIn("【关键证据】", answer)
        self.assertEqual(len(agent.conversation), 1)


if __name__ == "__main__":
    unittest.main()
