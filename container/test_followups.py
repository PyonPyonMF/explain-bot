"""Persistent video follow-ups: narration, isolated branches, no credentials and no text-only success path."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import followups
import interactions as I
import pipeline


def source_job(message="400", text="Explain the state machine"):
    return {"kind": "query", "text": text, "channel_id": "789", "response_channel_id": "789",
            "response_message_id": message, "prepared_inputs": ([], ["Original source: state and event produce a new state."], []),
            "token": "interaction-secret-must-not-be-stored", "DISCORD_BOT_TOKEN": "bot-secret-must-not-be-stored"}


class FollowupMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "bot.sqlite")
        self.old = I._state
        I._state = I.State(self.path)
        if os.name == "posix":
            self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)

    def tearDown(self):
        I._state.db.close()
        I._state = self.old
        self.tmp.cleanup()

    def root(self):
        job = source_job()
        followups.record_script(job, {"title": "State machine", "scenes": [{"heading": "Idea", "narration": "The reducer handles one event."}]})
        followups.remember(job)
        return I.state().get_explanation("789", "400")

    def test_memory_survives_reopen_and_is_scoped_to_channel_and_video(self):
        expected = self.root()
        I.state().db.close()
        I._state = I.State(self.path)
        self.assertEqual(I.state().get_explanation("789", "400"), expected)
        self.assertIsNone(I.state().get_explanation("456", "400"))
        self.assertIsNone(I.state().get_explanation("789", "401"))
        self.assertIn("The reducer handles one event.", expected["history"][0]["narration"])

    def test_serialized_memory_excludes_delivery_tokens_and_generated_code(self):
        job = source_job()
        job["animation_code"] = "must-not-save-executable-code"
        followups.record_script(job, {"title": "Source", "scenes": [{"narration": "Spoken explanation"}], "code": "do not save"})
        followups.remember(job)
        raw = I.state().db.execute("SELECT payload FROM explanations").fetchone()[0]
        for secret in [job["token"], job["DISCORD_BOT_TOKEN"], job["animation_code"]]:
            self.assertNotIn(secret, raw)
        self.assertNotIn('"token"', raw)

    def test_failed_video_upload_does_not_create_successful_memory(self):
        job = source_job()
        followups.record_script(job, {"title": "Source", "scenes": [{"narration": "Spoken explanation"}]})
        path = Path(self.tmp.name) / "clip.mp4"
        path.write_bytes(b"video")
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test-token"}), \
                patch.object(pipeline, "_http", side_effect=RuntimeError("upload failed")):
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                pipeline.discord_video(job, path, "Source")
        self.assertIsNone(I.state().get_explanation("789", "400"))

    def test_followup_runs_video_pipeline_and_saves_new_narration_after_upload(self):
        original = self.root()
        job = source_job("401", "Show a concrete booking app example")
        job.update(followup=original, followup_message_id="400", message={"content": job["text"]})
        job.pop("prepared_inputs")
        path = Path(self.tmp.name) / "clip.mp4"
        path.write_bytes(b"new-video-content")
        plan = {"title": "Booking app", "scenes": [{"heading": "Reserve", "narration": "A reserve event moves the booking from draft to confirmed."}]}
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test-token", "FULL_GEN": "0", "VISUAL_ASSETS": "0", "WEB_RESEARCH": "0"}), \
                patch.object(pipeline, "collect", return_value=([], [], [])) as collect, \
                patch.object(pipeline, "make_plan", return_value=plan), patch.object(pipeline, "voice_all", return_value=[]), \
                patch.object(pipeline, "render_all", return_value=(str(path), 1.0)) as render, \
                patch.object(pipeline, "_http", return_value=(200, b"{}")) as send:
            pipeline.handle_job(job)
        render.assert_called_once()
        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[0]["context_messages"], 0)
        self.assertIn(b"new-video-content", send.call_args.args[1])
        delivered = json.loads(send.call_args.args[1].split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0])
        self.assertIn("draft to confirmed", delivered["content"])
        saved = I.state().get_explanation("789", "401")
        self.assertEqual(saved["root_id"], "400")
        self.assertEqual(saved["parent_id"], "400")
        self.assertEqual(len(saved["history"]), 2)
        self.assertIn("draft to confirmed", saved["history"][-1]["narration"])
        self.assertEqual(I.state().get_explanation("789", "400"), original)
        _, prompt = pipeline.build_user_content(job, *job["prepared_inputs"][:2])
        self.assertIn("The reducer handles one event.", prompt)
        self.assertIn("Show a concrete booking app example", prompt)
        self.assertIn("NEW narrated video", prompt)

    def test_webhook_responses_are_not_indexed_as_public_mention_replies(self):
        job = source_job()
        job.pop("response_message_id")
        followups.record_script(job, {"title": "Private", "scenes": [{"narration": "Private response"}]})
        followups.remember(job)
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM explanations").fetchone()[0], 0)

    def test_history_is_bounded_and_keeps_root_and_latest_turn(self):
        context = self.root()
        for i in range(12):
            job = source_job(str(500 + i), f"Question {i}")
            job["followup"] = context
            followups.record_script(job, {"title": f"Answer {i}", "scenes": [{"narration": f"Narration {i}"}]})
            followups.remember(job)
            context = I.state().get_explanation("789", str(500 + i))
        self.assertEqual(len(context["history"]), 8)
        self.assertEqual(context["history"][0]["title"], "State machine")
        self.assertEqual(context["history"][-1]["title"], "Answer 11")
        self.assertTrue(context["history_truncated"])

    def test_retention_limits_number_and_age_of_saved_videos(self):
        for i in range(101):
            I.state().put_explanation("789", str(i), {"title": str(i)})
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM explanations").fetchone()[0], 100)
        self.assertIsNone(I.state().get_explanation("789", "0"))
        I.state().db.execute("UPDATE explanations SET created=0")
        I.state().db.commit()
        self.assertIsNone(I.state().get_explanation("789", "100"))
        I.state().put_explanation("789", "new", {"title": "new"})
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM explanations").fetchone()[0], 1)

    def test_truncated_source_and_image_listing_are_explicit_and_aligned(self):
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abcdefgh"}}
        job = source_job()
        job["prepared_inputs"] = ([image, image], ["12345", "not retained", '<attached_images>old listing</attached_images>'], [])
        job["image_meta"] = [{"caption": "First image"}, {"caption": "Second image"}]
        followups.record_script(job, {"title": "Source", "scenes": [{"narration": "Spoken"}]})
        with patch.object(followups, "MAX_SOURCE_CHARS", 5), patch.object(followups, "MAX_IMAGE_CHARS", 8):
            followups.remember(job)
        saved = I.state().get_explanation("789", "400")
        self.assertTrue(saved["source"]["truncated"])
        child = {"kind": "query", "text": "Explain", "followup": saved}
        images, texts, _ = followups.prepare(child, lambda fresh: ([], [], []))
        self.assertEqual(len(images), 1)
        self.assertIn("First image", "\n".join(texts))
        self.assertNotIn("Second image", "\n".join(texts))
        self.assertNotIn("old listing", "\n".join(texts))
        self.assertIn("source_memory_limit", "\n".join(texts))

    def test_legacy_video_uses_original_request_and_marks_missing_script(self):
        context = {"source_job": {"kind": "query", "text": "https://example.com/original", "context_messages": 10},
                   "history": [{"title": "Old video", "script_available": False, "narration": ""}]}
        job = {"kind": "query", "text": "Show an example", "followup": context}
        seen = []
        def collect(request):
            seen.append(request)
            return [], ["Original source text"] if "original" in request.get("text", "") else [], []
        _, texts, _ = followups.prepare(job, collect)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(s["context_messages"] == 0 for s in seen))
        self.assertIn("Original source text", texts)
        self.assertIn('"script_available": false', followups.prompt(job))


if __name__ == "__main__":
    unittest.main()
