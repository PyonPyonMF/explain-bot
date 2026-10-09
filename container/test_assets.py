"""Asset acquisition, rendering, and queue boundaries without paid API calls."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
import matplotlib.pyplot as plt

import assets
import fullgen
import kit


def png():
    out = io.BytesIO()
    Image.new("RGB", (256, 128), "orange").save(out, "PNG")
    return out.getvalue()


class AssetTests(unittest.TestCase):
    def test_user_reference_survives_external_service_failure(self):
        image = {"type": "image", "source": {"type": "base64", "data": base64.b64encode(png()).decode(), "media_type": "image/png"}}
        with tempfile.TemporaryDirectory() as directory, patch.object(assets, "plan_visuals", side_effect=RuntimeError("offline")):
            prepared, research = assets.prepare_visuals({}, [image], [], directory)
            self.assertEqual(len(prepared), 1)
            self.assertEqual(prepared[0]["kind"], "user")
            self.assertTrue(Path(prepared[0]["path"]).is_file())
            self.assertEqual(research, [])

    def test_commons_keeps_attribution_and_skips_unknown_license(self):
        def page(index, license_name):
            return {"index": index, "title": "Car engine", "imageinfo": [{"mime": "image/jpeg", "thumburl": "https://upload.wikimedia.org/test.jpg", "descriptionurl": "https://commons.wikimedia.org/wiki/File:Test.jpg", "extmetadata": {
                "LicenseShortName": {"value": license_name}, "Artist": {"value": "<a>Photographer</a>"},
            }}]}
        raw = json.dumps({"query": {"pages": {"1": page(1, "unknown"), "2": page(2, "CC BY-SA 4.0")}}}).encode()
        with patch.object(assets, "fetch", return_value=(raw, "application/json")):
            found = list(assets.commons_candidates("car engine"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["credit"], "Photographer")
        self.assertEqual(found[0]["license"], "CC BY-SA 4.0")

    def test_source_text_distinguishes_generated_illustrations(self):
        text = assets.source_text({"assets": [
            {"kind": "web", "credit": "Author", "license": "CC BY 4.0", "source_url": "https://example.com/photo"},
            {"kind": "generated"},
        ]})
        self.assertIn("Author · CC BY 4.0", text)
        self.assertIn("https://example.com/photo", text)
        self.assertIn("AI-иллюстрация", text)

    def queue_response(self, queue, result):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            for work in Path(queue).iterdir():
                if (work / "request.json").exists():
                    (work / "illustration.png").write_bytes(png())
                    (work / "response.json").write_text(json.dumps(result))
                    return
            time.sleep(0.01)

    def test_codex_queue_roundtrip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as queue, patch.dict(os.environ, {"CODEX_VISUAL_QUEUE": queue}):
            worker = threading.Thread(target=self.queue_response, args=(queue, {"status": "ok", "image": "illustration.png", "references": []}))
            worker.start()
            data, result = assets.request_codex("engine illustration", "engine", [], timeout=3)
            worker.join()
            self.assertEqual(data, png())
            self.assertEqual(list(Path(queue).iterdir()), [])

    def test_codex_queue_rejects_path_outside_its_job(self):
        with tempfile.TemporaryDirectory() as queue, patch.dict(os.environ, {"CODEX_VISUAL_QUEUE": queue}):
            worker = threading.Thread(target=self.queue_response, args=(queue, {"status": "ok", "image": "../secret.png"}))
            worker.start()
            with self.assertRaises(ValueError):
                assets.request_codex("engine", "", [], timeout=3)
            worker.join()


class RenderAssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        os.chmod(self.directory.name, 0o755)
        path = Path(self.directory.name) / "source.jpg"
        assets.save_image(png(), path)
        self.asset = {"id": "visual_1", "path": str(path), "kind": "generated", "credit": "AI-иллюстрация"}

    def tearDown(self):
        plt.close("all")
        kit.configure_assets([])
        self.directory.cleanup()

    def test_image_fits_preserves_canvas_and_tracks_usage(self):
        kit.configure_assets([self.asset])
        fig = plt.figure()
        canvas = kit.canvas(fig)
        artist = kit.image(canvas, "visual_1", 1, 2, 10, 5)
        self.assertEqual(tuple(artist.get_extent()), (1, 11, 2, 7))
        self.assertEqual(tuple(canvas.get_xlim()), (0, 16))
        self.assertEqual(tuple(canvas.get_ylim()), (0, 9))
        self.assertEqual(kit.used_assets(), ["visual_1"])
        with self.assertRaises(ValueError):
            kit.image(canvas, "../../not-an-asset", 1, 2, 10, 5)

    def test_sandbox_renders_real_asset_pixels(self):
        plan = {"scenes": [{"heading": "Image", "narration": ""}], "_assets": [self.asset]}
        code = 'from kit import *\ndef scene(fig,t,T):\n c=canvas(fig)\n image(c,"visual_1",1,2,14,5,credit=False)\nSCENES=[scene]\n'
        result = fullgen.test_render(code, plan, [0], self.directory.name, 0)
        self.assertNotIn("import_error", result, result)
        self.assertFalse(result["missing_visuals"])
        self.assertEqual(result["used_assets"], ["visual_1"])
        with Image.open(result["scenes"][0]["frames"][0]["path"]) as frame:
            r, g, b, *_ = frame.getpixel((640, 360))
            self.assertGreater(r, 240)
            self.assertGreater(g, 100)
            self.assertLess(b, 20)

    def test_unshown_assets_require_review(self):
        self.assertTrue(fullgen.needs_review({"missing_visuals": True, "scenes": []}))

    def test_review_can_approve_a_clearer_diagram_without_decorative_images(self):
        result = {"missing_visuals": True, "scenes": []}
        with patch.object(fullgen, "claude", return_value=("<verdict>ok</verdict>", "end_turn")):
            self.assertIsNone(fullgen.review("code", {"scenes": []}, result))
        self.assertTrue(result["review_approved"])


if __name__ == "__main__":
    unittest.main()
