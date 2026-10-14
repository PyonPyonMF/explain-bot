"""Photo enhancement: request detection, the worker request, honest labeling, upload size and failure replies."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import image_enhance as E
import pipeline
from render_modes import response_format, text_task


def png(width=300, height=200, color=(90, 90, 90)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


class DetectionTests(unittest.TestCase):
    def test_enhancement_requests(self):
        for text in ["джарвис enhance", "Jarvis, enhance!", "энханс", "улучши фото", "улучши качество этой картинки",
                     "сделай фото четче", "апскейл пожалуйста", "upscale this", "раскрась это фото", "восстанови старое фото",
                     "повысь качество картинки", "увеличь разрешение"]:
            with self.subTest(text=text):
                self.assertEqual((text_task(text), response_format(text)), ("enhance", "image"))

    def test_questions_about_the_words_and_other_requests_are_not_enhancements(self):
        for text in ["объясни, что такое upscale", "что значит enhance", "расскажи про апскейл", "what is upscale",
                     "не надо enhance, просто объясни", "улучши качество кода", "объясни текстом, что на фото"]:
            with self.subTest(text=text):
                self.assertNotEqual(text_task(text), "enhance")

    def test_colorize_option(self):
        self.assertTrue(E.colorize_requested("раскрась это фото"))
        self.assertTrue(E.colorize_requested("улучши фото и сделай в цвете"))
        self.assertFalse(E.colorize_requested("джарвис enhance"))
        self.assertFalse(E.colorize_requested("улучши «раскрась» фото"))  # quoted words are not instructions


class RunTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.job = {"kind": "query", "text": "джарвис enhance", "response_channel_id": "1", "response_message_id": "2",
                    "image_originals": [png(640, 480)]}
        self.images = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(png(64, 48)).decode()}}]
        patcher = patch.object(pipeline, "_safe_text")
        self.progress = patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_sends_only_the_photo_and_labels_the_result(self):
        with patch("assets.request_codex", return_value=(png(1280, 960), {"status": "ok"})) as codex, \
                patch.object(pipeline, "discord_file") as upload:
            E.run(self.job, self.images, self.work)
        prompt, query, references = codex.call_args.args
        self.assertEqual((prompt, query), ("", ""))  # the request text never leaves for the worker
        self.assertEqual(codex.call_args.kwargs["mode"], "enhance")
        self.assertEqual(codex.call_args.kwargs["options"], {"colorize": False})
        with Image.open(references[0]["path"]) as sent:
            self.assertEqual(sent.size, (640, 480))  # native resolution, not the small vision copy
        path, filename, kind, content = upload.call_args.args[1:]
        self.assertEqual((filename, kind), ("enhanced.png", "image/png"))
        self.assertIn(E.strings()["label"], content)
        self.assertIn("640×480 → 1280×960", content)

    def test_failures_are_explained(self):
        cases = [((RuntimeError("Codex image worker is unavailable"),), E.strings()["off"]),
                 ((TimeoutError(),), E.strings()["failed"].format(reason="")),
                 (((None, {"status": "ok", "description": "content would change"}),), "content would change")]
        for effect, expected in cases:
            with self.subTest(expected=expected), patch("assets.request_codex", side_effect=effect if isinstance(effect[0], Exception) else None,
                                                        return_value=effect[0] if not isinstance(effect[0], Exception) else None), \
                    self.assertRaises(pipeline.UserError) as error:
                E.run(self.job, self.images, self.work)
            self.assertIn(expected, str(error.exception))
        with self.assertRaises(pipeline.UserError) as error:
            E.run({"text": "enhance"}, [], self.work)
        self.assertEqual(str(error.exception), E.strings()["no_image"])
        with patch.dict(os.environ, {"IMAGE_ENHANCE": "0"}), self.assertRaises(pipeline.UserError) as error:
            E.run(self.job, self.images, self.work)
        self.assertEqual(str(error.exception), E.strings()["off"])

    def test_large_results_fit_the_upload_limit(self):
        noisy = Image.effect_noise((1600, 1200), 80).convert("RGB")
        buffer = io.BytesIO()
        noisy.save(buffer, "PNG")
        limit = 400_000
        path, kind, width, height = E.fit_upload(buffer.getvalue(), self.work, limit)
        self.assertEqual(kind, "image/jpeg")
        self.assertLessEqual(Path(path).stat().st_size, limit)
        self.assertLess(width, 1600)
        small, kind, *_ = E.fit_upload(png(), self.work, limit)
        self.assertEqual(kind, "image/png")

    def test_handle_job_routes_enhancement_before_any_explanation(self):
        job = {"kind": "query", "text": "джарвис enhance", "response_channel_id": "1", "response_message_id": "2"}
        with patch("followups.prepare", return_value=(self.images, [], [])), patch("image_enhance.run") as run, \
                patch.object(pipeline, "prepare_sources") as research, patch("router.observe"):
            pipeline.handle_job(job)
        run.assert_called_once()
        research.assert_not_called()


class WorkerRequestTests(unittest.TestCase):
    def test_request_carries_mode_and_options(self):
        with tempfile.TemporaryDirectory() as queue:
            source = Path(queue) / "photo.jpg"
            Image.new("RGB", (10, 10)).save(source)
            seen = {}

            def fake_sleep(_):
                job = next(Path(queue).glob("*/request.json"))
                seen.update(json.loads(job.read_text()))
                (job.parent / "response.json").write_text(json.dumps({"status": "ok", "image": None}))

            import assets
            with patch.dict(os.environ, {"CODEX_VISUAL_QUEUE": queue, "CODEX_VISUAL_UID": str(os.getuid())}), \
                    patch("time.sleep", side_effect=fake_sleep):
                assets.request_codex("", "", [{"path": str(source)}], mode="enhance", options={"colorize": True})
        self.assertEqual((seen["mode"], seen["options"], seen["prompt"]), ("enhance", {"colorize": True}, ""))
        self.assertEqual(seen["references"], ["reference-0.jpg"])


if __name__ == "__main__":
    unittest.main()
