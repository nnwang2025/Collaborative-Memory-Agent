"""Static delivery-safety checks; Docker itself is not required or invoked."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src import llm_config


ROOT = Path(__file__).resolve().parents[1]


class DeliveryConfigTests(unittest.TestCase):
    def test_dockerignore_covers_secrets_and_local_runtime_data(self):
        patterns = {line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")}
        for required in (".env", ".env.*", "!.env.example", "venv/", "db_md/",
                         "data/live/", "data/chat_raw.json", "*.sqlite3", "benchmark/reports/"):
            self.assertIn(required, patterns)

    def test_dockerfile_fails_closed_on_secret_files_and_drops_root(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY . .", dockerfile)
        self.assertIn("test ! -e .env", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertNotIn("COPY .env", dockerfile)

    def test_env_example_has_no_non_empty_credential(self):
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name, _, value = stripped.partition("=")
            if name in {"DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY", "OPENAI_API_KEY"}:
                self.assertEqual(value.strip(), "", f"{name} must be empty in .env.example")

    def test_compose_passes_deepseek_and_legacy_variables(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
                     "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL",
                     "OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_NAME"):
            self.assertIn(f"{name}=${{{name}:-}}", compose)


class LLMConfigTests(unittest.TestCase):
    def test_placeholder_secret_is_not_a_credential(self):
        for value in ("", "   ", "your_api_key_here", "sk-your-key-here",
                      "<your-key>", "changeme", "placeholder"):
            self.assertTrue(llm_config.is_placeholder_secret(value), value)
        self.assertFalse(llm_config.is_placeholder_secret("sk-0123456789abcdef"))

    def test_placeholder_key_is_ignored_at_runtime(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "your_api_key_here"}, clear=True):
            self.assertEqual(llm_config.resolve_api_key("https://api.deepseek.com"), "")
            self.assertIsNone(llm_config.credential_source("https://api.deepseek.com"))

    def test_deepseek_key_only_selects_official_endpoint(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-real-looking-value"}, clear=True):
            base_url = llm_config.resolve_base_url()
            self.assertEqual(base_url, llm_config.DEFAULT_DEEPSEEK_BASE_URL)
            self.assertEqual(llm_config.resolve_model_name(base_url), "deepseek-flash")
            self.assertEqual(llm_config.resolve_api_key(base_url), "sk-real-looking-value")
            self.assertEqual(llm_config.credential_source(base_url), "DEEPSEEK_API_KEY")

    def test_explicit_deepseek_settings_are_honoured(self):
        env = {"DEEPSEEK_API_KEY": "sk-real-looking-value",
               "DEEPSEEK_BASE_URL": "https://api.deepseek.com/",
               "DEEPSEEK_MODEL": "deepseek-custom"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(llm_config.resolve_base_url(), "https://api.deepseek.com")
            self.assertEqual(llm_config.resolve_model_name(), "deepseek-custom")

    def test_legacy_siliconflow_configuration_still_works(self):
        env = {"SILICONFLOW_API_KEY": "sf-legacy-value",
               "SILICONFLOW_BASE_URL": "https://api.siliconflow.cn/v1",
               "SILICONFLOW_MODEL": "Qwen/Qwen2.5-14B-Instruct"}
        with patch.dict(os.environ, env, clear=True):
            base_url = llm_config.resolve_base_url()
            self.assertEqual(base_url, "https://api.siliconflow.cn/v1")
            self.assertEqual(llm_config.resolve_model_name(base_url), "Qwen/Qwen2.5-14B-Instruct")
            self.assertEqual(llm_config.resolve_api_key(base_url), "sf-legacy-value")

    def test_deepseek_key_never_forwarded_to_another_origin(self):
        env = {"DEEPSEEK_API_KEY": "sk-deepseek-only",
               "SILICONFLOW_BASE_URL": "https://api.siliconflow.cn/v1"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(llm_config.resolve_api_key("https://api.siliconflow.cn/v1"), "")


if __name__ == "__main__":
    unittest.main()
