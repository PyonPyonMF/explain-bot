"""YouTube audio-first ingestion, self-hosted PO tokens, resumable ASR and opt-in visual samples."""
import contextlib
import hashlib
import html
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse

log = logging.getLogger("youtube")
CHUNK_SECONDS = 300
MAX_TEXT = 120_000
MAX_FRAMES = 24
CACHE_VERSION = 1


class YouTubeError(Exception):
    pass


def video_id(url):
    try:
        p = urllib.parse.urlsplit(str(url))
        port = p.port
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or p.username or p.password or port not in (None, 80, 443):
        return None
    host = re.sub(r"^(?:www|m|music)\.", "", (p.hostname or "").lower())
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        vid = urllib.parse.parse_qs(p.query).get("v", [""])[0]
        match = re.match(r"^/(?:shorts|live|embed|v)/([^/?#]+)", p.path)
        vid = vid or (match[1] if match else "")
    else:
        return None
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or "") else None


def frames_requested(text):
    from render_modes import _request_words
    words = _request_words(text)
    for match in re.finditer(r"\b(?:покадрово|по\s+кадрам|frame[ -]by[ -]frame|analy[sz]e\s+(?:the\s+)?frames)\b", words):
        if not re.search(r"\b(?:не|без|do not|don't|without)[^.!?;,\n]{0,60}$|\b(?:что значит|что означает)\s*$", words[max(0,match.start()-80):match.start()]):
            return True
    return False


def frame_times(text, duration):
    """A dense explicit range, or bounded samples over the full recording; never claim every frame was read."""
    from render_modes import _request_words
    words = _request_words(text)
    match = re.search(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)", words)
    def seconds(stamp):
        numbers = list(map(int, stamp.split(":")))
        if any(n >= 60 for n in numbers[1:]):
            raise YouTubeError("invalid_range")
        return sum(n * 60 ** i for i, n in enumerate(reversed(numbers)))
    start, end = (seconds(match[1]), seconds(match[2])) if match else (0, duration)
    if not 0 <= start < end <= duration + .5:
        raise YouTubeError("invalid_range")
    count = min(MAX_FRAMES, max(2, math.ceil((end-start)*2)))
    times = [start + (i + .5)*(end-start)/count for i in range(count)]
    return times, {"start": start, "end": end, "sample_count": count,
                   "notice": "Only these timestamped samples were inspected; events between frames may be missed."}


def limits():
    def bounded(key, default, low, high):
        try:
            return max(low, min(high, int(os.environ.get(key) or default)))
        except ValueError:
            return default
    return {"seconds": bounded("YOUTUBE_MAX_SECONDS", 3600, 60, 7200),
            "audio_bytes": 96 * 1024**2, "video_bytes": 256 * 1024**2,
            "cache_bytes": bounded("YOUTUBE_CACHE_MB", 512, 128, 4096) * 1024**2,
            "ttl": bounded("YOUTUBE_CACHE_HOURS", 24, 1, 168) * 3600}


def _atomic(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def _lock(directory, timeout=300):
    """Process-safe cache lock; no API tokens or interaction credentials are persisted."""
    if directory.is_symlink():
        raise YouTubeError("invalid_cache")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        import fcntl
        directory.chmod(0o700)
        with (directory / ".lock").open("a") as handle:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise YouTubeError("busy")
                    time.sleep(.2)
            try:
                directory.touch()
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    else:
        # Production is Linux. Windows callers use separate test caches.
        yield


def _prune(root, current):
    config = limits()
    entries = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and re.fullmatch(r"[A-Za-z0-9_-]{11}", p.name)]
    sizes = {p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink()) for p in entries}
    total = sum(sizes.values())
    for path in sorted(entries, key=lambda p:p.stat().st_mtime):
        if path == current or path.resolve().parent != root.resolve():
            continue
        expired = time.time() - path.stat().st_mtime > config["ttl"]
        if not expired and total <= config["cache_bytes"]:
            continue
        try:
            with _lock(path, timeout=0):
                shutil.rmtree(path)
                total -= sizes[path]
        except (YouTubeError, FileNotFoundError):
            continue


def _progress(job, text):
    if job.get("response_message_id") or (job.get("application_id") and job.get("token")):
        import pipeline as P
        P.research_progress(job, text)


def _download(vid, kind, directory):
    """Run the downloader without bot/provider secrets in its environment, with a wall-time bound."""
    config = limits()
    spec = {"id": vid, "kind": kind, "directory": str(directory), "max_seconds": config["seconds"],
            "max_bytes": config["video_bytes"] if kind == "video" else config["audio_bytes"],
            "pot_url": os.environ.get("YOUTUBE_POT_URL", "http://pot:4416")}
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
    env.update(LANG="C.UTF-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1", HOME=str(directory), DENO_NO_UPDATE_CHECK="1")
    process = subprocess.Popen([sys.executable, __file__, "--download", json.dumps(spec)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=os.name == "posix")
    try:
        out, _ = process.communicate(timeout=90 if kind == "metadata" else 360)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
        raise YouTubeError("timeout") from None
    if len(out) > 1_000_000:
        raise YouTubeError("invalid_response")
    try:
        reply = json.loads(out)
    except ValueError:
        raise YouTubeError("download") from None
    if process.returncode or reply.get("error"):
        raise YouTubeError(reply.get("error") or "download")
    if kind != "metadata":
        path = directory / reply["filename"]
        if path.resolve().parent != directory.resolve() or not path.is_file() or path.stat().st_size > spec["max_bytes"]:
            raise YouTubeError("too_large")
    return reply


def _metadata(vid, directory):
    path = directory / "metadata.json"
    cached = _read(path)
    if cached and time.time()-path.stat().st_mtime < limits()["ttl"]:
        return cached
    meta = _download(vid, "metadata", directory)
    if cached:
        for file in directory.iterdir():
            if file.is_file() and not file.is_symlink() and file.resolve().parent == directory.resolve() and (
                    file.name.startswith(("audio.", "video.", "speech-"))):
                file.unlink()
    _atomic(path, meta)
    return meta


def _media(vid, kind, directory):
    saved = _read(directory / (kind + ".json"))
    if saved:
        path = directory / str(saved.get("filename", ""))
        if path.resolve().parent == directory.resolve() and path.is_file():
            return path
    saved = _download(vid, kind, directory)
    _atomic(directory / (kind + ".json"), saved)
    return directory / saved["filename"]


def _transcribe(path, duration, directory, job):
    import speech
    model = os.environ.get("ELEVENLABS_STT_MODEL") or "scribe_v2"
    version = hashlib.sha256(f"{CACHE_VERSION}:{model}:{CHUNK_SECONDS}".encode()).hexdigest()[:12]
    parts, languages, completed, error = [], [], 0, None
    count = math.ceil(duration/CHUNK_SECONDS)
    for index, start in enumerate(range(0, math.ceil(duration), CHUNK_SECONDS)):
        seconds = min(CHUNK_SECONDS, duration-start)
        cache = directory / f"speech-{version}-{index}.json"
        part = _read(cache)
        _progress(job, f"🎧 Распознаю речь YouTube: часть {index+1}/{count}…")
        if part is None:
            try:
                with tempfile.TemporaryDirectory(prefix="audio-", dir=directory) as work:
                    chunk = Path(work) / "speech.flac"
                    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-protocol_whitelist", "file,pipe",
                                    "-ss", str(start), "-i", str(path), "-map", "0:a:0", "-t", str(seconds),
                                    "-vn", "-ac", "1", "-ar", "16000", "-af", "asetpts=PTS-STARTPTS", "-c:a", "flac", str(chunk)],
                                   capture_output=True, timeout=90, check=True)
                    if chunk.stat().st_size > 32*1024**2:
                        raise YouTubeError("too_large")
                    response = speech.request_transcript(chunk.read_bytes())
                for word in response.get("words") or []:
                    word["speaker_id"] = f"part{index+1}/" + str(word.get("speaker_id") or "unknown")[:30]
                part = {"text": speech.timed_text(response, offset=start), "language": str(response.get("language_code") or "unknown")[:16]}
                _atomic(cache, part)
            except Exception as exc:
                error = str(exc)[:100] if isinstance(exc, (YouTubeError, speech.SpeechError)) else type(exc).__name__
                log.warning("YouTube ASR part unavailable: %s", error)
                break
        if sum(len(p) for p in parts) + len(part.get("text", "")) > MAX_TEXT:
            error = "text_limit"
            break
        parts.append(part.get("text", ""))
        languages.append(part.get("language", "unknown"))
        completed = start+seconds
    text = "\n".join(parts).strip()
    partial = completed < duration-.1
    return {"status": "ok" if text else "failed" if error else "no_speech", "text": text,
            "partial": partial, "duration": duration, "seconds": completed,
            "language": languages[0] if languages else "unknown",
            "partial_notice": f"Transcribed {completed:.0f} of {duration:.0f} seconds; remaining speech was not read.",
            "notice": "Speaker labels are local to each part; equal labels in different parts do not establish identity."}


def _frames(path, request, duration, directory):
    times, coverage = frame_times(request, duration)
    frames = []
    with tempfile.TemporaryDirectory(prefix="frames-", dir=directory) as work:
        for index, timestamp in enumerate(times):
            dest = Path(work) / f"{index}.jpg"
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-protocol_whitelist", "file,pipe",
                            "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
                            "-q:v", "3", str(dest)], capture_output=True, timeout=30, check=True)
            frames.append({"data": dest.read_bytes(), "seconds": round(timestamp, 3)})
    return frames, coverage


def read(url, job, want_frames=False):
    vid = video_id(url)
    if not vid:
        raise YouTubeError("invalid_url")
    if os.environ.get("YOUTUBE_MEDIA", "1") != "1":
        raise YouTubeError("disabled")
    root = Path(os.environ.get("YOUTUBE_CACHE_DIR") or Path(os.environ.get("DATA_DIR") or "/data") / "youtube")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        root.chmod(0o700)
    directory = root / vid
    with contextlib.suppress(OSError):
        _prune(root, directory)  # Another request may have evicted an inactive entry concurrently.
    with _lock(directory):
        for path in directory.iterdir():
            if path.is_dir() and not path.is_symlink() and path.resolve().parent == directory.resolve() and path.name.startswith(("audio-", "frames-")):
                shutil.rmtree(path)  # Abandoned temporary decode files from a prior interrupted run.
        _progress(job, "⬇️ Загружаю аудио YouTube…")
        meta = _metadata(vid, directory)
        duration = float(meta["duration"])
        if not math.isfinite(duration) or not 0 < duration <= limits()["seconds"]:
            raise YouTubeError("duration_limit")
        if os.environ.get("SPEECH_RECOGNITION", "1") != "1":
            transcript = {"status": "disabled", "partial": False}
        else:
            try:
                audio = _media(vid, "audio", directory)
                transcript = _transcribe(audio, duration, directory, job)
            except YouTubeError as exc:
                transcript = {"status": "failed", "partial": False, "error": str(exc)}
        frames, coverage, frame_error = [], None, None
        if want_frames:
            try:
                frame_times(job.get("question") or job.get("text") or "", duration)  # Validate before downloading video.
                _progress(job, "🖼️ Извлекаю кадры YouTube по твоему запросу…")
                video = _media(vid, "video", directory)
                frames, coverage = _frames(video, job.get("question") or job.get("text") or "", duration, directory)
            except Exception as exc:
                frame_error = str(exc) if isinstance(exc, YouTubeError) else type(exc).__name__
        directory.touch()
        used = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
        if used > limits()["cache_bytes"]:
            saved = _read(directory/"video.json") or {}
            path = directory / str(saved.get("filename", ""))
            if path.is_file() and path.resolve().parent == directory.resolve():
                path.unlink()
            (directory/"video.json").unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        _prune(root, directory)
    return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": meta.get("title") or vid,
            "duration": duration, "transcript": transcript, "frames": frames, "coverage": coverage, "frame_error": frame_error}


def error_message(reason, russian=True):
    if not russian:
        return "YouTube could not be processed (" + str(reason)[:50] + "). No transcript was inferred from its thumbnail or description."
    messages = {"duration_limit": f"Ролик превышает лимит {limits()['seconds']//60} минут. Пришли более короткий фрагмент.",
                "access": "YouTube не разрешил скачать этот ролик. Пришли сам файл или другой доступный источник.",
                "unavailable": "Ролик YouTube недоступен для скачивания.", "live": "Прямые эфиры пока не обрабатываются; нужна завершённая запись.",
                "too_large": "Медиафайл YouTube превышает лимит загрузки.", "no_space": "Для загрузки YouTube сейчас недостаточно свободного места.",
                "timeout": "YouTube не удалось загрузить за отведённое время.", "busy": "Этот ролик уже обрабатывается. Попробуй чуть позже.",
                "no_speech": "В аудио YouTube не удалось распознать речь.", "speech_disabled": "Распознавание речи сейчас отключено.",
                "invalid_range": "Неверный диапазон кадров. Укажи время внутри ролика, например 00:10–00:20.",
                "disabled": "Обработка YouTube сейчас отключена."}
    return messages.get(str(reason), "Не удалось прочитать аудио YouTube. По названию и обложке содержимое не угадываю.")


def _download_worker(spec):
    import yt_dlp
    from yt_dlp.plugins import load_all_plugins
    load_all_plugins()
    vid, kind = spec["id"], spec["kind"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) or kind not in ("metadata", "audio", "video"):
        raise YouTubeError("invalid_url")
    directory = Path(spec["directory"])
    class QuietLogger:
        def debug(self, message): pass
        def warning(self, message): pass
        def error(self, message): pass
    def validate(info, *, incomplete=False):
        if incomplete:
            return None
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            raise YouTubeError("live")
        duration = float(info.get("duration") or 0)
        if not math.isfinite(duration) or not 0 < duration <= spec["max_seconds"]:
            raise YouTubeError("duration_limit")
    def progress(state):
        if state.get("downloaded_bytes", 0) > spec["max_bytes"]:
            raise YouTubeError("too_large")
        if shutil.disk_usage(directory).free < 256*1024**2:
            raise YouTubeError("no_space")
    options = {"logger": QuietLogger(), "quiet": True, "no_warnings": True, "noplaylist": True,
               "cachedir": False, "js_runtimes": {"deno": {}}, "socket_timeout": 20, "retries": 1,
               "fragment_retries": 1, "concurrent_fragment_downloads": 1, "match_filter": validate,
               "extractor_args": {"youtube": {"player_client": ["mweb"]},
                                  "youtubepot-bgutilhttp": {"base_url": [spec["pot_url"]]}},
               "outtmpl": str(directory / (kind + ".%(ext)s")), "max_filesize": spec["max_bytes"],
               "progress_hooks": [progress], "overwrites": False,
               "format": "bestvideo[height<=720][protocol=https]/best[height<=720]" if kind == "video" else "bestaudio[protocol=https]/bestaudio"}
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(f"https://www.youtube.com/watch?v={vid}", download=kind != "metadata")
        if not info:
            raise YouTubeError("unavailable")
        validate(info)
        result = {"id": vid, "title": str(info.get("title") or vid)[:250], "duration": float(info["duration"])}
        if kind != "metadata":
            result["filename"] = Path(downloader.prepare_filename(info)).name
        return result


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--download":
            raise YouTubeError("invalid_request")
        print(json.dumps(_download_worker(json.loads(sys.argv[2])), ensure_ascii=False))
    except Exception as exc:
        message = str(exc).lower()
        code = str(exc) if isinstance(exc, YouTubeError) else ("access" if any(s in message for s in ("sign in", "403", "private", "members-only", "age-restricted")) else
                "unavailable" if any(s in message for s in ("unavailable", "not available", "removed")) else "download")
        print(json.dumps({"error": code[:100]}))
        sys.exit(1)
