"""Discord interactions endpoint for self-hosting (no Cloudflare Worker).

Same behaviour as src/index.ts: Ed25519 signature check, /explain, "Explain (video)",
"Ask about this (video)" with a question form, daily limit, deferred reply, then the job
goes into the local queue. State (limits, open forms) is kept in SQLite / memory.
"""
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from render_modes import mention_mode, selected_mode, response_format, text_task

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger("interactions")

PING, APPLICATION_COMMAND, MESSAGE_COMPONENT, MODAL_SUBMIT = 1, 2, 3, 5
CHAT_INPUT, MESSAGE_COMMAND = 1, 3
PONG, CHANNEL_MESSAGE, DEFERRED, UPDATE_MESSAGE, MODAL = 1, 4, 5, 7, 9
EPHEMERAL = 64
ASK_COMMAND = "Ask about this (video)"
MAX_TEXT = 4000
FORM_TTL = 900

STRINGS = {
    "ru": {"empty": "В этом сообщении нечего объяснять.", "expired": "Форма устарела. Вызови команду ещё раз.",
           "limit": "Лимит: {} объяснений в день. Попробуй завтра.", "busy": "⚠️ Сервер сейчас занят. Попробуй через пару минут.",
           "modal_title": "Что объяснить в этом сообщении?", "modal_label": "Вопрос (можно пусто)",
           "modal_placeholder": "например: кто тут прав и почему?"},
    "en": {"empty": "There is nothing to explain in this message.", "expired": "This form expired. Run the command again.",
           "limit": "Limit: {} explanations per day. Try again tomorrow.", "busy": "⚠️ The server is busy. Try again in a few minutes.",
           "modal_title": "What should I explain here?", "modal_label": "Question (optional)",
           "modal_placeholder": "e.g. who is right here and why?"},
}


def _S():
    return STRINGS.get(os.environ.get("BOT_LANG") or "ru", STRINGS["en"])


# ------------------------------------------------------------ signature
_key = None


def verify(headers, body: bytes) -> bool:
    global _key
    sig, ts = headers.get("x-signature-ed25519"), headers.get("x-signature-timestamp")
    pub = os.environ.get("DISCORD_PUBLIC_KEY", "")
    if not sig or not ts or not pub:
        return False
    try:
        if _key is None:
            _key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub.strip()))
        _key.verify(bytes.fromhex(sig), ts.encode() + body)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------- state
class State:
    """Daily limits in SQLite (survives restarts); open question forms in memory."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        if os.name == "posix":
            os.chmod(path, 0o600)  # Source snapshots and narration must not be readable by generated render code.
        self.db.execute("CREATE TABLE IF NOT EXISTS quota (user TEXT, day TEXT, n INTEGER, PRIMARY KEY (user, day))")
        self.db.execute("CREATE TABLE IF NOT EXISTS explanations (channel TEXT, message TEXT, created REAL, payload TEXT, PRIMARY KEY (channel, message))")
        from clarifications import initialize
        initialize(self.db)
        self.db.commit()
        self.lock = threading.Lock()
        self.forms = {}

    def take_quota(self, user, limit):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.lock:
            row = self.db.execute("SELECT n FROM quota WHERE user=? AND day=?", (user, day)).fetchone()
            n = row[0] if row else 0
            if n >= limit:
                return False
            self.db.execute("INSERT OR REPLACE INTO quota VALUES (?, ?, ?)", (user, day, n + 1))
            self.db.execute("DELETE FROM quota WHERE day < date(?, '-2 day')", (day,))
            self.db.commit()
            return True

    def put_form(self, key, value):
        now = time.time()
        with self.lock:
            self.forms = {k: v for k, v in self.forms.items() if v[0] > now}
            self.forms[key] = (now + FORM_TTL, value)

    def get_form(self, key):
        with self.lock:
            v = self.forms.get(key)
            return v[1] if v and v[0] > time.time() else None

    def put_explanation(self, channel, message, record):
        payload = json.dumps(record, ensure_ascii=False)
        if len(payload.encode()) > 2_000_000:
            raise ValueError("explanation memory exceeds size limit")
        with self.lock:
            now = time.time()
            self.db.execute("INSERT OR REPLACE INTO explanations VALUES (?, ?, ?, ?)", (str(channel), str(message), now, payload))
            self.db.execute("DELETE FROM explanations WHERE created < ?", (now - 30 * 86400,))
            self.db.execute("DELETE FROM explanations WHERE rowid IN (SELECT rowid FROM explanations ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET 100)")
            self.db.commit()

    def get_explanation(self, channel, message):
        with self.lock:
            for _ in range(2):  # Continuation messages of a long text answer point to its first message.
                row = self.db.execute("SELECT payload FROM explanations WHERE channel=? AND message=? AND created>=?",
                                      (str(channel), str(message), time.time() - 30 * 86400)).fetchone()
                if not row:
                    return None
                record = json.loads(row[0])
                if not record.get("alias_of"):
                    return record
                message = record["alias_of"]
            return None


_state = None


def state():
    global _state
    if _state is None:
        data = os.environ.get("DATA_DIR") or "/data"
        try:
            _state = State(os.path.join(data, "bot.sqlite"))
        except Exception as e:
            log.warning("cannot use %s (%s); limits are kept in /tmp", data, e)
            _state = State("/tmp/explain-bot/bot.sqlite")
    return _state


# --------------------------------------------------------------- helpers
def _reply(obj):
    return 200, obj


def _ephemeral(text):
    return _reply({"type": CHANNEL_MESSAGE, "data": {"content": text, "flags": EPHEMERAL}})


def _base(i):
    user = ((i.get("member") or {}).get("user") or i.get("user") or {}).get("id")
    return {"application_id": str(i.get("application_id")), "token": str(i.get("token")), "locale": i.get("locale"),
            "request_user_id": str(user or "unknown"), "request_origin": "interaction"}


def _channel(i):
    return i.get("channel_id") or (i.get("channel") or {}).get("id")


def _option(i, name):
    for o in (i.get("data") or {}).get("options") or []:
        if o.get("name") == name:
            return o.get("value")
    return None


def _has_content(m):
    return bool((m.get("content") or "").strip() or m.get("attachments") or m.get("embeds") or m.get("sticker_items"))


def trim_message(m, depth=0):
    if not m:
        return None
    return {
        "id": m.get("id"), "content": str(m.get("content") or "")[:MAX_TEXT], "timestamp": m.get("timestamp"),
        "author": {"username": (m.get("author") or {}).get("username"), "global_name": (m.get("author") or {}).get("global_name")},
        "attachments": [{k: a.get(k) for k in ("url", "proxy_url", "filename", "content_type", "size")}
                        for a in (m.get("attachments") or [])[:10]],
        "embeds": [{"type": e.get("type"), "title": e.get("title"), "description": (e.get("description") or "")[:1000],
                    "url": e.get("url"), "provider": {"name": (e.get("provider") or {}).get("name")},
                    "image": {"url": e["image"]["url"]} if (e.get("image") or {}).get("url") else None,
                    "thumbnail": {"url": e["thumbnail"]["url"]} if (e.get("thumbnail") or {}).get("url") else None,
                    "video": {"url": e["video"]["url"]} if (e.get("video") or {}).get("url") else None}
                   for e in (m.get("embeds") or [])[:5]],
        "sticker_items": [{k: st.get(k) for k in ("id", "name", "format_type")} for st in (m.get("sticker_items") or [])[:3]],
        "message_reference": {k: m["message_reference"].get(k) for k in ("message_id", "channel_id", "type")}
        if m.get("message_reference") else None,
        "referenced_message": trim_message(m.get("referenced_message"), 1) if depth == 0 else None,
    }


def _find_value(components, cid, limit=500):
    for c in components or []:
        if isinstance(c, dict):
            if c.get("custom_id") == cid and isinstance(c.get("value"), str):
                return c["value"][:limit]
            v = _find_value(c.get("components"), cid, limit) or (_find_value([c["component"]], cid, limit) if c.get("component") else None)
            if v is not None:
                return v
    return None


def _fetch_message(cid, mid):
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        return None
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{cid}/messages/{mid}",
                                 headers={"authorization": f"Bot {token}", "user-agent": "DiscordBot (explain-bot, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ----------------------------------------------------------------- main
def handle(i, enqueue):
    """Return (status, response_json). enqueue(job) -> bool (False when the queue is full)."""
    S = _S()
    t = i.get("type")
    d = i.get("data") or {}
    if t == PING:
        return _reply({"type": PONG})
    if t in (MESSAGE_COMPONENT, MODAL_SUBMIT) and str(d.get("custom_id") or "").startswith("clarify:"):
        from clarifications import handle as clarify
        return clarify(i, enqueue, state())
    if t == APPLICATION_COMMAND and d.get("type") == CHAT_INPUT and d.get("name") == "help":
        from bot_help import help_text
        return _reply({"type": CHANNEL_MESSAGE, "data": {
            "content": help_text(_option(i, "section") or "overview", os.environ.get("BOT_LANG") or i.get("locale") or "ru", i.get("application_id")),
            "flags": EPHEMERAL, "allowed_mentions": {"parse": []}}})
    if t == APPLICATION_COMMAND and d.get("type") == CHAT_INPUT and d.get("name") == "objects":
        from object_library import describe
        return _ephemeral(describe(str(_option(i, "query") or "")[:200]))

    if t == APPLICATION_COMMAND and d.get("type") == MESSAGE_COMMAND and d.get("name") == ASK_COMMAND:
        m = ((d.get("resolved") or {}).get("messages") or {}).get(d.get("target_id"))
        if not m or not _has_content(m):
            return _ephemeral(S["empty"])
        state().put_form(i["id"], {"message": trim_message(m), "channel_id": _channel(i)})
        return _reply({"type": MODAL, "data": {
            "custom_id": f"ask:{i['id']}:{_channel(i) or ''}:{d.get('target_id')}", "title": S["modal_title"][:45],
            "components": [{"type": 1, "components": [{"type": 4, "custom_id": "q", "style": 2, "label": S["modal_label"][:45],
                                                        "placeholder": S["modal_placeholder"], "required": False,
                                                        "max_length": 500}]}]}})

    private = False
    if t == MODAL_SUBMIT and str(d.get("custom_id", "")).startswith("ask:"):
        _, iid, cid, mid = (str(d["custom_id"]).split(":") + ["", "", ""])[:4]
        saved = state().get_form(iid)
        if not saved and cid and mid:
            m = _fetch_message(cid, mid)
            saved = {"message": trim_message(m), "channel_id": cid} if m else None
        if not saved:
            return _ephemeral(S["expired"])
        job = {**_base(i), "kind": "message", "text": "", "message": saved["message"], "channel_id": saved["channel_id"],
               "question": _find_value(d.get("components"), "q") or ""}
        job["question"], job["render_mode"] = mention_mode(job["question"])
    elif t == APPLICATION_COMMAND and d.get("type") == CHAT_INPUT:
        q = _option(i, "query")
        if not isinstance(q, str) or not q.strip():
            return _ephemeral(S["empty"])
        n = max(0, min(50, int(_option(i, "messages") or 0)))
        job = {**_base(i), "kind": "query", "text": q[:MAX_TEXT], "channel_id": _channel(i), "context_messages": n}
        job["render_mode"] = selected_mode(_option(i, "mode"))
        private = _option(i, "private") is True
    elif t == APPLICATION_COMMAND and d.get("type") == MESSAGE_COMMAND:
        m = ((d.get("resolved") or {}).get("messages") or {}).get(d.get("target_id"))
        if not m or not _has_content(m):
            return _ephemeral(S["empty"])
        job = {**_base(i), "kind": "message", "text": "", "message": trim_message(m), "channel_id": _channel(i)}
    else:
        return 400, {"error": "unsupported interaction"}

    job["response_format"] = response_format(job.get("question") or job.get("text") or "")
    job["text_task"] = text_task(job.get("question") or job.get("text") or "")
    if t == APPLICATION_COMMAND and d.get("type") == CHAT_INPUT and d.get("name") == "research":
        job.update(response_format="text", text_task="research")
    job["private"] = private
    user = ((i.get("member") or {}).get("user") or {}).get("id") or (i.get("user") or {}).get("id") or "unknown"
    import clarifications as C
    request = job.get("question") or job.get("text") or ""
    why = None if job["text_task"] == "research" else C.reason(request, C.has_material(job))
    if why:
        key, record = C.create(state(), job, user, _channel(i), request, why, origin="interaction")
        return _reply({"type": CHANNEL_MESSAGE, "data": {**C.card(key, record), "flags": EPHEMERAL if private else 0}})
    limit = int(os.environ.get("DAILY_LIMIT") or 0)
    if limit > 0 and not state().take_quota(user, limit):
        return _ephemeral(S["limit"].format(limit))
    job["quota_paid"] = limit > 0
    if not enqueue(job):
        return _ephemeral(S["busy"])
    return _reply({"type": DEFERRED, "data": {"flags": EPHEMERAL} if private else {}})
