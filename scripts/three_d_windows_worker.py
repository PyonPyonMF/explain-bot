"""Windows GPU worker. Outbound SSH only, dedicated key, no Discord/API credentials."""
import argparse
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

log = logging.getLogger("3d-worker")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Broker:
    def __init__(self, cfg):
        self.proc = subprocess.Popen(["ssh", "-T", "-p", str(cfg.get("port", 2222)), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + cfg["known_hosts"],
            "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-i", cfg["key"], cfg["host"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)

    def reply(self):
        line = self.proc.stdout.readline(8192)
        if not line:
            raise ConnectionError("SSH disconnected")
        value = json.loads(line)
        if value.get("error"):
            raise RuntimeError(value["error"])
        return value

    def request(self, **value):
        self.proc.stdin.write(json.dumps(value).encode() + b"\n")
        self.proc.stdin.flush()
        return self.reply()

    def download(self, job, dest):
        size = int(self.request(op="download", job=job)["size"])
        if not 0 < size <= 500_000_000:
            raise ValueError("input too large")
        with dest.open("wb") as f:
            while size:
                chunk = self.proc.stdout.read(min(size, 1 << 20))
                if not chunk:
                    raise EOFError("download interrupted")
                f.write(chunk)
                size -= len(chunk)

    def upload(self, job, src):
        self.request(op="upload", job=job, size=src.stat().st_size)
        with src.open("rb") as f:
            shutil.copyfileobj(f, self.proc.stdin)
        self.proc.stdin.flush()
        self.reply()

    def close(self):
        self.proc.kill()
        self.proc.wait()


def extract_package(archive, work):
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 5 or sum(i.file_size for i in z.infolist()) > 800_000_000:
            raise ValueError("package exceeds size limit")
        for info in z.infolist():
            if info.filename != "manifest.json" and not re.fullmatch(r"scene_[0-3]\.blend", info.filename):
                raise ValueError("unexpected package file")
            (work / info.filename).write_bytes(z.read(info))
    manifest = json.loads((work / "manifest.json").read_text())
    if not 12 <= int(manifest["fps"]) <= 30 or not 1 <= len(manifest["scenes"]) <= 4:
        raise ValueError("invalid timeline")
    for scene in manifest["scenes"]:
        if not re.fullmatch(r"scene_[0-3]\.blend", scene["blend"]) or not 1 <= int(scene["frame_count"]) <= 1800:
            raise ValueError("invalid scene")
    return manifest


def run_job(broker, job, cfg):
    with tempfile.TemporaryDirectory(prefix="render-", dir=cfg["work"]) as directory:
        work = Path(directory)
        broker.download(job, work / "input.zip")
        manifest = extract_package(work / "input.zip", work)
        outputs = []
        for scene in manifest["scenes"]:
            output = work / (Path(scene["blend"]).stem + ".mp4")
            config = work / "render.json"
            config.write_text(json.dumps({"fps":manifest["fps"], "frames":scene["frame_count"], "output":str(output)}))
            args = [cfg["blender"], "--background", "--factory-startup", "--disable-autoexec",
                    str(work / scene["blend"]), "--python", str(Path(__file__).with_name("three_d_gpu_render.py")), "--", str(config)]
            log.info("Rendering %s / %s", job, scene["blend"])
            with (Path(cfg["work"]) / "last-blender.log").open("w", encoding="utf-8") as file:
                proc = subprocess.Popen(args, stdout=file, stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
                try:
                    started = time.monotonic()
                    while proc.poll() is None:
                        if time.monotonic() - started > 600 or not broker.request(op="heartbeat", job=job)["active"]:
                            raise TimeoutError("job cancelled or expired")
                        time.sleep(3)
                    if proc.returncode or not output.is_file() or output.stat().st_size < 1000:
                        raise RuntimeError("Blender failed; see last-blender.log")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
            outputs.append(output)
        archive = work / "output.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as z:
            for output in outputs:
                z.write(output, output.name)
        broker.upload(job, archive)
        log.info("Completed %s", job)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    Path(cfg["work"]).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(Path(cfg["work"]) / "worker.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    while True:
        broker = None
        try:
            broker = Broker(cfg)
            while True:
                job = broker.request(op="poll")["job"]
                if job:
                    try:
                        run_job(broker, job, cfg)
                    except Exception as exc:
                        log.exception("Render failed")
                        broker.request(op="failed", job=job, error=str(exc))
                time.sleep(5)
        except Exception as exc:
            log.warning("Reconnect: %s", type(exc).__name__)
            time.sleep(5)
        finally:
            if broker:
                broker.close()


if __name__ == "__main__":
    main()
