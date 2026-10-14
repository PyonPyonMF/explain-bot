"""Isolated tool executor: runs tool_harness.py per connection and relays its protocol to the bot.

Deploy it as its own container with network_mode: none, a read-only filesystem, no capabilities, no secrets and
no data volumes (see compose.gateway.yml). The only shared resource is the Unix socket. The bot's broker performs
every network request on the tool's behalf, so a tool can reach nothing the manifest did not declare.
"""
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading

from tool_runtime import APP_DIR, MAX_LINE, harness_env

log = logging.getLogger("tool-executor")


def relay(source, target, done):
    try:
        while True:
            line = source.readline(MAX_LINE + 1)
            if not line or not line.endswith(b"\n"):
                break
            target.write(line)
            target.flush()
    except (OSError, ValueError):
        pass
    done.set()


def serve_one(conn, slots):
    home, proc, relays = None, None, []
    with conn, slots:
        reader, writer = conn.makefile("rb"), conn.makefile("wb")
        try:
            conn.settimeout(10)
            first = reader.readline(MAX_LINE + 1)
            conn.settimeout(None)
            init = json.loads(first)
            if not isinstance(init, dict):
                raise ValueError("bad request")
            timeout = min(60, int((init.get("limits") or {}).get("timeout_s") or 20)) + 2
            home = tempfile.mkdtemp(prefix="tool-")
            proc = subprocess.Popen([sys.executable, "-I", os.path.join(APP_DIR, "tool_harness.py")],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    cwd=home, env=harness_env(home), start_new_session=True)
            proc.stdin.write(first)
            proc.stdin.flush()
            done = threading.Event()
            relays = [threading.Thread(target=relay, args=(reader, proc.stdin, done), daemon=True),
                      threading.Thread(target=relay, args=(proc.stdout, writer, done), daemon=True)]
            for thread in relays:
                thread.start()
            if not done.wait(timeout):
                log.warning("tool call exceeded %ss", timeout)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("tool call failed: %s", type(exc).__name__)
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if proc:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            for thread in relays:
                thread.join(2)
            for stream in (reader, writer) + ((proc.stdin, proc.stdout) if proc else ()):
                try:
                    stream.close()
                except OSError:
                    pass
            if home:
                shutil.rmtree(home, ignore_errors=True)


def serve(path, stop=None):
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o660)
    server.listen(8)
    server.settimeout(0.5)
    slots = threading.BoundedSemaphore(max(1, int(os.environ.get("TOOLS_EXECUTOR_CONCURRENCY") or 2)))
    log.info("tool executor ready")
    try:
        while not (stop and stop.is_set()):
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=serve_one, args=(conn, slots), daemon=True).start()
    finally:
        server.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    secrets = [name for name in os.environ if name.endswith(("_KEY", "_TOKEN", "_SECRET"))]
    if secrets:
        raise SystemExit("refusing to start: the tool executor must not receive credentials (" + ", ".join(sorted(secrets)) + ")")
    if sys.platform.startswith("linux"):
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE=0: tool code running as the same user cannot ptrace us.
    serve(os.environ.get("TOOLS_EXECUTOR_SOCKET") or "/tool-socket/executor.sock")


if __name__ == "__main__":
    main()
