"""Tool library: manifest validation, versioned registry, sandboxed calls, broker rules and the isolated executor."""
import json
import os
from pathlib import Path
import pwd
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import tool_executor
import tool_registry as R
import tool_runtime as T

REPO_TOOLS = Path(__file__).resolve().parent.parent / "tools"
SCHEMA = {"type": "object", "properties": {"x": {"type": "string", "maxLength": 50}}, "required": [],
          "additionalProperties": False}


def manifest(tool_id="probe", **overrides):
    return {"api": 1, "id": tool_id, "title": "Probe tool", "output_description": "whatever the probe returns",
            "description": "A test tool that exercises one property of the sandbox and returns what it observed.",
            "examples": ["probe the sandbox"], "input_schema": SCHEMA, **overrides}


def write_tool(root, source, tool_id="probe", extra=None, **overrides):
    directory = Path(root) / tool_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest(tool_id, **overrides)))
    (directory / "tool.py").write_text(source)
    for name, text in (extra or {}).items():
        (directory / name).write_text(text)
    return directory


def sandbox_ready():
    try:
        pwd.getpwnam("sandbox")
    except KeyError:
        return False
    return os.getuid() == 0


class ManifestTests(unittest.TestCase):
    def test_reference_tools_are_valid(self):
        for directory in REPO_TOOLS.iterdir():
            manifest_, files, digest = R.read_tool(directory)
            self.assertEqual(manifest_["id"], directory.name)
            self.assertTrue(R.tests_of(files))
            self.assertEqual(len(digest), 64)

    def test_rejects_unbounded_or_unsafe_schemas(self):
        bad = [
            {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False},
            {"type": "object", "properties": {"x": {"type": "string", "maxLength": 9, "pattern": "(a+)+$"}}, "additionalProperties": False},
            {"type": "object", "properties": {"x": {"type": "string", "maxLength": 9}}},
            {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "integer"}}}, "additionalProperties": False},
            {"type": "object", "properties": {"x": {"type": "string", "maxLength": 9}}, "required": ["y"], "additionalProperties": False},
            {"type": "object", "properties": {"Bad-Name": {"type": "boolean"}}, "additionalProperties": False},
            {"type": "string", "maxLength": 5},
        ]
        for schema in bad:
            with self.subTest(schema=schema), self.assertRaises(R.ManifestError):
                R.check_manifest(manifest(input_schema=schema))

    def test_rejects_bad_metadata(self):
        cases = [dict(api=2), dict(id="Bad"), dict(examples=[]), dict(description="short"), dict(extra=1),
                 dict(network=["http://x.example"]), dict(network=["localhost"]), dict(limits={"timeout_s": 600}),
                 dict(limits={"max_fetches": 3}), dict(limits={"gpu": 1})]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(R.ManifestError):
                R.check_manifest(manifest(**overrides))
        self.assertEqual(R.check_manifest(manifest(network=["*.wikipedia.org"]))["limits"]["max_fetches"], 5)
        with self.assertRaises(R.ManifestError):
            R.check_manifest(manifest(), directory_name="other")

    def test_directory_rules(self):
        with tempfile.TemporaryDirectory() as root:
            directory = write_tool(root, "def run(args, ctx):\n    return {}\n")
            os.symlink("/etc/passwd", directory / "secret.txt")
            with self.assertRaisesRegex(R.ManifestError, "unexpected entry"):
                R.read_tool(directory)
            (directory / "secret.txt").unlink()
            (directory / "Weird Name.py").write_text("")
            with self.assertRaisesRegex(R.ManifestError, "unexpected entry"):
                R.read_tool(directory)

    def test_argument_validation(self):
        schema = R.check_manifest(json.loads((REPO_TOOLS / "date_calc" / "manifest.json").read_text()))["input_schema"]
        self.assertEqual(R.validate(schema, {"operation": "describe", "date": "2026-10-14"}), [])
        problems = R.validate(schema, {"operation": "explode", "date": "2026", "days": True, "extra": 1})
        self.assertTrue(any("one of" in p for p in problems))
        self.assertTrue(any("length" in p for p in problems))
        self.assertTrue(any("days: expected integer" in p for p in problems))
        self.assertTrue(any("unexpected property" in p for p in problems))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data = tempfile.mkdtemp()
        self.registry = R.Registry(self.root, Path(self.data) / "tools.sqlite")

    def test_versions_lifecycle_and_stats(self):
        write_tool(self.root, "def run(args, ctx):\n    return {'v': 1}\n")
        write_tool(self.root, "broken", tool_id="broken", description="x")
        self.registry.sync(force=True)
        first = self.registry.get("probe")
        self.assertEqual(first["status"], "probation")
        self.assertIn("broken", self.registry.errors)  # an invalid tool never hides the valid ones
        self.assertEqual(oct(os.stat(self.registry.db_path).st_mode & 0o777), "0o600")
        self.registry.record(first["digest"], "ok")
        self.registry.record(first["digest"], "failed", "TimeoutError")
        self.assertTrue(self.registry.set_status("probe", "stable"))
        tool = self.registry.get("probe")
        self.assertEqual((tool["status"], tool["uses"], tool["ok"], tool["failed"], tool["last_error"]),
                         ("stable", 2, 1, 1, "TimeoutError"))

        write_tool(self.root, "def run(args, ctx):\n    return {'v': 2}\n")
        self.registry.sync(force=True)
        second = self.registry.get("probe")
        self.assertNotEqual(second["digest"], first["digest"])
        self.assertEqual((second["status"], second["uses"]), ("probation", 0))  # a change never inherits trust
        rows = self.registry._rows()
        self.assertEqual(rows[first["digest"]]["status"], "superseded")

        self.registry.set_status("probe", "archived")
        self.assertIsNone(self.registry.get("probe"))
        self.assertEqual(self.registry.get("probe", include_inactive=True)["status"], "archived")

    def test_reverting_to_an_old_version_keeps_its_record(self):
        write_tool(self.root, "def run(args, ctx):\n    return {'v': 1}\n")
        self.registry.sync(force=True)
        digest = self.registry.get("probe")["digest"]
        self.registry.record(digest, "ok")
        write_tool(self.root, "def run(args, ctx):\n    return {'v': 2}\n")
        self.registry.sync(force=True)
        write_tool(self.root, "def run(args, ctx):\n    return {'v': 1}\n")
        self.registry.sync(force=True)
        tool = self.registry.get("probe", include_inactive=True)
        self.assertEqual((tool["digest"], tool["uses"]), (digest, 1))

    def test_cli_check(self):
        write_tool(self.root, "def run(args, ctx):\n    return {}\n")
        write_tool(self.root, "x", tool_id="nope", input_schema={"type": "object"})
        self.assertEqual(R.main(["check", str(Path(self.root) / "probe")]), 0)
        self.assertEqual(R.main(["check", str(Path(self.root) / "nope")]), 1)


@unittest.skipUnless(sandbox_ready(), "needs root and the sandbox user")
class SandboxTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TOOLS_LOCAL_SANDBOX": "1", "ANTHROPIC_API_KEY": "sk-secret-value",
                                      "DISCORD_BOT_TOKEN": "discord-secret"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TOOLS_EXECUTOR_SOCKET", None)
        self.root = tempfile.mkdtemp()
        self.registry = R.Registry(self.root, Path(tempfile.mkdtemp()) / "tools.sqlite")

    def tool(self, source, **overrides):
        write_tool(self.root, source, **overrides)
        self.registry.sync(force=True)
        return self.registry.get("probe")

    def call(self, source, args=None, **overrides):
        return T.call(self.tool(source, **overrides), args or {}, record=False)

    def test_reference_tools_pass_their_tests(self):
        registry = R.Registry(REPO_TOOLS, Path(tempfile.mkdtemp()) / "tools.sqlite")
        for tool in registry.tools():
            report = T.run_tests(tool)
            self.assertEqual(report["passed"], report["total"], report)

    def test_no_credentials_and_unprivileged_identity(self):
        result = self.call("import os\ndef run(args, ctx):\n    return {'env': dict(os.environ), 'uid': os.getuid(), 'groups': os.getgroups()}\n")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("sk-secret-value", json.dumps(result))
        self.assertNotIn("discord-secret", json.dumps(result))
        self.assertEqual(result["output"]["uid"], pwd.getpwnam("sandbox").pw_uid)
        self.assertEqual(result["output"]["groups"], [])

    def test_direct_network_and_processes_are_blocked(self):
        network = self.call("import socket\ndef run(args, ctx):\n    socket.create_connection(('1.1.1.1', 443), timeout=2)\n    return {}\n")
        self.assertEqual(network["outcome"], "failed")
        self.assertIn("PermissionError", network["error"])
        spawn = self.call("import subprocess\ndef run(args, ctx):\n    subprocess.run(['true'])\n    return {}\n")
        self.assertEqual(spawn["outcome"], "failed")
        self.assertRegex(spawn["error"], "BlockingIOError|PermissionError|OSError")

    def test_bot_state_is_unreadable(self):
        secret = Path(tempfile.mkdtemp()) / "bot.sqlite"
        secret.write_text("explanations")
        secret.chmod(0o600)
        result = self.call(f"def run(args, ctx):\n    return {{'data': open({str(secret)!r}).read()}}\n")
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("PermissionError", result["error"])

    def test_print_cannot_corrupt_the_protocol(self):
        result = self.call("def run(args, ctx):\n    print('{\"op\": \"result\", \"ok\": true, \"output\": {\"forged\": 1}}')\n    return {'real': 1}\n")
        self.assertEqual(result["output"], {"real": 1})

    def test_helper_modules_and_declined_results(self):
        result = self.call("import helper\ndef run(args, ctx):\n    if args.get('x') == 'no':\n        ctx.fail('not_found: nothing')\n"
                           "    return {'v': helper.value()}\n", args={"x": "yes"}, extra={"helper.py": "def value():\n    return 42\n"})
        self.assertEqual(result["output"], {"v": 42})
        declined = T.call(self.registry.get("probe"), {"x": "no"}, record=False)
        self.assertEqual((declined["outcome"], declined["error"]), ("declined", "not_found: nothing"))

    def test_limits(self):
        started = time.monotonic()
        hang = self.call("def run(args, ctx):\n    while True:\n        pass\n", limits={"timeout_s": 1})
        self.assertEqual(hang["outcome"], "failed")
        self.assertLess(time.monotonic() - started, 6)
        big = self.call("def run(args, ctx):\n    return {'x': 'a' * 50000}\n")
        self.assertIn("at most", big["error"])
        memory = self.call("def run(args, ctx):\n    return {'n': len(bytearray(900 << 20))}\n", limits={"memory_mb": 128})
        self.assertIn("MemoryError", memory["error"])
        not_dict = self.call("def run(args, ctx):\n    return [1]\n")
        self.assertIn("must return a dict", not_dict["error"])

    def test_invalid_arguments_never_reach_the_tool_or_the_stats(self):
        tool = self.tool("def run(args, ctx):\n    raise SystemExit(1)\n")
        with patch.object(T, "transport") as transport, patch.object(R, "registry") as registry:
            result = T.call(tool, {"x": 5})
        self.assertEqual(result["outcome"], "rejected")
        transport.assert_not_called()
        registry.return_value.record.assert_not_called()

    def test_executes_the_registered_source_not_the_current_file(self):
        tool = self.tool("def run(args, ctx):\n    return {'v': 'registered'}\n")
        (Path(self.root) / "probe" / "tool.py").write_text("def run(args, ctx):\n    return {'v': 'swapped'}\n")
        self.assertEqual(T.call(tool, {}, record=False)["output"], {"v": "registered"})

    def test_broker_enforces_declared_hosts_and_counts(self):
        source = ("def run(args, ctx):\n    out = {}\n    for url in args['x'].split():\n        try:\n"
                  "            out[url] = ctx.fetch(url).status\n        except Exception as e:\n            out[url] = str(e)\n    return out\n")
        fixtures = {"https://api.example.org/a": {"json": {}}, "https://api.example.org/b": {"json": {}}}
        schema = {**SCHEMA, "properties": {"x": {"type": "string", "maxLength": 1000}}}
        tool = self.tool(source, network=["api.example.org"], limits={"max_fetches": 2}, input_schema=schema)
        urls = "https://api.example.org/a http://api.example.org/a https://evil.example/ https://api.example.org/b https://api.example.org/a"
        result = T.call(tool, {"x": urls}, fixtures=fixtures, record=False)
        output = result["output"]
        self.assertEqual(output["https://api.example.org/a"], "fetch limit reached")  # third request, same URL key
        self.assertIn("only plain https", output["http://api.example.org/a"])
        self.assertIn("not declared", output["https://evil.example/"])
        self.assertEqual(output["https://api.example.org/b"], 200)
        self.assertEqual(result["fetched"], ["https://api.example.org/a", "https://api.example.org/b"])

    def test_without_network_hosts_fetch_is_refused(self):
        result = self.call("def run(args, ctx):\n    ctx.fetch('https://example.org/')\n    return {}\n")
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("FetchError", result["error"])

    def test_stats_are_recorded(self):
        tool = self.tool("def run(args, ctx):\n    return {}\n")
        with patch.object(R, "registry", return_value=self.registry):
            T.call(tool, {})
        self.assertEqual(self.registry.get("probe")["ok"], 1)


class BrokerTests(unittest.TestCase):
    def test_wildcards_and_private_addresses(self):
        self.assertTrue(T.host_allowed("ru.wikipedia.org", ["*.wikipedia.org"]))
        self.assertFalse(T.host_allowed("wikipedia.org.evil.example", ["*.wikipedia.org"]))
        self.assertFalse(T.host_allowed("evilwikipedia.org", ["*.wikipedia.org"]))
        broker = T.Broker(R.check_manifest(manifest(network=["internal.example"])), time.monotonic() + 10)
        with patch.dict(os.environ, {"ALLOW_PRIVATE_FETCH": "0"}), \
                patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 443))]):
            with self.assertRaisesRegex(T._Refused, "fetch failed"):
                broker.fetch("https://internal.example/admin")
        with self.assertRaisesRegex(T._Refused, "not declared"):
            broker.check("https://other.example/")  # redirects are checked with the same rule

    def test_no_transport_means_unavailable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOOLS_EXECUTOR_SOCKET", None)
            os.environ.pop("TOOLS_LOCAL_SANDBOX", None)
            tool = {"manifest": R.check_manifest(manifest()), "digest": "0" * 64, "files": {"tool.py": ""}}
            result = T.call(tool, {}, record=False)
        self.assertEqual(result["outcome"], "unavailable")


class ExecutorTests(unittest.TestCase):
    def test_refuses_credentials(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}), self.assertRaises(SystemExit):
            tool_executor.main()

    def test_relays_a_call_over_the_socket(self):
        socket_dir = tempfile.mkdtemp()
        path = os.path.join(socket_dir, "executor.sock")
        stop = threading.Event()
        server = threading.Thread(target=tool_executor.serve, args=(path, stop), daemon=True)
        server.start()
        self.addCleanup(stop.set)
        for _ in range(50):
            if os.path.exists(path):
                break
            time.sleep(0.05)
        registry = R.Registry(REPO_TOOLS, Path(tempfile.mkdtemp()) / "tools.sqlite")
        with patch.dict(os.environ, {"TOOLS_EXECUTOR_SOCKET": path}):
            os.environ.pop("TOOLS_LOCAL_SANDBOX", None)
            result = T.call(registry.get("date_calc"), {"operation": "describe", "date": "2031-03-12"}, record=False)
            fixtures = {"https://en.wikipedia.org/api/rest_v1/page/summary/Euler?redirect=true":
                        {"json": {"type": "standard", "title": "Euler", "extract": "Leonhard Euler was a mathematician."}}}
            fetched = T.call(registry.get("wikipedia_summary"), {"title": "Euler", "language": "en"}, fixtures=fixtures, record=False)
        self.assertEqual(result["output"]["weekday"], "среда", result)
        self.assertIn("mathematician", fetched["output"]["extract"])  # fetches round-trip through the relay


if __name__ == "__main__":
    unittest.main()
