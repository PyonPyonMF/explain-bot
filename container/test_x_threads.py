"""Author threads, incomplete reads and media from later posts; no live network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fullgen
import inputs
import links
import pipeline
import three_d


def post(mid, text, parent=None, handle="writer", **extra):
    return {"type": "status", "id": str(mid), "url": f"https://x.com/{handle}/status/{mid}", "text": text,
            "author": {"id": "1" if handle == "writer" else "2", "name": handle, "screen_name": handle},
            "replying_to": {"screen_name": handle, "status": str(parent)} if parent else None, **extra}


def payload(rows, focal=None, **extra):
    return {"code": 200, "status": focal or rows[0], "thread": rows, **extra}


class ThreadReaderTests(unittest.TestCase):
    def test_middle_link_reads_ordered_deduplicated_author_chain_and_quotes_stay_separate(self):
        rows = [post(100, "Opening argument"), post(200, "Method", 100), post(300, "Conclusion https://example.com/source", 200)]
        rows[1]["quote"] = post(250, "Someone else's claim", handle="commenter")
        data = payload([rows[2], rows[1], rows[0], rows[1], post(260, "Unrelated reply", 100, handle="commenter")], rows[1])
        with patch.object(links, "_json", return_value=data) as fetch:
            result = links.read_link("https://x.com/writer/status/200")
        fetch.assert_called_once_with("https://api.fxtwitter.com/2/thread/200")
        self.assertEqual(result["kind"], "x_thread")
        self.assertEqual(result["thread"]["post_ids"], ["100", "200", "300"])
        text = result["text"]
        self.assertEqual(text.count("Opening argument"), 1)
        self.assertLess(text.index("Opening argument"), text.index("Method"))
        self.assertIn("Quoted post by commenter", text)
        self.assertNotIn("Unrelated reply", text)
        self.assertIn('requested="true"', text)
        self.assertEqual(result["links"], ["https://example.com/source"])
        self.assertEqual(result["thread"]["warnings"], [])

    def test_short_videos_from_later_posts_keep_parent_links_and_preview_fallback(self):
        rows = [post(100, "Root"), post(200, "Animated diagram", 100, media={"all": [
            {"type": "video", "format": "video/mp4", "duration": 5, "url": "https://video.example/clip.mp4",
             "thumbnail_url": "https://image.example/preview.jpg"}]})]
        with patch.object(links, "_json", return_value=payload(rows)):
            result = links.read_link("https://x.com/writer/status/100")
        media = result["images"][0]
        self.assertEqual(media["url"], "https://video.example/clip.mp4")
        self.assertEqual(media["fallback_url"], "https://image.example/preview.jpg")
        self.assertEqual(media["source_url"], "https://x.com/writer/status/200")
        self.assertIn("Thread post 2", media["caption"])
        self.assertIn("In reply to @writer", result["text"])
        self.assertNotIn("{'screen_name'", result["text"])

    def test_deleted_posts_and_missing_parents_are_explicit_gaps(self):
        rows = [post(100, "Root"), {"type": "tombstone", "id": "200", "reason": "deleted"}, post(300, "Later claim", 200)]
        with patch.object(links, "_json", return_value=payload(rows)):
            result = links.read_x("https://x.com/writer/status/100")
        self.assertIn("thread_gap", result["thread"]["warnings"])
        self.assertIn("Unavailable post IDs: 200", result["text"])
        self.assertIn("Later claim", result["text"])

    def test_thread_is_not_cut_to_generic_page_6000_character_limit(self):
        rows = [post(100, "Beginning " * 650), post(200, "Critical final conclusion", 100)]
        with patch.object(links, "_json", return_value=payload(rows)):
            result = links.read_link("https://x.com/writer/status/100")
        self.assertGreater(len(result["text"]), 6000)
        self.assertIn("Critical final conclusion", result["text"])

    def test_post_limit_keeps_requested_post_and_discloses_omissions(self):
        rows = [post(100 * n, f"Post {n}", 100 * (n - 1) if n > 1 else None) for n in range(1, 7)]
        with patch.object(links, "MAX_THREAD_POSTS", 3), patch.object(links, "_json", return_value=payload(rows, rows[-1])):
            result = links.read_link("https://x.com/writer/status/600")
        self.assertEqual(result["thread"]["post_ids"], ["100", "200", "600"])
        self.assertIn("thread_limit", result["thread"]["warnings"])
        self.assertIn("Post 6", result["text"])

    def test_character_limit_cannot_silently_hide_the_requested_post(self):
        rows = [post(100, "Long text " * 1000), post(200, "Requested conclusion", 100)]
        with patch.object(links, "MAX_THREAD_CHARS", 2000), patch.object(links, "_json", return_value=payload(rows, rows[-1])):
            result = links.read_link("https://x.com/writer/status/200")
        self.assertIn("Requested conclusion", result["text"])
        self.assertIn("thread_limit", result["thread"]["warnings"])

    def test_single_post_fallback_never_claims_a_complete_thread(self):
        def fetch(url, **kwargs):
            if "/2/thread/" in url:
                raise OSError("thread service unavailable")
            return {"code": 200, "tweet": post(100, "Still readable")}
        with patch.object(links, "_json", side_effect=fetch):
            result = links.read_x("https://x.com/writer/status/100")
        self.assertEqual(result["kind"], "x_post")
        self.assertIn("thread_unavailable", result["thread"]["warnings"])
        self.assertIn("Only the linked post", result["text"])


class ThreadPipelineTests(unittest.TestCase):
    def test_direct_thread_has_up_to_12_visual_inputs_and_no_unrelated_discord_history(self):
        page = {"kind": "x_thread", "text": "A long author thread", "links": [],
                "images": [{"url": f"https://example.com/{n}.png", "source_url": f"https://x.com/writer/status/{100+n}"} for n in range(13)],
                "thread": {"post_ids": ["100", "200"], "warnings": []}}
        job = {"kind": "query", "text": "Explain https://x.com/writer/status/100", "context_messages": 10, "channel_id": "5"}
        with patch.object(links, "read_link", return_value=page), patch.object(inputs, "_history") as history, \
                patch.object(inputs, "fetch", side_effect=lambda url, **kw: (url.encode(), "image/png")), \
                patch.object(inputs, "storyboard", return_value=None), patch.object(inputs, "_fingerprint", side_effect=lambda b: b), \
                patch.object(inputs, "image_block", side_effect=lambda data, ct: {"type": "image", "source": {"data": data.decode()}}):
            images, texts, _ = inputs.collect(job)
        history.assert_not_called()
        self.assertEqual(len(images), 12)
        self.assertIn("thread_media", job["source_warnings"])
        self.assertIn("Only the listed author posts", "\n".join(texts))

    def test_fullgen_keeps_source_images_beyond_the_renderer_asset_shortlist(self):
        images = [{"type": "image", "source": {"data": f"post-{n}"}} for n in range(12)]
        job = {"kind": "query", "text": "Explain the thread", "primary_x": True,
               "prepared_inputs": (images, ["Thread text"], []), "assets": [{"id": "visual_1"}]}
        with tempfile.TemporaryDirectory() as directory, patch("assets.visual_content", return_value=[{"type": "text", "text": "Renderer asset"}]), \
                patch.object(fullgen, "claude", side_effect=RuntimeError("stop before rendering")) as model:
            with self.assertRaisesRegex(RuntimeError, "stop before rendering"):
                fullgen.make_video_fullgen(job, directory, lambda _: None)
        self.assertEqual(model.call_args.args[1][:12], images)

    def test_partial_thread_notice_is_in_the_actual_discord_video_payload(self):
        job = {"application_id": "1", "token": "test", "source_warnings": ["thread_gap", "thread_media"]}
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "clip.mp4"
            video.write_bytes(b"video")
            with patch.object(pipeline, "_http", return_value=(200, b"{}")) as send:
                pipeline.discord_video(job, video, "Explanation")
        body = send.call_args.args[1]
        payload = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0])
        self.assertIn("Тред прочитан частично", payload["content"])
        self.assertIn("Не все вложения", payload["content"])

    def test_3d_planner_also_receives_later_posts_media(self):
        images = [{"type": "image", "source": {"data": f"post-{n}"}} for n in range(12)]
        job = {"kind": "query", "text": "Explain the thread", "primary_x": True, "prepared_inputs": (images, ["Thread text"], [])}
        with tempfile.TemporaryDirectory() as directory, patch.object(three_d, "avatar_info", return_value=(None, "")), \
                patch.object(three_d, "models_catalog", return_value=[]), \
                patch.object(fullgen, "claude", side_effect=RuntimeError("stop before rendering")) as model:
            with self.assertRaisesRegex(RuntimeError, "stop before rendering"):
                three_d.make_video_3d(job, directory, lambda _: None)
        self.assertEqual(model.call_args.args[1][:12], images)


if __name__ == "__main__":
    unittest.main()
