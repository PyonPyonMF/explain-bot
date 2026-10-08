"""HTTP entry point of the render container.

POST /jobs  {application_id, token, kind, text, author, locale}  -> 202, job runs in the background
GET  /...   -> 200 (health check)

The container has no public address: only the Worker can reach it through the Durable Object binding.
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


def worker():
    from pipeline import handle_job  # import here so the server answers health checks at once
    while True:
        job = JOBS.get()
        log.info("job start (%s, %d chars, question=%s)", job.get("kind"), len(job.get("text") or ""), bool(job.get("question")))
        handle_job(job)
        JOBS.task_done()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"ok": True, "queued": JOBS.qsize()})

    def do_POST(self):
        if self.path != "/jobs":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("content-length") or 0)
            job = json.loads(self.rfile.read(n))
            for k in ("application_id", "token"):
                if not isinstance(job.get(k), str) or not job[k]:
                    raise ValueError(f"missing {k}")
            if not (job.get("text") or job.get("message")):
                raise ValueError("missing text or message")
        except Exception as e:
            return self._send(400, {"error": str(e)})
        try:
            JOBS.put_nowait(job)
        except queue.Full:
            return self._send(429, {"error": "busy"})
        self._send(202, {"queued": JOBS.qsize()})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT") or 8080)
    log.info("listening on %d", port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
