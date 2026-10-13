"""Cache boundaries and token accounting, with mocked network requests."""
import json
import os
import unittest
from unittest.mock import patch

import assets
import fullgen
import pipeline
import prompt_cache as C


class PromptCacheTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_PROMPT_CACHE": "1",
                                      "ANTHROPIC_CACHE_TTL": "1h"})
        env.start()
        self.addCleanup(env.stop)

    def test_changing_questions_does_not_change_reusable_prefix(self):
        reply = json.dumps({"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"}).encode()
        for generate in (
            lambda q: assets._ask_claude("Stable visual instructions", [{"type": "text", "text": q}], "visual plan"),
            lambda q: fullgen.claude(fullgen.GEN_SYSTEM, [{"type": "text", "text": q}]),
            lambda q: pipeline.ask_claude({}, ([], q)),
        ):
            with self.subTest(generate=generate), patch.object(pipeline, "_http", return_value=(200, reply)) as http:
                generate("Question A")
                generate("Question B")
                first, second = (json.loads(call.args[1]) for call in http.call_args_list)
                self.assertEqual(first["system"], second["system"])
                self.assertEqual(first["system"][-1]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
                self.assertNotEqual(first["messages"], second["messages"])
                self.assertNotIn("cache_control", first)
                self.assertNotIn("cache_control", json.dumps(first["messages"]))

    def test_repair_suffix_stays_outside_cached_prefix(self):
        original = C.system("Stable instructions")
        repaired = C.system("Stable instructions", "\nFix the previous output")
        self.assertEqual(original[0], repaired[0])
        self.assertNotIn("cache_control", repaired[1])

    def test_disabled_restores_plain_system(self):
        with patch.dict(os.environ, {"ANTHROPIC_PROMPT_CACHE": "0"}):
            self.assertEqual(C.system("Static", "\nFeedback"), "Static\nFeedback")

    def test_ttl_selection_and_invalid_value_cannot_break_requests(self):
        for value, expected in (("5m", "5m"), ("1h", "1h"), ("invalid", "1h")):
            with patch.dict(os.environ, {"ANTHROPIC_CACHE_TTL": value}):
                self.assertEqual(C.system("Instructions")[0]["cache_control"]["ttl"], expected)
        self.assertEqual(C.system(""), "")

    def test_usage_logs_only_counters_and_counts_all_input_categories(self):
        response = {"content": [{"text": "private-answer"}], "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 70,
            "output_tokens": 8, "cache_creation": {"ephemeral_5m_input_tokens": 5, "ephemeral_1h_input_tokens": 15}}}
        with self.assertLogs("claude_usage", level="INFO") as records:
            C.record_usage(response, "test")
        output = records.output[0]
        for expected in ("input=10", "cache_write=20", "cache_read=70", "output=8", "cache_write_5m=5",
                         "cache_write_1h=15", "cache_read_pct=70.0"):
            self.assertIn(expected, output)
        self.assertNotIn("private-answer", output)
        self.assertNotIn("test-key", output)

    def test_missing_or_zero_usage_is_safe(self):
        with self.assertNoLogs("claude_usage"):
            C.record_usage({}, "test")
        with self.assertLogs("claude_usage", level="INFO") as records:
            C.record_usage({"usage": {"input_tokens": 0}}, "test")
        self.assertIn("cache_read_pct=0.0", records.output[0])


if __name__ == "__main__":
    unittest.main()
