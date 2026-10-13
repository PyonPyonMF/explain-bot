"""Help must fit Discord messages and never become a paid generation request."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import bot_help as H
import interactions as I
from render_modes import response_format, text_task


class HelpTests(unittest.TestCase):
    def test_all_registered_sections_fit_discord_and_render_in_both_languages(self):
        commands = json.loads(Path(H.__file__).with_name("commands.json").read_text())
        command = next(c for c in commands if c["name"] == "help")
        self.assertEqual(command["name_localizations"]["ru"], "помощь")
        sections = [c["value"] for c in command["options"][0]["choices"]]
        self.assertEqual(set(sections), set(H.SECTIONS))
        for lang in ("ru", "en-US"):
            for section in sections:
                content = H.help_text(section, lang, "1527401426983325776")
                self.assertGreater(len(content), 100)
                self.assertLessEqual(len(content.encode("utf-16-le")) // 2, 1900)
                self.assertIn("/help", content)

    def test_help_is_an_explicit_mention_command_not_a_keyword_in_an_explanation(self):
        for command, section in (("помощь", "overview"), ("Help!", "overview"), ("Что ты умеешь?", "overview"),
                                 ("помощь видео", "media"), ("help 3d", "3d"), ("help summary", "text")):
            self.assertEqual(H.mention_section(command), section)
        for question in ("Нужна помощь с интегралом", "Explain the help command in Unix", 'Что значит "help"?',
                         "> помощь", "help me debug", "объясни текстом, что ты умеешь"):
            self.assertIsNone(H.mention_section(question))

    def test_public_examples_select_the_advertised_text_modes(self):
        for example in ("объясни что тут говорят текстом", "объясни на примере текстом",
                        "explain in text why this works", "сделай транскрипцию видео текстом", "summarize видео",
                        "transcribe this video", "summarize this video"):
            self.assertEqual(response_format(example), "text", example)
        self.assertEqual(text_task("сделай транскрипцию видео текстом"), "transcript")
        self.assertEqual(text_task("summarize видео"), "summary")

    def test_http_help_is_private_and_never_reads_quota_or_enqueues_work(self):
        enqueue = Mock()
        with patch.dict(os.environ, {"BOT_LANG": "ru"}), patch.object(I, "state", side_effect=AssertionError("Help must not use quota/state")):
            status, response = I.handle({"type": I.APPLICATION_COMMAND, "application_id": "456", "locale": "en-US",
                "data": {"type": I.CHAT_INPUT, "name": "help", "options": [{"name": "section", "value": "media"}]}}, enqueue)
        self.assertEqual(status, 200)
        self.assertEqual(response["type"], I.CHANNEL_MESSAGE)
        self.assertEqual(response["data"]["flags"], I.EPHEMERAL)
        self.assertIn("Видео, голосовые", response["data"]["content"])
        self.assertIn("<@456>", response["data"]["content"])
        self.assertEqual(response["data"]["allowed_mentions"], {"parse": []})
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
