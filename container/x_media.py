"""Bounded X/Twitter video ingestion: prefer a separate HLS audio rendition, frames only on request."""
import base64
import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import youtube as media


class XMediaError(Exception):
    pass


def cdn_url(value, base=""):
    try:
        url = urllib.parse.urljoin(base, str(value))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "video.twimg.com" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError()
    except ValueError:
        raise XMediaError("invalid_media_url") from None
    return url


class CDNRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from inputs import _check_host
        cdn_url(newurl); _check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, deadline):
    from inputs import _check_host, UA
    url = cdn_url(url); _check_host(url)
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise XMediaError("timeout")
    return urllib.request.build_opener(CDNRedirect).open(urllib.request.Request(url, headers={"user-agent": UA}), timeout=min(20, remaining))


def _get(url, deadline, max_bytes=2*1024**2):
    with _open(url, deadline) as response:
        data = response.read(max_bytes+1)
    if len(data) > max_bytes:
        raise XMediaError("too_large")
    return data


def attributes(line):
    return {key: value.strip('"') for key, value in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line)}


def audio_playlist(master, base):
    candidates = []
    for line in master.splitlines():
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = attributes(line)
            if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                rates = re.findall(r"\d+", attrs.get("GROUP-ID", ""))
                candidates.append((int(rates[-1]) if rates else 0, cdn_url(attrs["URI"], base)))
    return max(candidates)[1] if candidates else None


def parse_audio_playlist(text, base, max_seconds):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise XMediaError("live_or_invalid_playlist")
    segments, init, duration = [], None, 0.0
    for line in lines:
        if line.startswith(("#EXT-X-BYTERANGE", "#EXT-X-DISCONTINUITY")):
            raise XMediaError("unsupported_playlist")
        if line.startswith("#EXT-X-KEY:") and attributes(line).get("METHOD") != "NONE":
            raise XMediaError("unsupported_playlist")
        if line.startswith("#EXT-X-MAP:"):
            value = cdn_url(attributes(line).get("URI", ""), base)
            if init and init != value:
                raise XMediaError("unsupported_playlist")
            init = value
        elif line.startswith("#EXTINF:"):
            seconds = float(line.split(":",1)[1].split(",",1)[0])
            if not math.isfinite(seconds) or not 0 < seconds <= 30:
                raise XMediaError("invalid_duration")
            duration += seconds
        elif not line.startswith("#"):
            segments.append(cdn_url(line, base))
    if not init or not segments or len(segments) > 4096:
        raise XMediaError("unsupported_playlist")
    if duration > max_seconds+1:
        raise XMediaError("duration_limit")
    return init, segments, duration


def formats(item):
    candidates = []
    for entry in item.get("formats") or []:
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        try:
            url = cdn_url(entry["url"])
        except XMediaError:
            continue
        candidates.append({"url": url, "bitrate": int(entry.get("bitrate") or 0)})
    if item.get("video_url"):
        with contextlib.suppress(XMediaError):
            candidates.append({"url": cdn_url(item["video_url"]), "bitrate": 0})
    return list({row["url"]: row for row in candidates}.values())


def _file(url, destination, max_bytes, deadline):
    temp = destination.with_suffix(".part")
    try:
        with _open(url, deadline) as response, temp.open("wb") as out:
            length = int(response.headers.get("content-length") or 0)
            if length > max_bytes:
                raise XMediaError("too_large")
            used = 0
            while True:
                chunk = response.read(1024*1024)
                if not chunk:
                    break
                used += len(chunk)
                if used > max_bytes:
                    raise XMediaError("too_large")
                if time.monotonic() > deadline:
                    raise XMediaError("timeout")
                if shutil.disk_usage(destination.parent).free < 256*1024**2:
                    raise XMediaError("no_space")
                out.write(chunk)
        os.replace(temp, destination)
        destination.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def _audio_hls(url, directory, max_seconds, deadline):
    master = _get(url, deadline).decode("utf-8")
    selected = audio_playlist(master, url)
    if not selected:
        raise XMediaError("no_audio_rendition")
    init, segments, _ = parse_audio_playlist(_get(selected, deadline).decode("utf-8"), selected, max_seconds)
    target = directory/"audio.m4a"
    with tempfile.TemporaryDirectory(prefix="download-", dir=directory) as work:
        joined = Path(work)/"audio.mp4"
        used = 0
        with joined.open("wb") as out, concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for start in range(0, len(segments)+1, 12):
                urls = ([init]+segments)[start:start+12]
                for part in pool.map(lambda u:_get(u, deadline), urls):
                    used += len(part)
                    if used > media.limits()["audio_bytes"]:
                        raise XMediaError("too_large")
                    if shutil.disk_usage(directory).free < 256*1024**2:
                        raise XMediaError("no_space")
                    out.write(part)
        # Rebuild the MP4 index locally. FFmpeg never receives a remote manifest or unvalidated URL.
        subprocess.run(["ffmpeg","-nostdin","-y","-v","error","-protocol_whitelist","file,pipe","-i",str(joined),
                        "-map","0:a:0","-vn","-c:a","copy","-movflags","+faststart",str(target)],
                       capture_output=True, timeout=90, check=True)
    target.chmod(0o600)
    return target


def _video(item, directory, deadline, audio_fallback=False):
    dest = directory/("source.mp4" if audio_fallback else "video.mp4")
    if dest.is_file():
        return dest
    if audio_fallback and (directory/"video.mp4").is_file():
        return directory/"video.mp4"
    mp4 = [row for row in formats(item) if urllib.parse.urlsplit(row["url"]).path.endswith(".mp4")]
    def height(row):
        match = re.search(r"/(\d+)x(\d+)/", row["url"])
        return int(match[2]) if match else 0
    mp4.sort(key=lambda r:(height(r) or 10000, r["bitrate"]), reverse=not audio_fallback)
    if not audio_fallback:
        mp4 = [r for r in mp4 if height(r) <= 720] or mp4
    last = "unavailable"
    for row in mp4:
        try:
            _file(row["url"], dest, media.limits()["video_bytes"], deadline)
            return dest
        except XMediaError as exc:
            last = str(exc)
            if last not in ("too_large", "unavailable"):
                raise
    raise XMediaError(last)


def _probe(path):
    raw = subprocess.check_output(["ffprobe","-v","error","-protocol_whitelist","file,pipe","-show_entries",
          "format=duration:stream=codec_type,duration","-of","json",str(path)],timeout=30)
    info = json.loads(raw)
    audio = next((s for s in info.get("streams",[]) if s.get("codec_type")=="audio"),None)
    duration = float((audio or {}).get("duration") or (info.get("format") or {}).get("duration") or 0)
    if not math.isfinite(duration) or duration<=0:
        raise XMediaError("invalid_duration")
    return duration, bool(audio)


def read(item, source_url, job, want_frames=False):
    max_seconds = media.limits()["seconds"]
    duration = float(item.get("duration") or 0)
    if duration and (not math.isfinite(duration) or duration > max_seconds+1):
        raise XMediaError("duration_limit")
    identity = source_url + "|" + str(item.get("id") or item.get("video_url") or "")
    key = "x-" + base64.urlsafe_b64encode(hashlib.sha256(identity.encode()).digest()).decode()[:11]
    root = Path(os.environ.get("YOUTUBE_CACHE_DIR") or Path(os.environ.get("DATA_DIR") or "/data")/"youtube")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix": root.chmod(0o700)
    directory = root/key
    with contextlib.suppress(OSError): media._prune(root,directory)
    with media._lock(directory):
        record = directory/"x-source.json"
        saved = media._read(record)
        if saved and (saved.get("identity")!=identity or time.time()-record.stat().st_mtime > media.limits()["ttl"]):
            for path in directory.iterdir():
                if path.is_file() and not path.is_symlink() and path.name != ".lock": path.unlink()
        for path in directory.iterdir():
            if path.is_dir() and not path.is_symlink() and path.name.startswith(("download-","audio-","frames-")) and path.resolve().parent==directory.resolve():
                shutil.rmtree(path)
        deadline = time.monotonic()+360
        audio = directory/"audio.m4a"
        media._progress(job,"⬇️ Загружаю аудио видео из X…")
        if not audio.is_file():
            hls = next((r["url"] for r in formats(item) if urllib.parse.urlsplit(r["url"]).path.endswith(".m3u8")),None)
            if hls:
                try:
                    audio = _audio_hls(hls,directory,max_seconds,deadline)
                except XMediaError as exc:
                    if str(exc) not in ("no_audio_rendition","unsupported_playlist"): raise
                    audio = _video(item,directory,deadline,audio_fallback=True)
            else:
                audio = _video(item,directory,deadline,audio_fallback=True)
        actual, has_audio = _probe(audio)
        if actual > max_seconds+1: raise XMediaError("duration_limit")
        duration = min(actual,max_seconds)
        if has_audio and audio.name!="audio.m4a":
            target=directory/"audio.m4a"
            subprocess.run(["ffmpeg","-nostdin","-y","-v","error","-protocol_whitelist","file,pipe","-i",str(audio),
                            "-map","0:a:0","-vn","-c:a","copy",str(target)],capture_output=True,timeout=90,check=True)
            if audio.name=="source.mp4": audio.unlink()
            audio=target; audio.chmod(0o600)
        if not has_audio:
            transcript = {"status":"no_audio","partial":False}
        elif os.environ.get("SPEECH_RECOGNITION","1")!="1":
            transcript = {"status":"disabled","partial":False}
        else:
            transcript = media._transcribe(audio,duration,directory,job,platform="X")
        media._atomic(record,{"identity":identity,"source_url":source_url,"duration":duration})
        frames, coverage, frame_error = [], None, None
        if want_frames:
            try:
                request = job.get("question") or job.get("text") or ""
                media.frame_times(request,duration)
                media._progress(job,"🖼️ Извлекаю кадры видео из X по твоему запросу…")
                video = _video(item,directory,time.monotonic()+360)
                frames, coverage = media._frames(video,request,duration,directory)
            except Exception as exc:
                frame_error = str(exc) if isinstance(exc,(XMediaError,media.YouTubeError)) else type(exc).__name__
        directory.touch()
    with contextlib.suppress(OSError): media._prune(root,directory)
    return {"url":source_url,"duration":duration,"transcript":transcript,"frames":frames,"coverage":coverage,"frame_error":frame_error}


def error_message(reason):
    if str(reason)=="duration_limit": return "Речь в видео из X превышает лимит 120 минут."
    if str(reason)=="no_audio": return "В видео из X нет звуковой дорожки; кадры можно запросить отдельно."
    return media.error_message(reason).replace("YouTube","видео из X")
