"""CPU speech service on localhost only; models and all inference stay on this computer."""
import argparse
import hmac
import io
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("local-speech")


class Engine:
    def __init__(self, config):
        from faster_whisper import WhisperModel
        from piper import PiperVoice
        self.stt_lock, self.tts_lock = threading.Lock(),threading.Lock()
        self.whisper = WhisperModel(config["whisper_model"],device="cpu",compute_type="int8",
                                    cpu_threads=4,num_workers=1,local_files_only=True)
        self.voices = {language:PiperVoice.load(path) for language,path in config["voices"].items()}

    def stt(self, audio):
        from faster_whisper.audio import decode_audio
        import numpy as np
        with self.stt_lock:
            decoded = decode_audio(io.BytesIO(audio),sampling_rate=16000)
            if len(decoded)>301*16000:
                raise ValueError("chunk_too_long")
            if len(decoded)==0 or float(np.max(np.abs(decoded)))<0.001:
                return {"text":"","words":[],"language_code":"unknown","provider":"local-whisper","diarized":False}
            segments,info = self.whisper.transcribe(decoded,beam_size=5,vad_filter=True,word_timestamps=True,
                                                    condition_on_previous_text=False)
            text, words = [],[]
            for segment in segments:
                text.append(segment.text.strip())
                for word in segment.words or []:
                    words.append({"text":word.word.strip(),"start":round(word.start,3),"end":round(word.end,3),"type":"word"})
            return {"text":" ".join(text),"words":words,"language_code":info.language,"provider":"local-whisper","diarized":False}

    def tts(self, text):
        if not isinstance(text,str) or not text.strip() or len(text)>8000:
            raise ValueError("invalid_text")
        language = "ru" if re.search("[А-Яа-яЁё]",text) else "en"
        with self.tts_lock:
            output = io.BytesIO()
            with wave.open(output,"wb") as wav:
                self.voices[language].synthesize_wav(text,wav)
            return output.getvalue()


def server(config,engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # Neither narration nor transcripts, request paths or bearer tokens belong in logs.

        def do_POST(self):
            self.connection.settimeout(30)
            if not hmac.compare_digest(self.headers.get("Authorization",""),"Bearer "+config["token"]):
                self.send_error(403); return
            try:
                size = int(self.headers.get("Content-Length","0"))
                if self.path not in ("/stt","/tts") or not 0<size<=32*1024**2 or (self.path=="/tts" and size>65536):
                    self.send_error(400); return
                body = self.rfile.read(size)
                if len(body)!=size:
                    raise ValueError("short_body")
                started = time.monotonic()
                if self.path=="/stt":
                    result = json.dumps(engine.stt(body),ensure_ascii=False).encode()
                    content_type = "application/json"
                else:
                    result = engine.tts(json.loads(body)["text"])
                    content_type = "audio/wav"
                self.send_response(200); self.send_header("Content-Type",content_type)
                self.send_header("Content-Length",str(len(result))); self.end_headers(); self.wfile.write(result)
                log.info("%s complete seconds=%.1f",self.path[1:],time.monotonic()-started)
            except Exception as exc:
                log.warning("speech request failed: %s",type(exc).__name__)
                try:self.send_error(500)
                except OSError:pass
    return ThreadingHTTPServer(("127.0.0.1",int(config.get("port",8766))),Handler)


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--config",required=True);args=parser.parse_args()
    path=Path(args.config);config=json.loads(path.read_text(encoding="utf-8-sig"))
    handler=RotatingFileHandler(path.parent/"worker.log",maxBytes=2_000_000,backupCount=2,encoding="utf-8")
    logging.basicConfig(level=logging.INFO,handlers=[handler],format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine=Engine(config);log.info("Local Whisper and Piper ready")
    server(config,engine).serve_forever()


if __name__=="__main__":main()
