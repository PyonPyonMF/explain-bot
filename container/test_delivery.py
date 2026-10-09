"""Video/progress delivery via bot messages and existing interaction webhooks."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pipeline


class DeliveryTests(unittest.TestCase):
    def test_mention_progress_edits_bot_reply_with_bot_auth(self):
        job = {"response_channel_id": "789", "response_message_id": "400"}
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test-token"}), patch.object(pipeline, "_http", return_value=(200, b"{}")) as http:
            pipeline.discord_text(job, "Rendering")
        args, kwargs = http.call_args
        self.assertTrue(args[0].endswith("/channels/789/messages/400"))
        self.assertEqual(args[2]["authorization"], "Bot test-token")
        self.assertEqual(kwargs["method"], "PATCH")
        self.assertEqual(json.loads(args[1])["allowed_mentions"], {"parse": []})

    def test_mention_video_attached_to_same_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"test mp4 bytes")
            with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test-token"}), patch.object(pipeline, "_http", return_value=(200, b"{}")) as http:
                pipeline.discord_video({"response_channel_id": "789", "response_message_id": "400"}, str(path), "Title")
        args, kwargs = http.call_args
        self.assertTrue(args[0].endswith("/channels/789/messages/400"))
        self.assertEqual(args[2]["authorization"], "Bot test-token")
        self.assertIn(b'filename="explain.mp4"', args[1])
        self.assertIn(b"test mp4 bytes", args[1])
        self.assertEqual(kwargs["method"], "PATCH")

    def test_slash_delivery_still_uses_interaction_webhook(self):
        with patch.object(pipeline, "_http", return_value=(200, b"{}")) as http:
            pipeline.discord_text({"application_id": "456", "token": "interaction-token"}, "Rendering")
        args, _ = http.call_args
        self.assertTrue(args[0].endswith("/webhooks/456/interaction-token/messages/@original"))
        self.assertNotIn("authorization", args[2])


if __name__ == "__main__":
    unittest.main()
