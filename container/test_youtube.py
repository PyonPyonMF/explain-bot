"""Audio-first YouTube ingestion, cache reuse, opt-in frames and grounding; no network."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import youtube as Y
import inputs
import speech as S
import pipeline as P
import text_answers as T
import followups
import clarifications as C

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
TRANSCRIPT = {"status": "ok", "text": "[00:01–00:03] part1/speaker_0: Example speech.", "partial": False, "language": "eng"}


def page(frames=()):
    return {"id": "jNQXAC9IVRw", "url": URL, "title": "Example video", "duration": 19,
            "transcript": dict(TRANSCRIPT), "frames": list(frames), "coverage": {"sample_count": len(frames)} if frames else None,
            "frame_error": None}


def jpeg(color):
    buf = io.BytesIO(); Image.new("RGB", (100, 100), color).save(buf, "JPEG"); return buf.getvalue()


class YouTubeTests(unittest.TestCase):
    def test_only_canonical_public_youtube_urls_are_accepted(self):
        for url in (URL, "https://youtu.be/jNQXAC9IVRw?t=3", "https://youtube.com/shorts/jNQXAC9IVRw", "https://m.youtube.com/live/jNQXAC9IVRw"):
            self.assertEqual(Y.video_id(url), "jNQXAC9IVRw")
        for url in ("file:///etc/passwd", "https://youtube.com.evil.example/watch?v=jNQXAC9IVRw", "https://user:pass@youtube.com/watch?v=jNQXAC9IVRw",
                    "https://youtube.com:bad/watch?v=jNQXAC9IVRw", "https://youtube.com:8000/watch?v=jNQXAC9IVRw", "https://youtube.com/@channel"):
            self.assertIsNone(Y.video_id(url), url)

    def test_frames_require_the_callers_explicit_non_negated_instruction(self):
        for text in ("разбери покадрово", "посмотри по кадрам 00:01–00:04", "analyze this frame-by-frame"):
            self.assertTrue(Y.frames_requested(text), text)
        for text in ("разбери видео", "summarize " + URL, "не разбирай покадрово", "не надо анализировать по кадрам",
                     'объясни слово "покадрово"', 'что значит покадрово', '> разбери покадрово\nСделай саммари'):
            self.assertFalse(Y.frames_requested(text), text)

    def test_frame_range_has_absolute_timestamps_and_a_visible_sampling_bound(self):
        times, coverage = Y.frame_times("разбери покадрово 01:10–01:13", 120)
        self.assertEqual(len(times), 6)
        self.assertTrue(all(70 <= t <= 73 for t in times))
        self.assertIn("between frames", coverage["notice"])
        self.assertEqual(len(Y.frame_times("покадрово", 3600)[0]), Y.MAX_FRAMES)
        for text in ("покадрово 01:20–00:01", "покадрово 00:99–02:00", "покадрово 03:00–04:00"):
            with self.assertRaises(Y.YouTubeError):
                Y.frame_times(text, 120)

    def test_read_downloads_only_audio_unless_frames_are_requested(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"YOUTUBE_CACHE_DIR": d, "SPEECH_RECOGNITION": "1"}), \
                patch.object(Y, "_metadata", return_value={"duration": 19, "title": "Video"}), \
                patch.object(Y, "_media", return_value=Path(d)/"audio.webm") as media, \
                patch.object(Y, "_transcribe", return_value=TRANSCRIPT), patch.object(Y, "_frames") as frames:
            found = Y.read(URL, {"text": "summarize"})
        self.assertEqual(media.call_args.args[1], "audio")
        self.assertEqual(media.call_count, 1)
        self.assertEqual(found["frames"], [])
        frames.assert_not_called()

    def test_requested_frames_download_a_separate_video_track(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"YOUTUBE_CACHE_DIR": d, "SPEECH_RECOGNITION": "1"}), \
                patch.object(Y, "_metadata", return_value={"duration": 19, "title": "Video"}), \
                patch.object(Y, "_media", return_value=Path(d)/"media.webm") as media, patch.object(Y, "_transcribe", return_value=TRANSCRIPT), \
                patch.object(Y, "_frames", return_value=([{"data": b"image", "seconds": 1}], {"sample_count": 1})) as frames:
            found = Y.read(URL, {"text": "разбери покадрово 00:01–00:03"}, want_frames=True)
        self.assertEqual([c.args[1] for c in media.call_args_list], ["audio", "video"])
        self.assertEqual(len(found["frames"]), 1)
        frames.assert_called_once()

    def test_chunked_asr_resumes_after_failure_and_keeps_global_timestamps(self):
        def ffmpeg(args, **kwargs):
            Path(args[-1]).write_bytes(b"fake audio")
        def transcript(audio):
            return {"text": "words", "language_code": "eng", "words": [
                {"type": "word", "text": "Words.", "start": 1, "end": 2, "speaker_id": "speaker_0"}]}
        with tempfile.TemporaryDirectory() as d, patch.object(Y.subprocess, "run", side_effect=ffmpeg):
            root = Path(d)
            with patch.object(S, "request_transcript", side_effect=[transcript(b""), S.SpeechError("http_503")]):
                first = Y._transcribe(root/"audio", 620, root, {})
            self.assertTrue(first["partial"])
            self.assertEqual(first["seconds"], 300)
            with patch.object(S, "request_transcript", side_effect=transcript) as api:
                finished = Y._transcribe(root/"audio", 620, root, {})
            self.assertEqual(api.call_count, 2)  # Completed first part was recovered from disk.
            self.assertFalse(finished["partial"])
            self.assertEqual(finished["seconds"], 620)
            self.assertIn("[05:01–05:02] part2/speaker_0", finished["text"])
            self.assertIn("[10:01–10:02] part3/speaker_0", finished["text"])
            with patch.object(S, "request_transcript", side_effect=AssertionError("No repeat ASR")):
                self.assertEqual(Y._transcribe(root/"audio", 620, root, {})["text"], finished["text"])

    def test_downloader_subprocess_does_not_receive_api_credentials(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({"id": "jNQXAC9IVRw", "title": "Video", "duration": 19}).encode(), b"")
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-one", "ELEVENLABS_API_KEY": "secret-two", "DISCORD_BOT_TOKEN": "secret-three"}), \
                patch.object(Y.subprocess, "Popen", return_value=process) as spawn:
            Y._download("jNQXAC9IVRw", "metadata", Path(d))
        env = spawn.call_args.kwargs["env"]
        self.assertFalse(any(k in env for k in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "DISCORD_BOT_TOKEN")))
        self.assertNotIn("secret", json.dumps(spawn.call_args.args))

    def test_expired_metadata_invalidates_old_speech_and_media_but_not_other_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            Y._atomic(root/"metadata.json", {"duration": 10})
            os.utime(root/"metadata.json", (0, 0))
            for name in ("audio.webm", "audio.json", "video.mp4", "speech-old-0.json", "keep.txt"):
                (root/name).write_text("old")
            with patch.object(Y, "_download", return_value={"duration": 20, "title": "Updated"}):
                self.assertEqual(Y._metadata("jNQXAC9IVRw", root)["duration"], 20)
            self.assertFalse((root/"audio.webm").exists())
            self.assertFalse((root/"speech-old-0.json").exists())
            self.assertTrue((root/"keep.txt").exists())

    def test_audio_collection_ignores_thumbnails_and_ambient_images(self):
        job = {"kind": "query", "text": "summarize " + URL, "channel_id": "1", "context_messages": 10,
               "message": {"content": URL, "embeds": [{"url": URL, "thumbnail": {"url": "https://example.org/thumbnail.jpg"}}]}}
        with patch.object(Y, "read", return_value=page()) as read, patch.object(inputs, "fetch") as fetch, \
                patch.object(inputs, "_history") as history, patch("links.read_link") as ordinary:
            images, texts, _ = inputs.collect(job)
        self.assertEqual(images, [])
        self.assertTrue(job["primary_youtube"])
        self.assertFalse(read.call_args.kwargs["want_frames"])
        self.assertIn("Example speech", "\n".join(texts))
        fetch.assert_not_called(); history.assert_not_called(); ordinary.assert_not_called()

    def test_explicit_frames_are_timestamped_and_do_not_invoke_regular_asr_again(self):
        frames = [{"data": jpeg("red"), "seconds": 10}, {"data": jpeg("blue"), "seconds": 11}]
        job = {"kind": "query", "text": "разбери покадрово 00:10–00:12 " + URL}
        with patch.object(Y, "read", return_value=page(frames)) as read, patch.object(S, "transcribe") as ordinary:
            images, texts, _ = inputs.collect(job)
        self.assertTrue(read.call_args.kwargs["want_frames"])
        self.assertEqual(len(images), 2)
        self.assertIn("10.000 seconds", "\n".join(texts))
        ordinary.assert_not_called()

    def test_duplicate_youtube_urls_are_processed_only_once(self):
        job = {"kind": "query", "text": "summarize " + URL + " https://youtu.be/jNQXAC9IVRw?t=5"}
        with patch.object(Y, "read", return_value=page()) as read:
            _, texts, _ = inputs.collect(job)
        read.assert_called_once()
        self.assertEqual(len(S.read_transcripts(texts)), 1)

    def test_second_video_does_not_download_frames_after_visual_budget_is_full(self):
        frames = [{"data": jpeg("red"), "seconds": i} for i in range(Y.MAX_FRAMES)]
        job = {"kind": "query", "text": "разбери покадрово " + URL + " https://youtu.be/_0G4OG39XjI"}
        with patch.object(Y, "read", side_effect=[page(frames), page()]) as read:
            images, texts, _ = inputs.collect(job)
        self.assertEqual(len(images), Y.MAX_FRAMES)
        self.assertFalse(read.call_args_list[1].kwargs["want_frames"])
        self.assertTrue(any("лимит кадров" in error for error in job["youtube_errors"]))

    def test_failed_youtube_never_becomes_a_thumbnail_explanation(self):
        job = {"kind": "query", "text": "объясни " + URL}
        with patch.object(Y, "read", side_effect=Y.YouTubeError("access")), patch.object(P, "_safe_text") as reply, \
                patch.object(P, "prepare_sources") as research, patch("fullgen.make_video_fullgen") as render:
            P.handle_job(job)
        self.assertIn("не разрешил", reply.call_args.args[1])
        research.assert_not_called(); render.assert_not_called()

    def test_explicit_youtube_analyze_is_not_an_ambiguous_action_picker(self):
        self.assertIsNone(C.reason("разбери " + URL, has_material=True))
        self.assertIsNotNone(C.reason("разбери это", has_material=True))

    def test_followup_requests_frames_of_the_original_youtube_without_asking_for_the_url_again(self):
        source = {"primary_youtube": True, "youtube_sources": [URL], "images": [], "texts": ["<media_transcript status='ok'><transcript_text>Saved speech</transcript_text></media_transcript>"]}
        job = {"kind": "query", "text": "разбери покадрово 00:01–00:04", "followup": {"source": source}}
        def collect(fresh):
            self.assertIn(URL, fresh["text"])
            fresh.update(primary_youtube=True, youtube_frame_analysis=True, youtube_sources=[URL])
            return [], ["<youtube_visual_samples>Fresh frames</youtube_visual_samples>"], []
        _, texts, _ = followups.prepare(job, collect)
        self.assertTrue(job["youtube_frame_analysis"])
        self.assertIn("Saved speech", "\n".join(texts))

    def test_default_followup_reuses_speech_without_images_or_a_second_download(self):
        source = {"primary_youtube": True, "youtube_sources": [URL], "images": [{"source": {"data": "eA=="}}],
                  "texts": ["<media_transcript status='ok'><transcript_text>Saved speech</transcript_text></media_transcript>",
                            "<youtube_visual_samples>Old frames</youtube_visual_samples>"]}
        job = {"kind": "query", "text": "summarize", "followup": {"source": source}}
        def collect(fresh):
            self.assertNotIn(URL, fresh["text"])
            return [], [], []
        images, texts, _ = followups.prepare(job, collect)
        self.assertEqual(images, [])
        self.assertNotIn("Old frames", "\n".join(texts))
        self.assertTrue(job["primary_youtube"])

    def test_summary_receives_frames_only_after_explicit_opt_in(self):
        block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "eA=="}}
        texts = ["<media_transcript status='ok'><transcript_text>Speech</transcript_text></media_transcript>",
                 "<youtube_visual_samples>Sample timestamps</youtube_visual_samples>"]
        for enabled in (False, True):
            job = {"kind": "query", "text": "summarize", "text_task": "summary", "youtube_frame_analysis": enabled,
                   "prepared_inputs": ([block], texts, [])}
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), patch.object(P, "_http", return_value=(200,json.dumps({
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Summary."}]}).encode())) as http:
                T.answer(job)
            body = json.loads(http.call_args.args[1])
            self.assertEqual(any(b["type"] == "image" for b in body["messages"][0]["content"]), enabled)


if __name__ == "__main__":
    unittest.main()
