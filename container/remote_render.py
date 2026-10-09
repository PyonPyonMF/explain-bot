"""Optional private render queue; unavailable/failed workers always fall back to the CPU."""
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
import zipfile

log = logging.getLogger("remote-render")


def root():
    return Path(os.environ.get("THREED_QUEUE_DIR", "/render-queue"))


def status():
    if os.environ.get("THREED_REMOTE", "0") != "1":
        return {}
    try:
        state = json.loads((root() / "worker.json").read_text())
        if time.time() - float(state["time"]) < 25:
            return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    temp.chmod(0o644)
    temp.replace(path)


def render(base, work, run_blender, progress):
    """Return encoded silent clips, or None. Never consume the server fallback deadline."""
    state = status()
    if not state or state.get("busy"):
        return None
    queue = root()
    lock = queue / "reserved"
    try:
        lock.mkdir()
    except FileExistsError:
        # Bot restart/crash recovery; longer than any allowed remote job.
        if time.time() - lock.stat().st_mtime < 1000:
            return None
        lock.rmdir()
        try:
            lock.mkdir()
        except FileExistsError:
            return None
    job = queue / uuid.uuid4().hex
    try:
        job.mkdir(mode=0o700)
        if os.getuid() == 0:
            os.chown(job, int(os.environ["THREED_WORKER_UID"]), int(os.environ["THREED_WORKER_GID"]))
        progress("🖥️ Готовлю качественный рендер на домашнем компьютере…")
        fps = max(12, min(30, int(os.environ.get("THREED_REMOTE_FPS", "12"))))
        high = {**base, "mode":"bake", "engine":"BLENDER_EEVEE_NEXT", "quality":True,
                "fps":fps, "width":1920, "height":1080, "samples":32,
                "out_dir":str(work / "3d-baked"), "scenes":[dict(s) for s in base["scenes"]]}
        # Resample the existing audio envelope to the worker's frame rate.
        for scene in high["scenes"]:
            old = scene.get("mouth", [])
            if old:
                scene["mouth"] = [old[min(len(old)-1, int(i * base["fps"] / fps))]
                                  for i in range(round(scene["duration"] * fps))]
        baked = run_blender(high, work, 120)
        package = job / "input.zip"
        with zipfile.ZipFile(package, "w", zipfile.ZIP_STORED) as z:
            for scene in baked["scenes"]:
                name = scene["blend"]
                if not re.fullmatch(r"scene_\d+\.blend", name):
                    raise ValueError("invalid baked scene name")
                z.write(Path(high["out_dir"]) / name, name)
            z.writestr("manifest.json", json.dumps({"fps":fps, "scenes":baked["scenes"]}))
        package.chmod(0o644)
        atomic_json(job / "request.json", {"expires":time.time()+float(os.environ.get("THREED_REMOTE_TIMEOUT", "600")),
                                            "created":time.time()})
        (job / "ready").touch()
        (job / "ready").chmod(0o644)
        start = time.monotonic()
        deadline = start + min(600, float(os.environ.get("THREED_REMOTE_TIMEOUT", "600")))
        progress("🎬 Рендерю 1080p на видеокарте домашнего компьютера…")
        while time.monotonic() < deadline:
            done = job / "done.json"
            if done.exists():
                result = json.loads(done.read_text())
                if result.get("error"):
                    raise RuntimeError(str(result["error"])[:300])
                output = work / "3d-remote"
                output.mkdir(exist_ok=True)
                expected = {f'scene_{s["index"]}.mp4' for s in baked["scenes"]}
                with zipfile.ZipFile(job / "output.zip") as z:
                    if set(z.namelist()) != expected or sum(i.file_size for i in z.infolist()) > 300_000_000:
                        raise ValueError("invalid render result archive")
                    for name in expected:
                        (output / name).write_bytes(z.read(name))
                for scene in baked["scenes"]:
                    clip = output / f'scene_{scene["index"]}.mp4'
                    media = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                            "stream=width,height,duration,nb_frames", "-of", "json", str(clip)],
                                           capture_output=True, text=True, check=True, timeout=15)
                    stream = json.loads(media.stdout)["streams"][0]
                    if (stream["width"], stream["height"]) != (1920, 1080) or int(stream["nb_frames"]) != scene["frame_count"]:
                        raise ValueError("incomplete GPU clip")
                return {**baked, "clips":{s["index"]:str(output / f'scene_{s["index"]}.mp4') for s in baked["scenes"]}, "fps":fps}
            alive = status()
            if not alive:
                raise RuntimeError("personal computer disconnected")
            claimed = job / "claimed"
            if claimed.exists() and time.time() - claimed.stat().st_mtime > 10 and alive.get("busy") != job.name:
                raise RuntimeError("worker restarted before finishing this job")
            if time.monotonic() - start > 25 and not (job / "claimed").exists():
                raise RuntimeError("worker did not claim the render")
            time.sleep(2)
        raise TimeoutError("personal computer render deadline")
    except Exception as exc:
        log.warning("GPU render unavailable, using server: %s", str(exc)[:250])
        progress("⚡ Компьютер недоступен или рендер прервался — запускаю быстрый рендер на codervm…")
        return None
    finally:
        if job.exists():
            atomic_json(job / "cancelled", {})
            shutil.rmtree(job, ignore_errors=True)
        try:
            lock.rmdir()
        except OSError:
            pass
