"""Runs ONE library tool call inside the sandbox: no credentials, no direct network, no child processes.

usage: python -I tool_harness.py   (protocol: JSON lines on stdin/stdout)
  in : {"api": 1, "files": {"tool.py": source, ...}, "args": {...}, "limits": {...}}
  out: {"op": "fetch", "id": n, "url": ...}  -> in: {"id": n, "ok": true, "status": 200, "url", "content_type", "text"}
  out: {"op": "result", "ok": true, "output": {...}}  or  {"op": "result", "ok": false, "kind": "declined"|"crash", "error"}
The parent validates every message; nothing a tool writes here is trusted. Tool `print` output goes to stderr.
"""
import importlib.abc
import importlib.util
import json
import os
import resource
import sys
import traceback

MAX_INIT = 4_000_000


class ToolDeclined(Exception):
    """An expected, user-explainable failure: not found, unsupported input, source unavailable."""


class FetchError(Exception):
    pass


class Response:
    def __init__(self, reply):
        self.status, self.url = int(reply.get("status") or 0), str(reply.get("url") or "")
        self.content_type, self.text = str(reply.get("content_type") or ""), str(reply.get("text") or "")

    @property
    def ok(self):
        return 200 <= self.status < 300

    def json(self):
        return json.loads(self.text)


class Context:
    """The only API a tool gets. ctx.fetch goes through the trusted broker, which enforces the manifest hosts."""

    def __init__(self, channel, limits):
        self._channel, self._seq, self.limits = channel, 0, dict(limits)

    def fetch(self, url):
        self._seq += 1
        self._channel.send({"op": "fetch", "id": self._seq, "url": str(url)[:2000]})
        reply = self._channel.receive()
        if reply.get("id") != self._seq:
            raise FetchError("protocol error")
        if not reply.get("ok"):
            raise FetchError(str(reply.get("error") or "fetch failed")[:300])
        return Response(reply)

    def fetch_json(self, url):
        response = self.fetch(url)
        if not response.ok:
            raise FetchError(f"HTTP {response.status}")
        return response.json()

    @staticmethod
    def fail(message):
        raise ToolDeclined(str(message)[:500])


class Channel:
    def __init__(self):
        # Keep private copies of the protocol pipes, then point fds 0/1 elsewhere so `print` cannot corrupt the protocol.
        self._in = os.fdopen(os.dup(0), "r", encoding="utf-8")
        self._out = os.fdopen(os.dup(1), "w", encoding="utf-8")
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.dup2(2, 1)
        sys.stdin, sys.stdout = open(os.devnull), sys.stderr

    def send(self, message):
        self._out.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._out.flush()

    def receive(self, limit=MAX_INIT):
        line = self._in.readline(limit)
        if not line.endswith("\n"):
            raise SystemExit(3)
        return json.loads(line)


class _Sources(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Import the tool's own modules from memory: the executed code is exactly what the parent verified."""

    def __init__(self, files):
        self.files = {name[:-3]: text for name, text in files.items() if name.endswith(".py")}

    def find_spec(self, name, path=None, target=None):
        return importlib.util.spec_from_loader(name, self) if name in self.files and name != "tool" else None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.files[module.__name__], module.__name__ + ".py", "exec"), module.__dict__)


def _deny(*args, **kwargs):
    raise PermissionError("direct network access is disabled; use ctx.fetch()")


def _restrict(limits):
    memory = int(limits.get("memory_mb") or 512) << 20
    seconds = int(limits.get("timeout_s") or 20) + 1
    for name, value in (("RLIMIT_AS", memory), ("RLIMIT_CPU", seconds), ("RLIMIT_FSIZE", 8 << 20),
                        ("RLIMIT_NOFILE", 32), ("RLIMIT_CORE", 0), ("RLIMIT_NPROC", 0)):
        if hasattr(resource, name):
            resource.setrlimit(getattr(resource, name), (value, value))
    if sys.platform.startswith("linux"):
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE=0: concurrent calls cannot ptrace or read each other.
    # Defense in depth only: the executor container has no network at all, and the broker owns every request.
    import socket
    for name in ("connect", "connect_ex", "sendto", "sendmsg", "bind"):
        setattr(socket.socket, name, _deny)
    socket.create_connection = socket.getaddrinfo = _deny


def main():
    channel = Channel()
    request = channel.receive()
    if request.get("api") != 1 or "tool.py" not in (request.get("files") or {}):
        channel.send({"op": "result", "ok": False, "kind": "crash", "error": "bad request"})
        return
    _restrict(request.get("limits") or {})
    try:
        sources = _Sources(request["files"])
        sys.meta_path.insert(0, sources)
        module = type(sys)("tool")
        exec(compile(request["files"]["tool.py"], "tool.py", "exec"), module.__dict__)
        if not callable(getattr(module, "run", None)):
            raise TypeError("tool.py must define run(args, ctx)")
        output = module.run(request.get("args") or {}, Context(channel, request.get("limits") or {}))
        if not isinstance(output, dict):
            raise TypeError("run() must return a dict")
        channel.send({"op": "result", "ok": True, "output": json.loads(json.dumps(output, ensure_ascii=False))})
    except ToolDeclined as exc:
        channel.send({"op": "result", "ok": False, "kind": "declined", "error": str(exc)})
    except BaseException as exc:  # noqa: BLE001 - every failure of untrusted code becomes a result
        lines = [ln for ln in traceback.format_exc().strip().splitlines() if "tool_harness.py" not in ln]
        channel.send({"op": "result", "ok": False, "kind": "crash",
                      "error": f"{type(exc).__name__}: {str(exc)[:300]}", "trace": "\n".join(lines[-12:])[:2000]})


if __name__ == "__main__":
    main()
