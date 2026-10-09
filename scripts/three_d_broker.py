"""Forced SSH command for a dedicated render-only account. No shell, paths, or code execution."""
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

ROOT = Path(os.environ.get("THREED_QUEUE_DIR", "/var/lib/explain-bot-render/queue"))
MAX_BYTES = 500_000_000


def atomic(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value))
    temp.replace(path)


def respond(value):
    sys.stdout.buffer.write(json.dumps(value).encode() + b"\n")
    sys.stdout.buffer.flush()


def job_path(value):
    if not re.fullmatch(r"[0-9a-f]{32}", str(value)):
        raise ValueError("invalid job")
    path = ROOT / value
    if path.is_symlink() or not path.is_dir():
        raise ValueError("job expired")
    return path


def heartbeat(job=None):
    atomic(ROOT / "worker.json", {"time":time.time(), "busy":job, "worker":"windows-gpu"})


def serve():
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    while line := sys.stdin.buffer.readline(8192):
        try:
            if not line.endswith(b"\n"):
                raise ValueError("oversized command")
            req = json.loads(line)
            op = req.get("op")
            if op == "poll":
                heartbeat()
                claimed = None
                for folder in sorted(ROOT.iterdir(), key=lambda p:p.name):
                    if not re.fullmatch(r"[0-9a-f]{32}", folder.name) or folder.is_symlink() or not folder.is_dir():
                        continue
                    if not (folder / "ready").is_file() or (folder / "done.json").exists():
                        continue
                    request = json.loads((folder / "request.json").read_text())
                    if request["expires"] < time.time():
                        shutil.rmtree(folder)
                        continue
                    try:
                        with (folder / "claimed").open("x") as f:
                            f.write(str(time.time()))
                    except FileExistsError:
                        continue
                    claimed = folder.name
                    break
                heartbeat(claimed)
                respond({"job":claimed})
            elif op == "heartbeat":
                job = req.get("job")
                if not re.fullmatch(r"[0-9a-f]{32}", str(job)):
                    raise ValueError("invalid job")
                heartbeat(job)
                active = bool(job and (ROOT / str(job) / "request.json").is_file())
                if active:
                    path = job_path(job)
                    active = json.loads((path / "request.json").read_text())["expires"] > time.time()
                respond({"active":active})
            elif op == "download":
                path = job_path(req.get("job")) / "input.zip"
                size = path.stat().st_size
                if size > MAX_BYTES:
                    raise ValueError("job too large")
                respond({"size":size})
                with path.open("rb") as f:
                    shutil.copyfileobj(f, sys.stdout.buffer)
                sys.stdout.buffer.flush()
            elif op == "upload":
                path = job_path(req.get("job"))
                size = int(req.get("size", 0))
                if not 0 < size <= MAX_BYTES:
                    raise ValueError("result too large")
                respond({"ready":True})
                with (path / "output.tmp").open("wb") as f:
                    remaining = size
                    while remaining:
                        chunk = sys.stdin.buffer.read(min(remaining, 1 << 20))
                        if not chunk:
                            raise EOFError("incomplete upload")
                        f.write(chunk)
                        remaining -= len(chunk)
                (path / "output.tmp").replace(path / "output.zip")
                atomic(path / "done.json", {"ok":True})
                respond({"ok":True})
            elif op == "failed":
                atomic(job_path(req.get("job")) / "done.json", {"error":str(req.get("error", "render failed"))[:300]})
                respond({"ok":True})
            else:
                raise ValueError("unknown operation")
        except Exception as exc:
            respond({"error":str(exc)[:200]})


if __name__ == "__main__":
    serve()
