"""Acceptance gates for generated tools: code contract, scope against the specification, own and hidden tests."""
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

import tool_gates as G

REPO_TOOLS = Path(__file__).resolve().parent.parent / "tools"


def sandbox_ready():
    try:
        pwd.getpwnam("sandbox")
    except KeyError:
        return False
    return os.getuid() == 0


class CodeGateTests(unittest.TestCase):
    def problems(self, source, **files):
        return G.check_code({"tool.py": source, **files})

    def test_plain_tools_pass(self):
        source = "import math\nfrom datetime import date\nfrom urllib.parse import quote\nimport helper\n\n\ndef run(args, ctx):\n    return {}\n"
        self.assertEqual(self.problems(source, **{"helper.py": "X = 1\n"}), [])

    def test_escape_hatches_are_flagged(self):
        cases = {
            "import os": "import of 'os'", "import subprocess": "import of 'subprocess'",
            "from urllib import request": "import of 'urllib'", "import ctypes": "import of 'ctypes'",
            "x = open('/etc/passwd')": "open is not allowed", "x = eval('1')": "eval is not allowed",
            "x = getattr(ctx, 'fetch')": "getattr is not allowed", "x = ().__class__": "dunder attribute",
            "x = __builtins__": "__builtins__ is not allowed", "x = '{0.__globals__}'.format(run)": "format strings",
            "import io\nx = io.open('f')": "attribute 'open'", "from . import helper": "relative imports",
        }
        for snippet, expected in cases.items():
            with self.subTest(snippet=snippet):
                found = self.problems(snippet + "\n\ndef run(args, ctx):\n    return {}\n")
                self.assertTrue(any(expected in p for p in found), found)

    def test_contract(self):
        self.assertTrue(any("define run" in p for p in self.problems("def main(args):\n    return {}\n")))
        self.assertTrue(any("syntax error" in p for p in self.problems("def run(args, ctx)\n")))
        self.assertTrue(any("global" in p for p in self.problems("n = 0\ndef run(args, ctx):\n    global n\n    return {}\n")))


class ScopeTests(unittest.TestCase):
    def test_hosts_must_come_from_the_specification_and_ids_must_be_new(self):
        manifest = {"id": "date_calc", "network": ["api.example.org", "tracker.example"]}
        problems = G.check_scope(manifest, {"network_hosts": ["api.example.org"]}, REPO_TOOLS, "/elsewhere/date_calc")
        self.assertTrue(any("tracker.example" in p for p in problems))
        self.assertTrue(any("already exists" in p for p in problems))
        self.assertEqual(G.check_scope(manifest | {"network": []}, None, REPO_TOOLS, REPO_TOOLS / "date_calc"), [])


@unittest.skipUnless(sandbox_ready(), "needs root and the sandbox user")
class EvaluateTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TOOLS_LOCAL_SANDBOX": "1", "DATA_DIR": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TOOLS_EXECUTOR_SOCKET", None)

    def test_reference_tools_pass_every_gate(self):
        for directory in REPO_TOOLS.iterdir():
            with self.subTest(tool=directory.name):
                report = G.evaluate(directory, existing=REPO_TOOLS)
                self.assertTrue(report["passed"], report)

    def test_hidden_tests_and_test_quantity(self):
        hidden = [{"name": "wrong on purpose", "args": {"operation": "describe", "date": "2031-03-12"}, "expect": {"weekday": "среда"}},
                  {"name": "would be wrong", "args": {"operation": "describe", "date": "2031-03-13"}, "expect": {"weekday": "среда"}}]
        report = G.evaluate(REPO_TOOLS / "date_calc", hidden=hidden)
        self.assertFalse(report["passed"])
        self.assertEqual(report["tests"]["hidden"], {"passed": 1, "total": 2})
        self.assertTrue(any("would be wrong" in p for p in report["gates"]["tests"]))
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "tiny_tool"
            directory.mkdir()
            source = (REPO_TOOLS / "date_calc" / "manifest.json").read_text().replace('"date_calc"', '"tiny_tool"')
            (directory / "manifest.json").write_text(source)
            (directory / "tool.py").write_text("def run(args, ctx):\n    return {'weekday': 'среда'}\n")
            (directory / "tests.json").write_text(json.dumps([{"name": "one", "args": {"operation": "describe", "date": "2031-03-12"},
                                                               "expect": {"weekday": "среда"}}]))
            report = G.evaluate(directory)
        self.assertTrue(any("at least 4 cases" in p for p in report["gates"]["tests"]))
        self.assertTrue(any("expect_error" in p for p in report["gates"]["tests"]))

    def test_code_gate_failure_skips_execution(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "date_calc"
            directory.mkdir()
            for name in ("manifest.json", "tests.json"):
                (directory / name).write_text((REPO_TOOLS / "date_calc" / name).read_text())
            (directory / "tool.py").write_text("import os\n\ndef run(args, ctx):\n    return {'env': dict(os.environ)}\n")
            with patch("tool_runtime.call") as call:
                report = G.evaluate(directory)
        call.assert_not_called()
        self.assertEqual(report["gates"]["tests"], ["skipped: code gate failed"])


if __name__ == "__main__":
    unittest.main()
