import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import codex_visual_worker as worker


class WorkerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
