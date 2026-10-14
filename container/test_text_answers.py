"""Opt-in text answers, media-free execution, complete delivery and mixed-format follow-ups."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import followups
import interactions as I
import pipeline
from render_modes import response_format
import text_answers as T


class FormatTests(unittest.TestCase):
    def test_format_can_follow_the_question_in_natural_word_order(self):
        for text in ["объясни что тут говорят текстом", "обьясни что тут говорят текстом",
                     "Объясни, что происходит в этом ролике, текстом.", "Расскажи почему он так ответил текстом",
                     "Поясни как работает эта штука в текстовом виде", "Объясни https://x.com/example/status/1 текстом",
                     "Объясни словами и примерами, что здесь случилось, текстом, пожалуйста",
                     "Сделай видео. Нет, объясни что тут говорят текстом",
                     "что за макароны дай рецепт текстом", "Дайте рецепт в формате текста",
                     "Опиши эту картинку текстом", "Составь план ремонта текстом", "Предложи пример текстом"]:
            with self.subTest(text=text):
                self.assertEqual(response_format(text), "text")

    def test_free_word_order_keeps_negation_topics_quotes_and_later_overrides(self):
        for text in ["Не объясняй что тут говорят текстом", "Объясни что тут говорят, только не текстом",
                     "Объясни как работать с текстом", "Объясни как работать с большим текстом",
                     "Объясни что называют текстом", "Объясни разницу между текстом и видео",
                     'Что значит "объясни что тут говорят текстом"?',
                     "Объясни что тут говорят текстом, а лучше видео",
                     "Не дай рецепт текстом", 'Что означает команда «дай рецепт текстом»?',
                     "Дай пример работы с этим текстом", "Дай рецепт текстом, а лучше видео"]:
            with self.subTest(text=text):
                self.assertEqual(response_format(text), "video")

    def test_explicit_requests_and_common_spelling_variants(self):
        for text in ["Объясни текстом, что такое редьюсер", "обьясни текстом", "обясни текстом", "объяснить текстом",
                     "Ответь текстом", "Объясни мне этот тред текстом", "Объясни, пожалуйста, текстом", "Текстом объясни почему",
                     "Что это? Только текстом.", "Можно текстом?", "Напиши текстом", "Дай текстовый ответ", "Хочу объяснение текстом",
                     "Поясни в текстовом виде", "3d объясни текстом", "Explain in text", "answer in plain text"]:
            with self.subTest(text=text):
                self.assertEqual(response_format(text), "text")

    def test_quoted_negated_and_topical_text_does_not_switch_format(self):
        for text in ["Что это?", "Объясни разницу между текстом и видео", "Не объясняй текстом, сделай видео",
                     "Не хочу текстом", 'Что значит команда "объясни текстом"?', "Что значит «ответь текстом»?",
                     "Прочитай этот код: `объясни текстом`", "> объясни текстом\nЧто он хочет?",
                     "Объясни текстом. Хотя нет, сделай видео", "don't explain in text"]:
            with self.subTest(text=text):
                self.assertEqual(response_format(text), "video")


class TextAnswerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.old = I._state
        I._state = I.State(str(Path(self.directory.name) / "bot.sqlite"))
        self.env = patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test-token", "ANTHROPIC_API_KEY": "test-key", "WEB_RESEARCH": "0"})
        self.env.start()

    def tearDown(self):
        I._state.db.close()
        I._state = self.old
        self.env.stop()
        self.directory.cleanup()

    def job(self, **extra):
        return {"kind": "query", "text": "Объясни текстом редьюсер", "channel_id": "789", "response_channel_id": "789",
                "response_message_id": "400", "prepared_inputs": ([], ["Original source"], []), **extra}

    def test_text_branch_never_calls_video_voice_or_visual_generation(self):
        job = self.job(render_mode="3d", response_format="text")
        with patch.dict(os.environ, {"FULL_GEN": "1", "VISUAL_ASSETS": "1", "WEB_RESEARCH": "1"}), \
                patch.object(pipeline, "collect", return_value=([], ["Source"], [])), \
                patch.object(pipeline, "prepare_sources") as sources, \
                patch.object(T, "answer", return_value="Редьюсер получает состояние и событие и возвращает новое состояние."), \
                patch.object(pipeline, "_http", return_value=(200, b"{}")) as send, \
                patch("fullgen.make_video_fullgen") as fullgen, patch("three_d.make_video_3d") as three_d, \
                patch.object(pipeline, "make_video") as templates, patch.object(pipeline, "voice_all") as voice, \
                patch("assets.prepare_visuals") as visuals, patch.object(pipeline, "discord_video") as video:
            pipeline.handle_job(job)
        for call in (fullgen, three_d, templates, voice, visuals, video):
            call.assert_not_called()
        self.assertIs(sources.call_args.kwargs["visuals_on"], False)
        for call in send.call_args_list:
            self.assertEqual(call.args[2]["content-type"], "application/json")
            self.assertNotIn("attachments", json.loads(call.args[1]))
        saved = I.state().get_explanation("789", "400")
        self.assertEqual(saved["history"][-1]["format"], "text")
        self.assertIn("возвращает новое состояние", saved["history"][-1]["text"])

    def test_plain_model_request_includes_prior_answer_and_source_without_video_instructions(self):
        job = self.job(followup={"history": [{"format": "video", "narration": "Earlier explanation"}]})
        response = {"content": [{"type": "text", "text": "A direct text answer."}], "stop_reason": "end_turn"}
        with patch.object(pipeline, "_http", return_value=(200, json.dumps(response).encode())) as model:
            self.assertEqual(T.answer(job), "A direct text answer.")
        body = json.loads(model.call_args.args[1])
        system = "".join(block["text"] for block in body["system"])
        self.assertIn("TEXT", system)
        prompt = body["messages"][0]["content"][-1]["text"]
        self.assertIn("Earlier explanation", prompt)
        self.assertIn("Original source", prompt)
        self.assertIn("TEXT ONLY", prompt)
        self.assertNotIn("NEW narrated video", prompt)
        self.assertNotIn(pipeline.PLAN_SCHEMA_TEXT, system)

    def test_cut_off_model_reply_is_retried_instead_of_published(self):
        def response(text, stop):
            return 200, json.dumps({"content": [{"type": "text", "text": text}], "stop_reason": stop}).encode()
        with patch.object(pipeline, "_http", side_effect=[response("unfinished", "max_tokens"), response("Complete answer.", "end_turn")]) as model:
            self.assertEqual(T.answer(self.job()), "Complete answer.")
        self.assertEqual(model.call_count, 2)
        first, retry = (json.loads(call.args[1]) for call in model.call_args_list)
        self.assertEqual(first["system"][0], retry["system"][0])
        self.assertNotIn("cache_control", retry["system"][1])

    def test_long_answer_keeps_all_text_and_balanced_code_fences(self):
        code = "```python\n" + "".join(f"    value_{i} = {i}\n" for i in range(350)) + "```\nFinal conclusion."
        parts = T.chunks("Intro.\n\n" + code)
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 1900 for p in parts))
        self.assertTrue(all(p.count("```") % 2 == 0 for p in parts))
        joined = "".join(parts)
        for i in range(350):
            self.assertIn(f"    value_{i} = {i}\n", joined)
        self.assertTrue(joined.endswith("Final conclusion."))
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 1900 for p in T.chunks("😀" * 4000)))

    def test_every_part_of_a_text_answer_recalls_the_same_memory(self):
        text = ("A concrete explanation. " * 230) + "FINAL_SENTENCE"
        calls = []
        def send(url, data, headers, method=None, **kwargs):
            payload = json.loads(data)
            calls.append((url, payload, method))
            return 200, json.dumps({"id": "400" if method == "PATCH" else str(500 + len(calls))}).encode()
        with patch.object(pipeline, "_http", side_effect=send):
            T.deliver(self.job(response_format="text"), text)
        self.assertGreater(len(calls), 1)
        self.assertIn("FINAL_SENTENCE", calls[-1][1]["content"])
        original = I.state().get_explanation("789", "400")
        self.assertEqual(original["history"][0]["text"], text)
        for index, (_, payload, method) in enumerate(calls[1:], 2):
            self.assertEqual(method, "POST")
            self.assertTrue(payload["enforce_nonce"])
            self.assertLessEqual(len(payload["nonce"]), 25)
            self.assertEqual(payload["message_reference"]["message_id"], "400")
            self.assertFalse(payload["allowed_mentions"]["replied_user"])
            self.assertEqual(I.state().get_explanation("789", str(500 + index)), original)
            self.assertIsNone(I.state().get_explanation("other", str(500 + index)))

    def test_private_interaction_continuations_stay_private_and_are_not_indexed(self):
        job = {"application_id": "123", "token": "private-token", "private": True}
        with patch.object(pipeline, "_http", return_value=(200, b"{}")) as send:
            T.deliver(job, "Long private explanation. " * 200)
        self.assertGreater(send.call_count, 1)
        for call in send.call_args_list[1:]:
            self.assertIn("/webhooks/123/private-token", call.args[0])
            self.assertEqual(json.loads(call.args[1])["flags"], 64)
            self.assertNotIn("authorization", call.args[2])
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM explanations").fetchone()[0], 0)

    def test_memory_alias_cycles_are_not_followed(self):
        I.state().put_explanation("789", "1", {"alias_of": "2"})
        I.state().put_explanation("789", "2", {"alias_of": "1"})
        self.assertIsNone(I.state().get_explanation("789", "1"))

    def test_text_failure_does_not_fall_back_to_video(self):
        with patch.object(pipeline, "collect", return_value=([], [], [])), patch.object(T, "answer", side_effect=RuntimeError("model unavailable")), \
                patch.object(pipeline, "_safe_text") as reply, patch.object(pipeline, "discord_video") as video, \
                self.assertLogs("pipeline", level="ERROR"):
            pipeline.handle_job(self.job(response_format="text"))
        video.assert_not_called()
        self.assertIn("ответ текстом", reply.call_args.args[1])
        self.assertNotIn("сделать видео", reply.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
