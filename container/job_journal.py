"""Persistent journal of accepted jobs that reply in a channel, so a restart resumes them instead of dropping them.

Only jobs delivered through response_channel_id/response_message_id with the bot token are written. Jobs bound to
an interaction token are never written: tokens are not stored and expire after 15 minutes anyway.

  python -m job_journal status                 counts only, no request contents
  python -m job_journal wait-idle --timeout N  exit 0 once no job is running, 1 on timeout
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid

log = logging.getLogger("job_journal")

MAX_BYTES = 2_000_000
# Fields a job has when it is accepted. Everything derived while it runs (downloaded inputs, assets, scripts,
# generated animation code) is rebuilt on resume and never written.
FIELDS = {"kind", "text", "question", "message", "request_message", "channel_id", "context_before_id", "context_messages",
          "request_user_id", "request_origin", "bot_user_id", "locale", "render_mode", "response_format", "text_task",
          "private", "quota_paid", "clarification_rounds", "followup", "followup_message_id",
          "response_channel_id", "response_message_id"}
SECRETS = {"token", "application_id", "bot_token", "api_key", "authorization", "animation_code"}

STRINGS = {
    "ru": {"resumed": "♻️ Бот перезапускался — продолжаю работу над запросом…",
           "expired": "⚠️ Бот перезапускался и не смог продолжить этот запрос. Отправь его ещё раз.",
           "restarting": "⚠️ Бот перезапускается. Повтори запрос через минуту."},
    "en": {"resumed": "♻️ The bot restarted — continuing work on your request…",
           "expired": "⚠️ The bot restarted and could not continue this request. Please send it again.",
           "restarting": "⚠️ The bot is restarting. Please send the request again in a minute."},
}


def strings():
    return STRINGS.get(os.environ.get("BOT_LANG") or "ru", STRINGS["en"])


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in SECRETS}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def snapshot(job):
    job = {k: v for k, v in job.items() if k in FIELDS}
    if isinstance(job.get("followup"), dict) and "source" in job["followup"]:
        job["followup"] = {"stored": True}  # Saved explanation memory (up to 2 MB); reloaded from bot.sqlite.
    return _clean(job)


def resumable(job):
    return not job.get("token") and bool(job.get("response_channel_id") and job.get("response_message_id"))


class Journal:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        if os.name == "posix":
            os.chmod(path, 0o600)  # Request text and source snapshots must not be readable by generated render code.
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, state TEXT, attempts INTEGER, "
                        "created REAL, updated REAL, payload TEXT)")
        # One row of counts reported by the running bot, including jobs that are not journaled.
        self.db.execute("CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, running INTEGER, waiting INTEGER, "
                        "draining INTEGER, updated REAL)")
        self.db.commit()
        self.lock = threading.Lock()

    def _write(self, sql, args):
        try:
            with self.lock:
                self.db.execute(sql, args)
                self.db.commit()
        except sqlite3.Error as exc:
            log.warning("job journal write failed: %s", type(exc).__name__)

    def record(self, job):
        """Return a key for a resumable job, or None. A journal failure never blocks the job itself."""
        if not resumable(job):
            return None
        try:
            payload = json.dumps(snapshot(job), ensure_ascii=False)
            if len(payload.encode()) > MAX_BYTES:
                log.warning("job not journaled: snapshot exceeds %d bytes", MAX_BYTES)
                return None
            key, now = uuid.uuid4().hex, time.time()
            with self.lock:
                self.db.execute("INSERT INTO jobs VALUES (?, 'queued', 0, ?, ?, ?)", (key, now, now, payload))
                self.db.commit()
            return key
        except Exception as exc:
            log.warning("job not journaled: %s", type(exc).__name__)
            return None

    def start(self, key):
        if key:
            self._write("UPDATE jobs SET state='running', attempts=attempts+1, updated=? WHERE id=?", (time.time(), key))

    def requeue(self, key):
        self._write("UPDATE jobs SET state='queued', updated=? WHERE id=?", (time.time(), key))

    def finish(self, key):
        if key:
            self._write("DELETE FROM jobs WHERE id=?", (key,))

    def pending(self):
        """[(key, attempts, created, job or None)] oldest first."""
        with self.lock:
            rows = self.db.execute("SELECT id, attempts, created, payload FROM jobs ORDER BY created, rowid").fetchall()
        out = []
        for key, attempts, created, payload in rows:
            try:
                job = json.loads(payload)
            except (TypeError, ValueError):
                job = None
            out.append((key, attempts, created, job if isinstance(job, dict) else None))
        return out

    def activity(self, running, waiting, draining=False):
        self._write("INSERT OR REPLACE INTO activity VALUES (0, ?, ?, ?, ?)", (running, waiting, int(draining), time.time()))

    def counts(self):
        with self.lock:
            states = dict(self.db.execute("SELECT state, count(*) FROM jobs GROUP BY state").fetchall())
            live = self.db.execute("SELECT running, waiting, draining FROM activity").fetchone() or (0, 0, 0)
        return {"journal": {"queued": states.get("queued", 0), "running": states.get("running", 0)},
                "process": {"running": live[0], "waiting": live[1], "draining": bool(live[2])}}


_journal = None


def get():
    global _journal
    if _journal is None:
        data = os.environ.get("DATA_DIR") or "/data"
        try:
            _journal = Journal(os.path.join(data, "jobs.sqlite"))
        except Exception as e:
            log.warning("cannot use %s (%s); the job journal is kept in /tmp", data, e)
            _journal = Journal("/tmp/explain-bot/jobs.sqlite")
    return _journal


def notify(job, text):
    from pipeline import _safe_text
    _safe_text(job, text)


def abandon(job, text):
    """The job will not run: tell the requester and return the quota it used."""
    notify(job, text)
    if job.get("quota_paid") and job.get("request_user_id"):
        try:
            import interactions as I
            I.state().refund_quota(job["request_user_id"])
        except Exception as exc:
            log.warning("quota refund failed: %s", type(exc).__name__)


def _restore(job):
    if not (job.get("followup") or {}).get("stored"):
        return True
    import interactions as I
    job["followup"] = I.state().get_explanation(job.get("channel_id"), job.get("followup_message_id"))
    return job["followup"] is not None


def resume(reserve, submit, journal=None):
    """Resubmit jobs left by the previous process. reserve() -> bool claims capacity; submit(job, key) queues it.
    A job that already started JOB_MAX_ATTEMPTS times, or is older than JOB_RESUME_MAX_AGE, is dropped with a notice.
    Quota is never taken again: resubmission bypasses the request handlers that charge it."""
    journal, S = journal or get(), strings()
    max_attempts = int(os.environ.get("JOB_MAX_ATTEMPTS") or 2)
    max_age = float(os.environ.get("JOB_RESUME_MAX_AGE") or 6 * 3600)
    resumed = dropped = 0
    for key, attempts, created, job in journal.pending():
        if job is not None and attempts < max_attempts and time.time() - created <= max_age and _restore(job) and reserve():
            journal.requeue(key)
            notify(job, S["resumed"])
            submit(job, key)
            resumed += 1
            continue
        if job is not None:
            abandon(job, S["expired"])
        journal.finish(key)
        dropped += 1
    if resumed or dropped:
        log.info("journal: resumed %d jobs, dropped %d", resumed, dropped)
    return resumed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect the persistent job journal (counts only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("wait-idle").add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)
    journal = get()
    if args.command == "status":
        print(json.dumps(journal.counts()))
        return 0
    deadline = time.monotonic() + args.timeout
    while True:
        counts = journal.counts()
        if not (counts["process"]["running"] or counts["journal"]["running"]):
            return 0
        if time.monotonic() >= deadline:
            return 1
        time.sleep(min(2, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    sys.exit(main())
