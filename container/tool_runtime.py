"""Call library tools in isolation and broker their network access.

The sandboxed harness gets the verified source, the validated arguments and nothing else: no credentials, no
Discord data beyond the arguments, no direct network. Every ctx.fetch is checked here against the manifest's hosts,
public addresses only, bounded in count, size and time. Results are untrusted data, like any fetched page.

Transports: TOOLS_EXECUTOR_SOCKET (tool_executor.py in a separate container with network_mode: none) or, for local
development and tests, TOOLS_LOCAL_SANDBOX=1 (a subprocess as the unprivileged `sandbox` user, without that
network boundary). Without either, tools are unavailable.
"""
import json
import logging
import os
import pwd
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import tool_registry as R

log = logging.getLogger("tools")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_LINE = 4_000_000
MAX_MESSAGES = 200


class ToolUnavailable(RuntimeError):
    pass


class _Violation(Exception):
    pass


def harness_env(home):
    # `python -I` ignores PYTHON* variables; the harness opens its protocol pipes as UTF-8 itself.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1"}


class _Lines:
    """Line-delimited JSON over a pair of binary streams, with a reader thread and a receive deadline."""

    def __init__(self, reader, writer):
        self.writer, self.inbox = writer, queue.Queue()
        threading.Thread(target=self._pump, args=(reader,), daemon=True).start()

    def _pump(self, reader):
        try:
            while True:
                line = reader.readline(MAX_LINE + 1)
                if not line:
                    break
                self.inbox.put(line if line.endswith(b"\n") else b"")  # b"" marks an oversized message
        except (OSError, ValueError):
            pass
        self.inbox.put(None)

    def send(self, message):
        self.writer.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        self.writer.flush()

    def receive(self, deadline):
        try:
            line = self.inbox.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError("tool timed out") from None
        if line is None:
            raise _Violation("the tool exited without a result")
        if not line:
            raise _Violation("oversized protocol message")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            raise _Violation("malformed protocol message") from None
        if not isinstance(message, dict):
            raise _Violation("malformed protocol message")
        return message


class LocalSandbox:
    """Subprocess as the `sandbox` user. The harness lowers its own limits before it loads tool code."""

    def __init__(self, init):
        try:
            account = pwd.getpwnam(os.environ.get("SANDBOX_USER", "sandbox"))
        except KeyError:
            raise ToolUnavailable("sandbox user is missing") from None
        if os.getuid() != 0:
            raise ToolUnavailable("the local tool sandbox needs root to switch to the sandbox user")
        self.home = tempfile.mkdtemp(prefix="tool-")
        os.chown(self.home, account.pw_uid, account.pw_gid)
        self.proc = subprocess.Popen([sys.executable, "-I", os.path.join(APP_DIR, "tool_harness.py")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     cwd=self.home, env=harness_env(self.home), user=account.pw_uid,
                                     group=account.pw_gid, extra_groups=[], start_new_session=True)
        self.lines = _Lines(self.proc.stdout, self.proc.stdin)
        self.lines.send(init)

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        shutil.rmtree(self.home, ignore_errors=True)


class ExecutorSocket:
    """Connection to tool_executor.py; the same protocol, relayed by the isolated executor."""

    def __init__(self, init, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(5)
            self.sock.connect(path)
            self.sock.settimeout(None)
        except OSError as exc:
            self.sock.close()
            raise ToolUnavailable(f"tool executor unavailable: {type(exc).__name__}") from None
        self.reader, self.writer = self.sock.makefile("rb"), self.sock.makefile("wb")
        self.lines = _Lines(self.reader, self.writer)
        self.lines.send(init)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # unblocks the reader thread before its buffered file is closed
        except OSError:
            pass
        for item in (self.reader, self.writer, self.sock):
            try:
                item.close()
            except OSError:
                pass


def transport(init):
    path = os.environ.get("TOOLS_EXECUTOR_SOCKET")
    if path:
        return ExecutorSocket(init, path)
    if os.environ.get("TOOLS_LOCAL_SANDBOX") == "1":
        return LocalSandbox(init)
    raise ToolUnavailable("no isolated tool executor is configured")


# -------------------------------------------------------------- broker
def host_allowed(host, allowed):
    host = (host or "").lower().rstrip(".")
    return any(host == entry or (entry.startswith("*.") and host.endswith(entry[1:])) for entry in allowed)


class Broker:
    """Performs the tool's fetches: manifest hosts only, https, public addresses, bounded count/size/time."""

    def __init__(self, manifest, deadline, fixtures=None):
        self.hosts, self.limits, self.deadline = manifest["network"], manifest["limits"], deadline
        self.fixtures, self.fetched = fixtures, []

    def check(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
            raise _Refused("only plain https URLs are allowed")
        if not host_allowed(parts.hostname, self.hosts):
            raise _Refused(f"host {parts.hostname!r} is not declared in the manifest")

    def fetch(self, url):
        if len(self.fetched) >= self.limits["max_fetches"]:
            raise _Refused("fetch limit reached")
        self.check(url)
        self.fetched.append(url)
        if self.fixtures is not None:
            fixture = self.fixtures.get(url)
            if fixture is None:
                raise _Refused("no test fixture for this URL")
            body = fixture["body"] if "body" in fixture else json.dumps(fixture.get("json"), ensure_ascii=False)
            return {"status": int(fixture.get("status", 200)), "url": url,
                    "content_type": fixture.get("content_type", "application/json"), "text": body}
        from inputs import UA, _check_host, _Blocked
        broker = self

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                broker.check(newurl)
                _check_host(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        limit = self.limits["max_fetch_kb"] * 1024
        timeout = min(10.0, self.deadline - time.monotonic())
        if timeout <= 0.5:
            raise _Refused("no time left for another fetch")
        try:
            _check_host(url)
            request = urllib.request.Request(url, headers={"user-agent": UA, "accept": "application/json, text/*;q=0.9"})
            try:
                response = urllib.request.build_opener(Redirect).open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                response = exc  # A 404 is an answer the tool should see, not a broker failure.
            with response:
                data = response.read(limit + 1)
                status, final = response.status if hasattr(response, "status") else response.code, response.geturl()
                ctype = response.headers.get("content-type") or ""
        except (_Blocked, urllib.error.URLError, OSError, ValueError) as exc:
            raise _Refused(f"fetch failed: {type(exc).__name__}") from None
        if len(data) > limit:
            raise _Refused("response exceeds max_fetch_kb")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            text = data.decode(charset, "replace")
        except LookupError:
            text = data.decode("utf-8", "replace")
        return {"status": status, "url": final, "content_type": ctype.split(";")[0].strip().lower(), "text": text}


class _Refused(Exception):
    pass


# ---------------------------------------------------------------- calls
def call(tool, args, fixtures=None, record=True):
    """Run one call. Returns {"ok", "outcome", "output", "error", "tool", "digest", "fetched", "ms"}.

    outcome: ok | declined (expected failure reported by the tool) | failed (crash, timeout, protocol violation)
    | rejected (invalid arguments; not the tool's fault) | unavailable (no executor).
    """
    manifest, started = tool["manifest"], time.monotonic()
    result = {"ok": False, "tool": manifest["id"], "digest": tool["digest"][:12], "output": None, "error": "", "fetched": []}

    def finish(outcome, error="", output=None):
        result.update(outcome=outcome, ok=outcome == "ok", error=str(error)[:500], output=output,
                      ms=int(1000 * (time.monotonic() - started)))
        if record and outcome in ("ok", "declined", "failed"):
            try:
                R.registry().record(tool["digest"], outcome, result["error"])
            except Exception:
                log.exception("could not record tool usage")
        log.info("tool %s %s outcome=%s fetches=%d ms=%d", manifest["id"], result["digest"], outcome,
                 len(result["fetched"]), result["ms"])
        return result

    problems = R.validate(manifest["input_schema"], args)
    if problems:
        return finish("rejected", "; ".join(problems))
    limits = manifest["limits"]
    deadline = started + limits["timeout_s"] + 1
    broker = Broker(manifest, deadline, fixtures)
    result["fetched"] = broker.fetched
    init = {"api": R.API, "files": {k: v for k, v in tool["files"].items() if k.endswith(".py")}, "args": args,
            "limits": limits}
    try:
        channel = transport(init)
    except ToolUnavailable as exc:
        return finish("unavailable", exc)
    try:
        for _ in range(MAX_MESSAGES):
            message = channel.lines.receive(deadline)
            op = message.get("op")
            if op == "fetch" and isinstance(message.get("url"), str) and isinstance(message.get("id"), int):
                try:
                    reply = {"id": message["id"], "ok": True, **broker.fetch(message["url"])}
                except _Refused as exc:
                    reply = {"id": message["id"], "ok": False, "error": str(exc)}
                channel.lines.send(reply)
            elif op == "result":
                if message.get("ok") is True:
                    output = message.get("output")
                    size = len(json.dumps(output, ensure_ascii=False)) if isinstance(output, dict) else -1
                    if not 0 <= size <= limits["max_output_chars"]:
                        return finish("failed", f"output must be a JSON object of at most {limits['max_output_chars']} characters")
                    return finish("ok", output=output)
                if message.get("kind") == "declined":
                    return finish("declined", message.get("error") or "declined")
                result["trace"] = str(message.get("trace") or "")[:2000]
                return finish("failed", message.get("error") or "crash")
            else:
                raise _Violation("unexpected protocol message")
        raise _Violation("too many protocol messages")
    except (TimeoutError, _Violation, OSError) as exc:
        return finish("failed", exc if not isinstance(exc, OSError) else f"channel error: {type(exc).__name__}")
    finally:
        channel.close()


def _path(value, dotted):
    for part in dotted.split(".") if dotted else []:
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return KeyError
    return value


def run_tests(tool):
    """Run the declarative tests.json cases offline (fixtures instead of network) and check them here."""
    cases, report = R.tests_of(tool["files"]), []
    for case in cases:
        outcome = call(tool, case["args"], fixtures=case.get("fixtures") or {}, record=False)
        problems = []
        if "expect_error" in case:
            if outcome["outcome"] not in ("declined", "rejected") or str(case["expect_error"]) not in outcome["error"]:
                problems.append(f"expected an error containing {case['expect_error']!r}, got {outcome['outcome']}: {outcome['error']}")
        elif not outcome["ok"]:
            problems.append(f"{outcome['outcome']}: {outcome['error']}")
        else:
            for dotted, expected in (case.get("expect") or {}).items():
                actual = _path(outcome["output"], dotted)
                if actual != expected:
                    problems.append(f"{dotted}: expected {expected!r}, got {'missing' if actual is KeyError else repr(actual)}")
            for dotted, fragment in (case.get("expect_contains") or {}).items():
                actual = _path(outcome["output"], dotted)
                if actual is KeyError or str(fragment).casefold() not in str(actual).casefold():
                    problems.append(f"{dotted}: expected to contain {fragment!r}")
        report.append({"name": case["name"], "passed": not problems, "problems": problems, "trace": outcome.get("trace", "")})
    return {"tool": tool["id"], "digest": tool["digest"][:12], "total": len(report),
            "passed": sum(case["passed"] for case in report), "cases": report}
