"""Offline provider-response fixtures, not real LLM accuracy or tokenization tests."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from src.agents.react_agent import ReActAgent
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.ingest import normalize_record
from src.investigation import Budget, ModelReply, TokenUsage, parse_action
from src.smoke_diagnostics import diagnose

from offline_env import OFFLINE_PROVIDER_ENV


class ProtocolRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cma-protocol-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "synthetic.sqlite3"
        self.scope = EvidenceScope(project_id="protocol-test", dataset_kind="live")
        self.record = normalize_record("Decision B used bounded latency.", "benchmark://protocol/issue", "issues", "T-1",
            project_id=self.scope.project_id, source_type="issue")
        EvidenceStore(self.db).append([self.record])
        env = patch.dict(os.environ, {**OFFLINE_PROVIDER_ENV, "EVIDENCE_DB_PATH": str(self.db),
                                     "DECISION_DB_PATH": str(self.root / "absent-memory.sqlite3")})
        env.start(); self.addCleanup(env.stop)

    def tool(self, name="search_memory", **kwargs):
        return {"action": "tool_call", "tool": name, "query": "Decision", **kwargs}

    def finish(self, record=None, **kwargs):
        claims = [] if record is None else [{"text": record["content"], "refs": [
            {"evidence_id": record["evidence_id"], "content_hash": record["content_hash"]}]}]
        return {"action": "finish", "claims": claims, **kwargs}

    def reply(self, value, reason="stop", output=30):
        return ModelReply(text=value if isinstance(value, str) else json.dumps(value), finish_reason=reason,
            usage=TokenUsage(input_tokens=30, output_tokens=output, estimated=False))

    def agent(self, replies=(), budget=None):
        agent = ReActAgent(scope=self.scope, db_path=self.db, budget=budget)
        agent.api_key = "unit-test-only-never-sent"
        agent._call_llm = Mock(side_effect=list(replies))
        return agent

    @staticmethod
    def response(value):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(value).encode("utf-8")
        return Response()

    def test_http_request_limit_and_provider_stop_metadata(self):
        agent = self.agent()
        agent._call_llm = ReActAgent._call_llm.__get__(agent)
        body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.finish())}}],
                "usage": {"prompt_tokens": 17, "completion_tokens": 512, "completion_tokens_details": {"reasoning_tokens": 20}}}
        with patch("urllib.request.urlopen", return_value=self.response(body)) as transport:
            result = agent.run_result("Question")
        payload = json.loads(transport.call_args.args[0].data)
        self.assertEqual(payload["max_tokens"], 2048)
        step = result.trace[0]
        self.assertEqual((step.finish_reason, step.requested_output_tokens, step.reasoning_tokens), ("stop", 2048, 20))
        self.assertEqual(result.usage.output_tokens, 512)
        self.assertIsNone(step.protocol_category)  # 512 tokens is not evidence of truncation.

    def test_output_limit_is_capped_by_remaining_total_budget(self):
        agent = self.agent(budget=Budget(max_tokens=5000, max_output_tokens=8000))
        agent._call_llm = ReActAgent._call_llm.__get__(agent)
        body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.finish())}}]}
        with patch("urllib.request.urlopen", return_value=self.response(body)) as transport:
            result = agent.run_result("Question")
        payload = json.loads(transport.call_args.args[0].data)
        reservation = result.trace[0].context_usage["estimated_input_tokens"]
        self.assertEqual(payload["max_tokens"], 5000 - reservation)
        self.assertGreater(payload["max_tokens"], 0)
        self.assertTrue(result.usage.estimated)

    def test_long_finish_json_above_old_limit_passes_strict_validation(self):
        long_record = normalize_record("技术决策证据原文。" * 60, "benchmark://protocol/document", "docs", "L-1",
            project_id=self.scope.project_id, source_type="document")
        EvidenceStore(self.db).append([long_record])
        action = self.finish(long_record)
        action["claims"] *= 3
        self.assertGreater(len(json.dumps(action, ensure_ascii=False).encode("utf-8")), 3000)
        agent = self.agent()
        # Query must match the synthetic source; no canned evidence executor.
        agent._call_llm.side_effect = [self.reply({**self.tool("search_docs"), "query": "技术决策"}), self.reply(action, output=1200)]
        result = agent.run_result("Explain historical design")
        self.assertEqual(result.termination_reason, "sufficient")
        self.assertEqual(len(result.claims), 3)
        self.assertEqual(result.trace[-1].requested_output_tokens, 2048)
        self.assertTrue(all(c.refs[0].content_hash == long_record["content_hash"] for c in result.claims))

    def test_confirmed_truncation_is_not_parsed_or_executed_then_repairs(self):
        agent = self.agent([self.reply('{"action":"tool_call","tool":"search_issues","query":"Dec', "length", 512),
                            self.reply(self.finish())])
        with patch.object(agent.executor, "execute", wraps=agent.executor.execute) as execute:
            result = agent.run_result("Question")
        self.assertEqual(execute.call_count, 0)
        self.assertEqual(result.trace[0].protocol_category, "output_truncated")
        self.assertEqual(result.termination_detail, "finish_validated")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertFalse(result.claims)
        feedback = json.loads(agent._call_llm.call_args_list[1].args[0][-1]["content"])
        self.assertEqual(feedback["category"], "output_truncated")

    def test_complete_looking_json_with_length_stop_is_still_rejected(self):
        agent = self.agent([self.reply(self.tool(), "length", 512), self.reply(self.finish())])
        with patch.object(agent.executor, "execute", wraps=agent.executor.execute) as execute:
            result = agent.run_result("Question")
        self.assertEqual(execute.call_count, 0)
        self.assertEqual(result.trace[0].protocol_category, "output_truncated")

    def test_missing_usage_keeps_stop_reason_and_estimates_usage(self):
        agent = self.agent()
        agent._call_llm = ReActAgent._call_llm.__get__(agent)
        bodies = [self.response({"choices": [{"finish_reason": "length", "message": {"content": ""}}]}),
                  self.response({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.finish())}}]})]
        with patch("urllib.request.urlopen", side_effect=bodies):
            result = agent.run_result("Question")
        self.assertEqual(result.trace[0].finish_reason, "length")
        self.assertEqual(result.trace[0].protocol_category, "output_truncated")
        self.assertTrue(result.usage.estimated)

    def test_empty_output_without_stop_reason_is_not_confirmed_truncation(self):
        result = self.agent([self.reply("", None, 512), self.reply(self.finish())]).run_result("Question")
        self.assertEqual(result.trace[0].protocol_category, "empty_output")
        self.assertIsNone(result.trace[0].finish_reason)

    def test_illegal_filters_get_actionable_feedback_without_echo_or_scope_change(self):
        bad = self.tool(filters={"source_types": ["decision", "doc"], "private-marker-key": "private-marker-value"})
        agent = self.agent([self.reply(bad), self.reply(self.finish())])
        result = agent.run_result("Question")
        feedback = json.loads(agent._call_llm.call_args_list[1].args[0][-1]["content"])
        self.assertEqual(feedback["category"], "invalid_filter")
        self.assertIn("document", feedback["allowed_filters"]["source_types"])
        self.assertTrue(any(i["path"] == "filters.source_types.0" for i in feedback["validation_issues"]))
        self.assertNotIn("private-marker", json.dumps(feedback) + result.model_dump_json())
        self.assertEqual(result.scope, self.scope)
        with self.assertRaises(ValueError): parse_action(json.dumps(bad))

    def test_extra_json_or_prose_is_not_silently_salvaged(self):
        for suffix in ('\nAdditional private-marker-text', '\n{"action":"finish"}'):
            with self.subTest(suffix=suffix):
                agent = self.agent([self.reply(json.dumps(self.tool()) + suffix), self.reply(self.finish())])
                with patch.object(agent.executor, "execute", wraps=agent.executor.execute) as execute:
                    result = agent.run_result("Question")
                self.assertEqual(execute.call_count, 0)
                self.assertEqual(result.trace[0].protocol_category, "json_extra_data")
                self.assertNotIn("private-marker", result.model_dump_json())

    def test_other_schema_errors_and_provider_stop_reason_are_distinct(self):
        for value, stop, expected in [({"action": "finish", "gaps": "wrong_type"}, "stop", "schema_mismatch"),
                                      (self.finish(), "content_filter", "unexpected_finish_reason")]:
            with self.subTest(category=expected):
                result = self.agent([self.reply(value, stop), self.reply(self.finish())]).run_result("Question")
                self.assertEqual(result.trace[0].protocol_category, expected)
                self.assertFalse(result.claims)

    def test_consecutive_protocol_repairs_are_bounded_without_hidden_calls(self):
        agent = self.agent([self.reply("{")] * 5, Budget(max_model_rounds=7, max_protocol_retries=2))
        result = agent.run_result("Question")
        self.assertEqual(agent._call_llm.call_count, 3)
        self.assertEqual(result.termination_detail, "protocol_retry_limit")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertFalse(result.claims)

    def test_truncation_on_last_normal_round_gets_reserved_recovery_call(self):
        agent = self.agent([self.reply(self.tool()),
                            self.reply('{"action":"finish","claims":[', "length", 2048),
                            self.reply(self.finish(self.record))],
                           Budget(max_model_rounds=2, max_truncation_retry_output_tokens=4096))
        result = agent.run_result("Question")
        self.assertEqual(agent._call_llm.call_count, 3)
        self.assertEqual(result.trace[2].protocol_category, "output_truncated")
        retry = result.trace[3]
        self.assertEqual(retry.recovery_kind, "output_budget_retry")
        self.assertEqual(retry.requested_output_tokens, 4096)
        self.assertEqual((result.termination_reason, result.termination_detail),
                         ("sufficient", "finish_validated"))
        self.assertEqual(len(result.claims), 1)

    def test_truncation_recovery_is_not_blocked_by_protocol_retry_limit(self):
        agent = self.agent([self.reply("{"), self.reply("{"),
                            self.reply('{"action":"finish","claims":[', "length", 2048),
                            self.reply({"action": "finish", "claims": [], "gaps": ["insufficient"]})],
                           Budget(max_model_rounds=3, max_protocol_retries=2,
                                  max_truncation_retry_output_tokens=4096))
        result = agent.run_result("Question")
        self.assertEqual(agent._call_llm.call_count, 4)
        self.assertEqual(result.trace[2].protocol_category, "output_truncated")
        self.assertEqual(result.trace[3].recovery_kind, "output_budget_retry")
        self.assertEqual(result.trace[3].requested_output_tokens, 4096)
        self.assertFalse(result.claims)

    def test_truncation_recovery_is_bounded_to_one_call(self):
        agent = self.agent([self.reply(self.tool()),
                            self.reply('{"action":"finish","claims":[', "length", 2048),
                            self.reply('{"action":"finish","claims":[', "length", 4096)],
                           Budget(max_model_rounds=2, max_truncation_retry_output_tokens=4096))
        result = agent.run_result("Question")
        self.assertEqual(agent._call_llm.call_count, 3)
        self.assertEqual(result.termination_reason, "budget")
        self.assertEqual(result.termination_detail, "output_truncated_after_recovery")
        self.assertFalse(result.claims)

    def test_model_round_exhaustion_is_reported_separately(self):
        agent = self.agent([self.reply("{")] * 5, Budget(max_model_rounds=2, max_protocol_retries=5))
        result = agent.run_result("Question")
        self.assertEqual(agent._call_llm.call_count, 2)
        self.assertEqual(result.termination_reason, "budget")
        self.assertEqual(result.termination_detail, "model_round_budget")

    def test_total_token_budget_exhaustion_rejects_tool_action(self):
        agent = self.agent([ModelReply(text=json.dumps(self.tool()), finish_reason="stop",
            usage=TokenUsage(input_tokens=10000, estimated=False))], Budget(max_tokens=10000))
        with patch.object(agent.executor, "execute", wraps=agent.executor.execute) as execute:
            result = agent.run_result("Question")
        self.assertEqual(execute.call_count, 0)
        self.assertEqual((result.termination_reason, result.termination_detail), ("budget", "token_budget"))

    def test_total_token_budget_uses_tokens_not_utf8_bytes(self):
        agent = self.agent([self.reply(self.finish())],
                           Budget(max_tokens=5000, max_output_tokens=4096))
        usage = {
            "estimated_input_tokens": 1000,
            "input_bytes": 6000,  # bytes exceed the remaining tokens
            "tokenizer_mode": "estimated",
            "candidate_evidence_count": 0,
            "selected_evidence_count": 0,
            "deduped_evidence_count": 0,
            "pruned_count": 0,
            "pruning_reasons": {},
            "important_conflicts_retained": False,
        }
        with patch("src.agents.react_agent.InvestigationContext.request",
                   return_value=([{"role": "system", "content": "rules"}], usage)):
            result = agent.run_result("Question")
        self.assertEqual(agent._call_llm.call_count, 1)
        self.assertNotEqual(result.termination_detail, "token_budget")
        self.assertEqual(result.trace[0].requested_output_tokens, 4000)

    def test_tool_failure_switches_source_and_short_finish_after_truncation(self):
        agent = self.agent([self.reply(self.tool("search_issues")), self.reply(self.tool("search_memory")),
            self.reply('{"action":"finish","claims":[', "length", 512), self.reply(self.finish(self.record))])
        with patch.object(agent.executor.registry.get("problem"), "run", side_effect=TimeoutError("private failure")):
            result = agent.run_result("Question")
        self.assertEqual([s.tool_name for s in result.trace if s.action == "tool_attempt"],
                         ["search_issues", "search_issues", "search_memory"])
        self.assertEqual(result.termination_reason, "sufficient")
        self.assertTrue(result.claims)
        self.assertEqual(result.trace[-2].protocol_category, "output_truncated")
        self.assertNotIn("private failure", result.model_dump_json())

    def test_schema_valid_finish_does_not_bypass_hash_ledger_validation(self):
        bad = self.finish(self.record)
        bad["claims"][0]["refs"][0]["content_hash"] = "0" * 64
        result = self.agent([self.reply(self.tool()), self.reply(bad)]).run_result("Question")
        self.assertEqual(result.termination_detail, "citation_validation_failed")
        self.assertEqual(result.termination_reason, "insufficient")
        self.assertFalse(result.claims)
        self.assertTrue(any(e.error_code == "citation_error" for s in result.trace for e in s.errors))
        self.assertFalse(any(s.protocol_category for s in result.trace))

    def test_saved_report_diagnostics_separate_observation_from_inference(self):
        actions = [json.dumps(self.tool(filters={"source_types": ["doc"]})), '{"action":"finish","claims":[',
                   json.dumps(self.finish()), json.dumps(self.finish())]
        report = {"results": [{"scenario": "synthetic", "actions": actions, "trace": [
            {"action": "protocol_error", "usage": {"output_tokens": 30}},
            {"action": "protocol_error", "usage": {"output_tokens": 512}},
            {"action": "protocol_error", "finish_reason": "length", "usage": {"output_tokens": 512}},
            {"action": "finish", "finish_reason": "stop", "usage": {"output_tokens": 512}}]}]}
        result = diagnose(report, legacy_output_limit=512)
        self.assertEqual(result["category_counts"], {"invalid_filter": 1, "json_syntax": 1, "output_truncated": 1, "schema_valid": 1})
        scenario = result["scenarios"][0]
        self.assertEqual((scenario["truncation_confirmed"], scenario["output_limit_suspected"]), (1, 1))

    def test_smoke_report_retains_stop_metadata_without_any_provider_request(self):
        from src.llm_smoke import run_suite
        original = ReActAgent
        def factory(**kwargs):
            agent = original(**kwargs)
            agent.api_key = "unit-test-key"
            agent._call_llm = lambda messages: ModelReply(text=json.dumps(self.finish()), finish_reason="stop",
                requested_output_tokens=2048, usage=TokenUsage(input_tokens=10, output_tokens=10, estimated=False))
            return agent
        with patch("src.llm_smoke.prepare", return_value=(self.db, self.scope)), \
             patch("src.agents.react_agent.ReActAgent", side_effect=factory), \
             patch.dict(os.environ, {"SILICONFLOW_BASE_URL": "https://synthetic.invalid/v1",
                                     "DEEPSEEK_BASE_URL": ""}), \
             patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            report = run_suite(online=True, approved_origin="https://synthetic.invalid", output=self.root / "only-fixture.json")
        self.assertEqual(len(report["results"]), 5)
        for row in report["results"]:
            self.assertEqual(row["model_responses"][0]["finish_reason"], "stop")
            self.assertEqual(row["configuration"]["budget"]["max_output_tokens"], 2048)
            self.assertIsNone(row["task_success"])
            self.assertTrue(row["finish_accepted"])
        self.assertNotIn("unit-test-key", json.dumps(report))

    def test_http_error_reports_status_and_redacts_provider_body(self):
        secret = "deepseek-test-key-never-print"
        with patch.dict(os.environ, {
                "SILICONFLOW_BASE_URL": "https://api.deepseek.com/",
                "SILICONFLOW_MODEL": "deepseek-flash",
                "SILICONFLOW_API_KEY": "", "OPENAI_API_KEY": "",
                "DEEPSEEK_BASE_URL": "", "DEEPSEEK_MODEL": "",
                "DEEPSEEK_API_KEY": secret}, clear=False):
            agent = ReActAgent(scope=self.scope, db_path=self.db)
        agent._call_llm = ReActAgent._call_llm.__get__(agent)
        body = json.dumps({"error": {
            "code": "invalid_api_key",
            "message": f"Incorrect API key provided: {secret}"}}).encode("utf-8")
        failure = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 401,
                                         "Unauthorized", {}, io.BytesIO(body))
        with patch("urllib.request.urlopen", side_effect=failure) as transport:
            result = agent.run_result("Question")
        error = result.trace[0].errors[0]
        self.assertEqual(error.diagnostic["phase"], "http_request")
        self.assertEqual(error.diagnostic["http_status"], 401)
        self.assertEqual(error.diagnostic["provider_error_code"], "invalid_api_key")
        self.assertEqual(error.diagnostic["credential_source"], "DEEPSEEK_API_KEY")
        self.assertIn("[REDACTED]", error.diagnostic["provider_message"])
        self.assertIn("HTTP 401", error.message)
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(json.loads(request.data)["model"], "deepseek-flash")
        serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("Authorization", serialized)

    def test_model_auth_error_is_not_mapped_to_evidence_insufficiency(self):
        agent = self.agent()
        body = json.dumps({"error": {
            "code": "invalid_api_key",
            "message": "Authentication Fails"}}).encode("utf-8")
        failure = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 401,
                                         "Unauthorized", {}, io.BytesIO(body))
        agent._call_llm = Mock(side_effect=failure)
        result = agent.run_result("Question")
        self.assertEqual(result.termination_reason, "model_error")
        self.assertEqual(result.termination_detail, "model_error")
        self.assertEqual(agent._call_llm.call_count, 1)
        self.assertFalse(any(step.action == "tool_attempt" for step in result.trace))
        self.assertFalse(any(step.protocol_category for step in result.trace))
        self.assertTrue(result.usage.estimated)

    def test_transport_error_reports_phase_without_exception_text(self):
        agent = self.agent()
        agent._call_llm = ReActAgent._call_llm.__get__(agent)
        reason = ConnectionRefusedError("private transport detail")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(reason)):
            result = agent.run_result("Question")
        diagnostic = result.trace[0].errors[0].diagnostic
        self.assertEqual(diagnostic["phase"], "transport")
        self.assertEqual(diagnostic["transport_reason"], "ConnectionRefusedError")
        self.assertNotIn("private transport detail", result.model_dump_json())

    def test_invalid_provider_body_is_classified_as_response_decode(self):
        agent = self.agent()
        agent._call_llm = ReActAgent._call_llm.__get__(agent)
        class BadResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b"<html>private provider page</html>"
        with patch("urllib.request.urlopen", return_value=BadResponse()):
            result = agent.run_result("Question")
        self.assertEqual(result.trace[0].errors[0].diagnostic["phase"], "response_decode")
        self.assertNotIn("private provider page", result.model_dump_json())

    def test_unexpected_response_shape_is_classified_without_body(self):
        agent = self.agent()
        agent._call_llm = ReActAgent._call_llm.__get__(agent)
        with patch("urllib.request.urlopen", return_value=self.response({})):
            result = agent.run_result("Question")
        self.assertEqual(result.trace[0].errors[0].diagnostic["phase"], "response_shape")


if __name__ == "__main__":
    unittest.main()
