"""Render a plan locally without any API: fake audio (a quiet tone) instead of ElevenLabs.

usage: python test_local.py sample_plan.json out.mp4
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from plan import validate_plan
from pipeline import render_all


def fake_audio(text, path):
    dur = max(1.0, len(text) / 14.0)  # about 14 characters per second of speech
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=330:duration={dur}",
                    "-af", "volume=0.05", "-c:a", "libmp3lame", "-b:a", "128k", path], check=True)
    return path, dur


def main(src, dst):
    plan = validate_plan(json.load(open(src)))
    if plan["dropped"]:
        print("DROPPED:", plan["dropped"])
    work = tempfile.mkdtemp()
    audio = [fake_audio(s["narration"], os.path.join(work, f"a{i}.mp3")) if s["narration"] else (None, 0.0)
             for i, s in enumerate(plan["scenes"])]
    t = time.time()
    out, dur = render_all(plan, audio, work)
    shutil.copy(out, dst)
    print(f"{len(plan['scenes'])} scenes, {dur:.1f}s video, render {time.time() - t:.1f}s, {os.path.getsize(dst)} bytes")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
