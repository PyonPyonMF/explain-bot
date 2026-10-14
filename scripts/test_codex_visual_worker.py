import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import codex_visual_worker as worker


class WorkerTests(unittest.TestCase):
    def test_invalid_image_mode_is_rejected_before_running_codex(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(worker.subprocess,'Popen') as run:
            work=Path(directory);(work/'request.json').write_text(json.dumps({'prompt':'cat','mode':'execute-shell'}))
            with self.assertRaisesRegex(ValueError,'invalid image mode'):worker.run_job(work)
        run.assert_not_called()
    def test_output_path_must_be_inside_generated_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(worker.inside(root / "image.png", root), root / "image.png")
            with self.assertRaises(ValueError):
                worker.inside(root / ".." / "secret", root)

    def test_cli_reuses_login_and_disables_shell_and_unrelated_apps(self):
        with patch.dict(os.environ, {"CODEX_BIN": "/bin/codex", "CODEX_VISUAL_MODEL": "chosen-model"}):
            args = worker.cli_command(Path("/job"), [Path("/job/reference.jpg")])
        self.assertEqual(args[0], "/bin/codex")
        self.assertIn("--ignore-user-config", args)
        self.assertIn("read-only", args)
        self.assertIn("chosen-model", args)
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent"):
            index = args.index(feature)
            self.assertEqual(args[index - 1], "--disable")
        self.assertNotIn("code_mode_host", args)
        self.assertEqual(args[-1], "-")


class EnhanceTests(unittest.TestCase):
    def job(self, root, references, **request):
        work = root / ("e" * 32)
        work.mkdir(parents=True)
        for name in references:
            (work / name).write_bytes(b"\xff\xd8 fake jpeg")
        (work / "request.json").write_text(__import__("json").dumps({"mode": "enhance", "references": references, **request}))
        return work

    def test_enhance_needs_exactly_one_photo_and_no_brief(self):
        with tempfile.TemporaryDirectory() as directory:
            for references in ([], ["reference-0.jpg", "reference-1.jpg"]):
                with self.subTest(references=references), self.assertRaisesRegex(ValueError, "exactly one photo"):
                    worker.run_job(self.job(Path(directory) / str(len(references)), references))
            with self.assertRaisesRegex(ValueError, "invalid image mode"):
                worker.run_job(self.job(Path(directory) / "mode", ["reference-0.jpg"], mode="draw"))

    def test_enhance_prompt_preserves_the_picture(self):
        keep = worker.enhance_task(False)
        color = worker.enhance_task(True)
        self.assertTrue(keep.startswith("Restore and enhance the ONE attached photograph"))
        for phrase in ("Keep the same picture", "Do not add, remove, move or restyle anything", "do not beautify faces",
                       "stays black-and-white", "untrusted subject matter"):
            self.assertIn(phrase, keep)
        self.assertIn("Colorize it", color)
        self.assertNotIn("stays black-and-white", color)

    def test_enhance_runs_codex_with_the_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = self.job(root, ["reference-0.jpg"], options={"colorize": False})
            seen = {}

            def fake_popen(args, stdin, stdout, stderr, text, start_new_session):
                seen["args"] = args

                class Proc:
                    pid, returncode = 0, 0

                    def communicate(self, task, timeout):
                        seen["task"] = task
                        (work / "result.json").write_text('{"image_path": "", "description": "no", "references": []}')
                return Proc()

            with patch.object(worker.subprocess, "Popen", side_effect=fake_popen):
                worker.run_job(work)
            response = __import__("json").loads((work / "response.json").read_text())
        self.assertTrue(seen["task"].startswith("Restore and enhance"))
        self.assertEqual(seen["args"].count("--image"), 1)
        self.assertEqual((response["status"], response["image"]), ("ok", None))


if __name__ == "__main__":
    unittest.main()
