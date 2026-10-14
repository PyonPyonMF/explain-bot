"""Tool forge end to end with a fake Claude Code, a local git remote and a fake GitHub API; real gates and sandbox."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

import tool_forge as forge

ROOT = Path(__file__).resolve().parent.parent
SPEC = {"capability": "length_convert", "summary": "Convert lengths between metric and imperial units.",
        "inputs": "value, from_unit, to_unit", "outputs": "converted value", "example_requests": ["сколько метров в 10 футах"],
        "network_hosts": [], "category": "conversion"}

FAKE_CLAUDE = textwrap.dedent('''
    import json, os, re, sys
    from pathlib import Path
    prompt = sys.stdin.read()
    log = Path(os.environ.get("HOME", "/tmp")) / "claude-calls.jsonl"
    with log.open("a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd(), "prompt": prompt[:6000]}) + "\\n")
    if prompt.startswith("Write hidden acceptance tests") and (Path.home() / "hidden_fails").exists():
        print(json.dumps({"type": "result", "subtype": "success", "result": "could not", "is_error": False}))
        sys.exit(0)
    if prompt.startswith("Write hidden acceptance tests"):
        cases = [{"name": "feet to metres", "args": {"value": 10, "from_unit": "ft", "to_unit": "m"}, "expect": {"value": 3.048}},
                 {"name": "miles to km", "args": {"value": 2, "from_unit": "mi", "to_unit": "km"}, "expect": {"value": 3.218688}},
                 {"name": "negative is declined", "args": {"value": -1, "from_unit": "m", "to_unit": "cm"}, "expect_error": "negative"},
                 {"name": "unknown unit", "args": {"value": 1, "from_unit": "parsec", "to_unit": "m"}, "expect_error": "one of"}]
        Path("hidden_tests.json").write_text(json.dumps(cases))
        print(json.dumps({"type": "result", "subtype": "success", "result": "hidden tests written", "is_error": False}))
        sys.exit(0)
    tool_id = re.search(r'id "([a-z0-9_]+)"', prompt)[1]
    fixed = "The gates failed" in prompt
    feet = 0.3048 if fixed else 0.3
    directory = Path("tools") / tool_id
    directory.mkdir(parents=True, exist_ok=True)
    units = ["m", "km", "cm", "in", "ft", "mi"]
    manifest = {"api": 1, "id": tool_id, "title": "Length conversion",
                "description": "Converts a length between metres, kilometres, centimetres, inches, feet and miles exactly.",
                "examples": ["сколько метров в 10 футах", "convert 3 miles to km"],
                "input_schema": {"type": "object", "additionalProperties": False, "required": ["value", "from_unit", "to_unit"],
                                 "properties": {"value": {"type": "number", "minimum": -1e12, "maximum": 1e12},
                                                "from_unit": {"type": "string", "enum": units}, "to_unit": {"type": "string", "enum": units}}},
                "output_description": "value converted to to_unit, rounded to 6 decimals", "network": []}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "tool.py").write_text(
        "METRES = {'m': 1, 'km': 1000, 'cm': 0.01, 'in': 0.0254, 'ft': %r, 'mi': 1609.344}\\n\\n\\n"
        "def run(args, ctx):\\n"
        "    if args['value'] < 0:\\n"
        "        ctx.fail('negative lengths are not supported')\\n"
        "    value = args['value'] * METRES[args['from_unit']] / METRES[args['to_unit']]\\n"
        "    return {'value': round(value, 6), 'unit': args['to_unit']}\\n" % feet)
    tests = [{"name": "metres to cm", "args": {"value": 1.5, "from_unit": "m", "to_unit": "cm"}, "expect": {"value": 150.0}},
             {"name": "feet to metres", "args": {"value": 10, "from_unit": "ft", "to_unit": "m"}, "expect": {"value": round(10 * feet, 6)}},
             {"name": "inches to cm", "args": {"value": 1, "from_unit": "in", "to_unit": "cm"}, "expect": {"value": 2.54}},
             {"name": "same unit", "args": {"value": 7, "from_unit": "km", "to_unit": "km"}, "expect": {"value": 7}},
             {"name": "negative", "args": {"value": -3, "from_unit": "m", "to_unit": "km"}, "expect_error": "negative"},
             {"name": "bad unit", "args": {"value": 3, "from_unit": "yard", "to_unit": "m"}, "expect_error": "one of"}]
    (directory / "tests.json").write_text(json.dumps(tests))
    if os.environ.get("FAKE_OUTSIDE") or (Path.home() / "outside").exists():
        Path("README.md").write_text("changed by the builder")
    print(json.dumps({"type": "result", "subtype": "success", "result": "Converts lengths between six units; no network.",
                      "total_cost_usd": 0.01, "num_turns": 7, "is_error": False}))
''')


def sandbox_ready():
    try:
        pwd.getpwnam("sandbox")
    except KeyError:
        return False
    return os.getuid() == 0


class FakeGitHub:
    def __init__(self):
        self.requests = []
        github = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                github.requests.append({"path": self.path, "auth": self.headers.get("authorization"),
                                        "body": json.loads(self.rfile.read(int(self.headers["content-length"])))})
                data = json.dumps({"html_url": "https://github.com/owner/repo/pull/7"}).encode()
                self.send_response(201)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@unittest.skipUnless(sandbox_ready(), "needs root and the sandbox user (gates run tools in the sandbox)")
class ForgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        bare, seed = self.tmp / "remote.git", self.tmp / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        shutil.copytree(ROOT / "tools" / "date_calc", seed / "tools" / "date_calc")
        (seed / "README.md").write_text("seed\n")
        for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed"],
                     ["remote", "add", "origin", str(bare)], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", "-C", str(seed), *args], check=True)
        self.repo = self.tmp / "forge-repo"
        subprocess.run(["git", "clone", "-q", str(bare), str(self.repo)], check=True)
        self.bare = bare
        fake = self.tmp / "claude"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
        fake.chmod(0o755)
        self.github = FakeGitHub()
        self.addCleanup(self.github.server.shutdown)
        venv_python = ROOT / ".venv" / "bin" / "python"
        env = {"CLAUDE_BIN": str(fake), "FORGE_REPO": str(self.repo), "FORGE_GITHUB_REPO": "owner/repo",
               "FORGE_GITHUB_API": self.github.url, "FORGE_GITHUB_TOKEN": "ghs_secret_token", "HOME": str(self.home),
               "FORGE_TEST_CMD": json.dumps([str(venv_python if venv_python.exists() else sys.executable), "-m", "tool_gates"]),
               "PYTHONPATH": str(ROOT / "container"), "FORGE_REPAIR_ROUNDS": "2"}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, capability="length_convert", existing=("date_calc",)):
        job_dir = self.tmp / "queue" / ("a" * 32)
        job_dir.mkdir(parents=True)
        (job_dir / "request.json").write_text(json.dumps({"api": 1, "capability": capability, "spec": SPEC,
                                                           "requests": 3, "existing_tools": list(existing)}))
        return job_dir

    def calls(self):
        log = self.home / "claude-calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_builds_repairs_from_hidden_tests_and_opens_a_pull_request(self):
        job_dir = self.job()
        forge.run_job(job_dir)
        result = json.loads((job_dir / "response.json").read_text())
        self.assertEqual(result["status"], "pr_opened", result)
        self.assertEqual(result["pr_url"], "https://github.com/owner/repo/pull/7")
        self.assertEqual(result["rounds"], 2)  # the hidden tests caught the wrong feet factor
        self.assertEqual(result["tests"]["hidden"], {"passed": 4, "total": 4})
        files = subprocess.run(["git", "-C", str(self.bare), "diff", "--name-only", "main", "tools/length_convert"],
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(sorted(files), ["tools/length_convert/manifest.json", "tools/length_convert/tests.json",
                                         "tools/length_convert/tool.py"])
        pull = self.github.requests[0]
        self.assertEqual((pull["path"], pull["auth"]), ("/repos/owner/repo/pulls", "Bearer ghs_secret_token"))
        self.assertEqual((pull["body"]["head"], pull["body"]["base"]), ("tools/length_convert", "main"))
        self.assertIn("hidden tests: 4/4", pull["body"]["body"])

        calls = self.calls()
        builders = [c for c in calls if not c["prompt"].startswith("Write hidden")]
        hidden = [c for c in calls if c["prompt"].startswith("Write hidden")]
        self.assertEqual((len(builders), len(hidden)), (2, 1))
        self.assertIn("The gates failed", builders[1]["prompt"])
        for call in calls:
            self.assertNotIn("FORGE_GITHUB_TOKEN", call["env"])  # the token never reaches an agent
            self.assertIn("--restricted", call["argv"])
            self.assertEqual(call["argv"][call["argv"].index("--permission-mode") + 1], "dontAsk")
        for call in calls:  # no agent gets a shell: prefix rules are not a boundary for command substitution
            self.assertNotIn("Bash", call["argv"][call["argv"].index("--tools") + 1])
            self.assertFalse(any(arg.startswith("Bash") for arg in call["argv"]))
        self.assertNotIn("tool.py", hidden[0]["prompt"])
        self.assertNotEqual(Path(hidden[0]["cwd"]).resolve().parent, self.repo.resolve())  # never inside the worktree
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "worktree", "list"], capture_output=True,
                                        text=True).stdout.count("\n"), 1)  # the job worktree was removed

    def test_changes_outside_the_tool_directory_are_rejected(self):
        (self.home / "outside").write_text("1")
        job_dir = self.job()
        forge.run_job(job_dir)
        result = json.loads((job_dir / "response.json").read_text())
        self.assertEqual(result["status"], "rejected")
        self.assertIn("README.md", result["reason"])
        self.assertFalse(self.github.requests)
        heads = subprocess.run(["git", "-C", str(self.bare), "branch"], capture_output=True, text=True).stdout
        self.assertNotIn("tools/", heads)

    def test_a_failing_hidden_author_does_not_rerun_the_builder(self):
        (self.home / "hidden_fails").write_text("1")
        job_dir = self.job()
        forge.run_job(job_dir)
        result = json.loads((job_dir / "response.json").read_text())
        self.assertEqual((result["status"], result["reason"]), ("failed", "the hidden tests could not be written"))
        calls = self.calls()
        self.assertEqual(sum(not c["prompt"].startswith("Write hidden") for c in calls), 1)
        self.assertEqual(sum(c["prompt"].startswith("Write hidden") for c in calls), 2)
        self.assertFalse(self.github.requests)

    def test_existing_or_invalid_ids_never_start_a_builder(self):
        for capability in ("date_calc", "Bad-Id"):
            shutil.rmtree(self.tmp / "queue", ignore_errors=True)
            job_dir = self.job(capability)
            forge.run_job(job_dir)
            self.assertEqual(json.loads((job_dir / "response.json").read_text())["status"], "rejected")
        self.assertEqual(self.calls(), [])

    def test_gate_command_for_the_builder(self):
        report = forge.gates(ROOT / "tools" / "date_calc")
        self.assertTrue(report["passed"], report)


class ClaudeResultTests(unittest.TestCase):
    def test_error_results_fail_the_round(self):
        with tempfile.TemporaryDirectory() as work:
            fake = Path(work) / "claude"
            fake.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({{'type': 'result', 'subtype': "
                            "'error_max_budget_usd', 'is_error': True, 'errors': ['budget']}))\n")
            fake.chmod(0o755)
            with patch.dict(os.environ, {"CLAUDE_BIN": str(fake)}), self.assertRaisesRegex(RuntimeError, "error_max_budget_usd"):
                forge.claude("task", work, ["Read"], ["Read"])


class PushTests(unittest.TestCase):
    def test_token_travels_in_the_environment_not_in_arguments(self):
        with patch.dict(os.environ, {"FORGE_GITHUB_TOKEN": "ghs_x"}):
            env = forge.push_env()
            agent = forge.agent_env()
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertNotIn("ghs_x", env["GIT_CONFIG_VALUE_0"])  # base64 credentials
        self.assertNotIn("FORGE_GITHUB_TOKEN", agent)


if __name__ == "__main__":
    unittest.main()
