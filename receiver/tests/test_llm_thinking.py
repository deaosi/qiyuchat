"""Provider thinking controls are optional and do not change legacy cleaning."""
import os
import unittest
from unittest.mock import patch

from src.config import LLMConfig, load_settings
from src.clean.llm import LLMClient


class TestThinkingConfig(unittest.TestCase):
    def test_default_and_allowed_environment_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_settings().llm.thinking, "")
            for raw, expected in (("", ""), ("enabled", "enabled"), ("disabled", "disabled"), (" DISABLED ", "disabled")):
                os.environ["LLM_THINKING"] = raw
                self.assertEqual(load_settings().llm.thinking, expected)

    def test_invalid_environment_value_is_rejected_without_echoing_it(self):
        with patch.dict(os.environ, {"LLM_THINKING": "unsupported-value"}, clear=True):
            with self.assertRaises(ValueError) as caught:
                load_settings()
            self.assertIn("LLM_THINKING", str(caught.exception))
            self.assertNotIn("unsupported-value", str(caught.exception))

    def test_legacy_cleaning_payload_is_unchanged(self):
        cfg = LLMConfig(thinking="disabled")
        payload = LLMClient(cfg)._payload("model", "system", "user", True)
        self.assertNotIn("thinking", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
