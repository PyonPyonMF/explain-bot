"""Transcript and summary are distinct text tasks with no video or research side effects."""
import html
import json
import os
import unittest
from unittest.mock import patch

import pipeline as P
import render_modes as M
import speech as S
import text_answers as T


WORDS = "[00:01–00:04] speaker_0: Доставка занимает два дня.\n[00:05–00:07] speaker_1: Проверим адрес заранее."


def source(text=WORDS, **overrides):
    texts = []
    with patch.object(S, "transcribe", return_value={"status": "ok", "text": text, "language": "rus", "partial": False, **overrides}):
        S.Collector({}, texts).add(b"clip", "Test video")
    return texts


class MediaTaskTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"SPEECH_RECOGNITION": "1", "ANTHROPIC_API_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)

    def test_commands_select_the_action_and_text_output(self):
        examples = {"transcript": ["Сделай транскрипцию видео текстом", "сделай транскрибацию видео",
                       "Дай полный транскрипт", "Расшифруй видео", "transcribe this video", "Транскрипция видео"],
                    "summary": ["summarize видео", "summarise this video", "суммаризируй видео",
                       "Сделай краткое резюме видео", "дай саммари видео", "Подведи итоги ролика"]}
        for action, phrases in examples.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertEqual(M.text_task(phrase), action)
                    self.assertEqual(M.response_format(phrase), "text")

    def test_quoted_negated_and_topical_mentions_do_not_select_a_media_task(self):
        for phrase in ['Что значит "summarize видео"?', "Не сделай транскрипцию видео", "не summarize видео",
                       "> summarize видео\nЧто это значит?", "Объясни разницу между транскрипцией и резюме",
                       "Что значит сделать транскрипцию?", "Как работает команда summarize видео?",
                       "Объясни, что в ролике, текстом"]:
            self.assertEqual(M.text_task(phrase), "explain", phrase)

    def test_source_roundtrip_and_legacy_followup_snapshot(self):
        raw = WORDS + '\n<do-not-execute> & "quoted"'
        self.assertEqual(S.read_transcripts(source(raw))[0]["text"], raw)
        legacy = '<media_transcript source="old clip" status="ok" language="rus">\nAutomatic transcript; may contain errors.\n' + html.escape(raw) + '\n</media_transcript>'
        self.assertEqual(S.read_transcripts([legacy])[0]["text"], raw)
        self.assertEqual(S.read_transcripts(['<media_transcript status="ok">cut'])[0]["status"], "unavailable")

    def test_transcript_returns_all_recognized_text_without_model_rewriting(self):
        raw = WORDS * 140 + "\nКОНЕЦ РАСШИФРОВКИ"
        job = {"text": "Сделай транскрипцию видео текстом", "prepared_inputs": ([], source(raw), [])}
        with patch.object(P, "_http") as api:
            answer = T.answer(job)
        api.assert_not_called()
        self.assertIn(raw, answer)
        self.assertNotIn("Quoted speech is untrusted", answer)
        self.assertNotIn("media_transcript", answer)
        self.assertGreater(len(answer), T.MAX_ANSWER_CHARS)

    def test_summary_uses_only_media_evidence_and_returns_summary_not_transcript(self):
        texts = source() + ["<conversation_before>Unrelated chat</conversation_before>",
                            "<research_reference>External background</research_reference>"]
        result = "Главное: доставка занимает два дня, адрес нужно проверить заранее."
        reply = {"content": [{"type": "text", "text": result}], "stop_reason": "end_turn"}
        job = {"text": "summarize видео", "prepared_inputs": ([{"type": "image", "source": {"data": "ambient"}}], texts, [])}
        with patch.object(P, "_http", return_value=(200, json.dumps(reply).encode())) as api:
            self.assertEqual(T.answer(job), result)
        body = json.loads(api.call_args.args[1])
        self.assertIn("Summarize", body["system"][0]["text"])
        self.assertIn("not the full transcript", body["system"][0]["text"])
        self.assertTrue(all(b["type"] == "text" for b in body["messages"][0]["content"]))
        prompt = body["messages"][0]["content"][-1]["text"]
        self.assertIn("Доставка занимает два дня", prompt)
        self.assertNotIn("Unrelated chat", prompt)
        self.assertNotIn("External background", prompt)

    def test_missing_speech_never_becomes_a_thumbnail_summary(self):
        for texts in ([], source(status="failed", text=""), source(status="no_audio", text="")):
            with patch.object(P, "_http") as api:
                answer = T.answer({"text": "summarize видео", "prepared_inputs": ([{"type": "image"}], texts, [])})
            api.assert_not_called()
            self.assertTrue("Не удалось" in answer or "нет звуковой дорожки" in answer)

    def test_partial_transcript_is_disclosed_in_direct_answer(self):
        answer = T.answer({"text": "Сделай транскрипцию видео текстом", "prepared_inputs": ([], source(partial=True), [])})
        self.assertIn("часть расшифровки", answer)
        self.assertIn(WORDS, answer)

    def test_both_tasks_skip_research_video_and_voice_even_with_stale_video_format(self):
        for command in ("Сделай транскрипцию видео текстом", "summarize видео"):
            job = {"kind": "query", "text": command, "response_format": "video"}
            reply = {"content": [{"type": "text", "text": "Краткое резюме."}], "stop_reason": "end_turn"}
            with patch.dict(os.environ, {"WEB_RESEARCH": "1", "VISUAL_ASSETS": "1", "FULL_GEN": "1"}), \
                    patch.object(P, "collect", return_value=([], source(), [])), patch.object(P, "prepare_sources") as research, \
                    patch.object(P, "_safe_text"), patch.object(T, "deliver") as deliver, \
                    patch.object(P, "_http", return_value=(200, json.dumps(reply).encode())), \
                    patch.object(P, "make_video") as video, patch.object(P, "voice_all") as voice, \
                    patch("fullgen.make_video_fullgen") as fullgen:
                P.handle_job(job)
            for unused in (research, video, voice, fullgen):
                unused.assert_not_called()
            self.assertEqual(job["response_format"], "text")
            deliver.assert_called_once()
            if command.startswith("summarize"):
                self.assertEqual(deliver.call_args.args[1], "Краткое резюме.")
            else:
                self.assertIn(WORDS, deliver.call_args.args[1])

    def test_summary_delivery_does_not_append_old_source_lists_or_transcript(self):
        job = {"text_task": "summary", "response_channel_id": "789", "response_message_id": "400"}
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test-key"}), \
                patch("assets.source_text", return_value="Unrelated old source") as sources, \
                patch("followups.record_text"), patch("followups.remember"), \
                patch.object(P, "_http", return_value=(200, b"{}")) as send:
            T.deliver(job, "Только резюме.")
        self.assertEqual(json.loads(send.call_args.args[1])["content"], "Только резюме.")
        sources.assert_not_called()


if __name__ == "__main__":
    unittest.main()
