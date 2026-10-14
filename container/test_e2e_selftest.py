"""The end-to-end self-test passes in hermetic mode (fresh process: endpoints are read at import time)."""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class SelfTest(unittest.TestCase):
    def test_default_scenarios_pass(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_BASE_URL", "TOOLS_EXECUTOR_SOCKET")}
        run = subprocess.run([sys.executable, os.path.join(HERE, "e2e_selftest.py")], cwd=HERE, env=env,
                             capture_output=True, text=True, timeout=300)
        self.assertEqual(run.returncode, 0, run.stdout[-4000:] + run.stderr[-2000:])
        self.assertIn("PASS:", run.stdout)


if __name__ == "__main__":
    unittest.main()
