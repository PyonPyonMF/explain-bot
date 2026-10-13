"""Private local speech transport and a shared ElevenLabs quota circuit breaker."""
import http.client
import json
import logging
import os
from pathlib import Path
import socket
import threading
import time
import urllib.error

log = logging.getLogger("speech-backend")
_lock = threading.Lock()
_blocked_until = 0.0


class LocalSpeechError(Exception):
    pass


def configured():
    return bool(os.environ.get("LOCAL_SPEECH_SOCKET") and os.environ.get("LOCAL_SPEECH_TOKEN"))


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.environ["LOCAL_SPEECH_SOCKET"])


def request(endpoint, body, content_type):
    if not configured():
        raise LocalSpeechError("local_speech_not_configured")
    connection = Connection("localhost", timeout=900)
    try:
        connection.request("POST", endpoint, body, {"Content-Type":content_type,
                           "Authorization":"Bearer "+os.environ["LOCAL_SPEECH_TOKEN"]})
        response = connection.getresponse()
        data = response.read(32*1024**2+1)
        if response.status != 200 or len(data)>32*1024**2:
            raise LocalSpeechError("local_speech_failed")
        return data
    except (OSError, http.client.HTTPException):
        raise LocalSpeechError("local_speech_offline") from None
    finally:
        connection.close()


def transcribe(audio):
    try:
        result = json.loads(request("/stt", audio, "audio/flac"))
        if not isinstance(result,dict) or not isinstance(result.get("text"),str):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise LocalSpeechError("local_speech_invalid_response") from None


def synthesize(text):
    return request("/tts", json.dumps({"text":text},ensure_ascii=False).encode(), "application/json")


def _state_path():
    return Path(os.environ.get("DATA_DIR") or "/data")/"speech-provider.json"


def blocked():
    global _blocked_until
    with _lock:
        try:
            _blocked_until = max(_blocked_until,float(json.loads(_state_path().read_text())["until"]))
        except (OSError,ValueError,KeyError,TypeError):
            pass
        return _blocked_until > time.time()


def block():
    global _blocked_until
    with _lock:
        _blocked_until = time.time()+86400
        try:
            path = _state_path(); path.parent.mkdir(parents=True,exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"until":_blocked_until}),encoding="utf-8")
            temp.chmod(0o600); temp.replace(path)
        except OSError:
            pass


def run(kind, remote, local):
    mode = os.environ.get(kind.upper()+"_PROVIDER","auto").lower()
    if mode not in ("auto","local","elevenlabs"):
        raise LocalSpeechError("invalid_speech_provider")
    if mode=="local" or (mode=="auto" and configured() and blocked()):
        return local()
    try:
        return remote()
    except Exception as exc:
        code = str(exc)
        retryable = code in ("quota_exceeded","invalid_api_key","missing_permissions","missing_key") or code.startswith("http_")
        retryable = retryable or isinstance(exc,(urllib.error.URLError,TimeoutError))
        if mode!="auto" or not configured() or not retryable:
            raise
        if code in ("quota_exceeded","invalid_api_key","missing_permissions","missing_key"):
            block()
        log.warning("%s switching to local speech; provider=%s",kind,code if code in ("quota_exceeded","invalid_api_key","missing_permissions","missing_key") else type(exc).__name__)
        return local()
