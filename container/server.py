"""HTTP entry point of the render container.

POST /interactions  Discord interactions (self-hosted mode; needs DISCORD_PUBLIC_KEY)
POST /jobs          job from the Cloudflare Worker -> 202 (disable with ENABLE_JOBS_ENDPOINT=0 when self-hosting)
GET  /...           health check

On Cloudflare, only the Worker can reach this server (through the Durable Object binding).
When self-hosting, expose only /interactions to the internet (see Caddyfile).
"""
import json
import logging
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

JOBS: "queue.Queue[dict]" = queue.Queue(maxsize=int(os.environ.get("MAX_QUEUED_JOBS") or 4))
JOBS_ENDPOINT = os.environ.get("ENABLE_JOBS_ENDPOINT", "1") == "1"
INTERACTIONS = bool(os.environ.get("DISCORD_PUBLIC_KEY"))
MAX_BODY = 1 << 20


def worker():
    from pipeline import handle_job  # import here so the server answers health checks at once
    while True:
        job = JOBS.get()
        log.info("job start (%s, %d chars, question=%s)", job.get("kind"), len(job.get("text") or ""), bool(job.get("question")))
        handle_job(job)
        JOBS.task_done()


def enqueue(job):
    try:
        JOBS.put_nowait(job)
        return True
    except queue.Full:
        return False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n)

    def do_GET(self):
        self._send(200, {"ok": True, "queued": JOBS.qsize()})

    def do_POST(self):
        if self.path == "/interactions" and INTERACTIONS:
            return self._interactions()
        if self.path == "/jobs" and JOBS_ENDPOINT:
            return self._jobs()
        return self._send(404, {"error": "not found"})

    def _interactions(self):
        import interactions as I
        try:
            body = self._body()
        except ValueError:
            return self._send(413, {"error": "too large"})
        if not I.verify({k.lower(): v for k, v in self.headers.items()}, body):
            return self._send(401, {"error": "invalid request signature"})
        try:
            code, obj = I.handle(json.loads(body), enqueue)
        except Exception:
            log.exception("interaction failed")
            code, obj = 500, {"error": "internal error"}
        self._send(code, obj)

    def _jobs(self):
        try:
            job = json.loads(self._body())
            for k in ("application_id", "token"):
                if not isinstance(job.get(k), str) or not job[k]:
                    raise ValueError(f"missing {k}")
            if not (job.get("text") or job.get("message")):
                raise ValueError("missing text or message")
        except Exception as e:
            return self._send(400, {"error": str(e)})
        if not enqueue(job):
            return self._send(429, {"error": "busy"})
        self._send(202, {"queued": JOBS.qsize()})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT") or 8080)
    log.info("listening on %d (interactions=%s, jobs endpoint=%s)", port, INTERACTIONS, JOBS_ENDPOINT)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
