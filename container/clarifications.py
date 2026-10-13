"""Bounded, owner-scoped request choices. No downloads or model calls before the user selects an action."""
import json
import os
import re
import time
import uuid

from render_modes import _request_words, mention_mode, response_format, text_task

TTL = 900
MAX_PENDING = 128
MAX_PER_USER = 5
ACTIONS = {
    "video": ("video", "explain", "2d"), "text": ("text", "explain", "2d"),
    "transcript": ("text", "transcript", "2d"), "summary": ("text", "summary", "2d"),
    "3d": ("video", "explain", "3d"),
    "research": ("video", "research", "2d"),
}
LABELS = {
    "ru": {"video": "Объяснить видео", "text": "Объяснить текстом", "transcript": "Транскрипция",
           "summary": "Краткое резюме", "3d": "3D-видео", "research": "Исследовать источники", "edit": "Уточнить запрос", "cancel": "Отмена"},
    "en": {"video": "Explain with video", "text": "Explain in text", "transcript": "Transcript",
           "summary": "Summary", "3d": "3D video", "research": "Research sources", "edit": "Edit request", "cancel": "Cancel"},
}


def reason(text, has_material=False):
    """Conservative ambiguity gate: clear questions, topics, known modes and bare sources keep their defaults."""
    if os.environ.get("CLARIFY_AMBIGUOUS", "1") != "1":
        return None
    words = re.sub(r"\s+", " ", _request_words(text)).strip(" .,!?:;—-")
    if re.search(r"\b(?:текстом\s+или\s+видео|видео\s+или\s+текстом|транскрипци[яю]\s+или\s+(?:резюме|саммари)|"
                 r"(?:резюме|саммари)\s+или\s+транскрипци[яю]|text\s+or\s+video|transcript\s+or\s+summary)\b", words):
        return "format"
    action_word = r"(?:об[ъь]?ясни(?:ть)?|расшифруй|расшифровать|пересказать|транскрипци[яю]|резюме|саммари|summary|transcript)"
    if re.search(rf"\b{action_word}\s+(?:или|or)\s+(?:(?:сделай|кратко)\s+)?{action_word}\b", words):
        return "format"
    from youtube import video_id
    from links import _x_match
    if has_material and words in ("разбери", "разбери это", "разбери видео", "посмотри", "посмотри это") and any(
            video_id(u.rstrip(".,;:!?)»")) or _x_match(u.rstrip(".,;:!?)»")) for u in re.findall(r"https?://[^\s<>]+", text)):
        return None
    if mention_mode(text)[1] == "3d" or text_task(text) != "explain" or response_format(text) == "text":
        return None
    if re.search(r"\b(?:сделай|сгенерируй|запиши)\s+(?:мне\s+)?(?:видео|ролик)\b|\bлучше\s+видео\b", words):
        return None
    if not words:
        return None if has_material or re.search(r"https?://", text) or re.search(r"[\w]", text) else "action"
    if words in ("выбор", "выбрать действие", "options", "choose", "не знаю что выбрать",
                 "сделай", "сделай это", "сделай что-нибудь", "сделай что нибудь", "сделай нормально",
                 "давай", "ну давай", "помоги", "помоги с этим", "обработай", "обработай это",
                 "разбери", "разбери это", "разбери видео", "посмотри", "посмотри это", "прочитай это",
                 "че тут", "что тут", "что это", "что здесь", "что тут говорят", "и что", "дальше",
                 "что с этим делать", "что можно с этим сделать", "давай что-нибудь с этим", "с этим что-нибудь сделай",
                 "do this", "do something", "help", "process this", "analyze this", "what now"):
        return "action"
    if re.match(r"^(?:сделай|обработай|поработай|разберись|do|process)\b", words):
        return "action"
    # A text cue that our format parser did not understand must not silently become a paid video.
    if words.endswith("текстом") and not re.search(r"\b(?:с|со|над|под|перед|между|называют|считают)(?:\s+[\w-]+(?:ым|им))?\s+текстом$", words):
        return "format"
    return None


def has_material(job):
    message = job.get("message") or {}
    return bool(job.get("followup") or job.get("kind") == "message" or message.get("attachments")
                or message.get("embeds") or re.search(r"https?://", str(job.get("question") or job.get("text") or "")))


def speech_possible(job):
    if job.get("followup") or re.search(r"https?://|\b(?:видео|ролик|аудио|речь|говорят|video|audio|speech)\b",
                                         str(job.get("question") or job.get("text") or ""), re.I):
        return True
    for message in (job.get("message") or {}, job.get("request_message") or {}):
        if message.get("message_reference"):
            return True  # Its source may be an older audio/video post in the quoted chain.
        if any(str(a.get("content_type") or "").startswith(("video/", "audio/")) or
               str(a.get("filename") or "").lower().endswith((".mp4", ".webm", ".mov", ".ogg", ".mp3", ".m4a", ".wav"))
               for a in message.get("attachments") or []):
            return True
        if re.search(r"https?://", str(message.get("content") or "")) or any(e.get("video") for e in message.get("embeds") or []):
            return True
    return False


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS clarifications (id TEXT PRIMARY KEY, owner TEXT, channel TEXT, "
               "created REAL, expires REAL, status TEXT, payload TEXT, claim TEXT, charged_day TEXT, charged INTEGER DEFAULT 0)")


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in
                ("token", "application_id", "bot_token", "api_key", "authorization", "response_message_id", "response_channel_id",
                 "prepared_inputs", "image_originals", "assets", "research", "research_assessment", "video_script", "image_story_plan")
                and not k.startswith("_clarification")}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def create(state, job, owner, channel, request, why, origin="mention", prompt="", detail="", credit=False):
    from conversation import excerpt
    key, now = uuid.uuid4().hex, time.time()
    record = {"job": _clean(job), "request": str(request)[:4000], "reason": why, "origin": origin,
              "revision": 0, "picker_id": None, "prompt": excerpt(prompt, 280), "detail": excerpt(detail, 700), "quota_credit": bool(credit)}
    payload = json.dumps(record, ensure_ascii=False)
    if len(payload.encode()) > 2_000_000:
        raise ValueError("clarification snapshot exceeds limit")
    with state.lock:
        state.db.execute("DELETE FROM clarifications WHERE expires<=?", (now,))
        state.db.execute("INSERT INTO clarifications (id,owner,channel,created,expires,status,payload) VALUES (?,?,?,?,?,'pending',?)",
                         (key, str(owner), str(channel), now, now + TTL, payload))
        state.db.execute("DELETE FROM clarifications WHERE id IN (SELECT id FROM clarifications WHERE owner=? ORDER BY created DESC LIMIT -1 OFFSET ?)",
                         (str(owner), MAX_PER_USER))
        state.db.execute("DELETE FROM clarifications WHERE id IN (SELECT id FROM clarifications ORDER BY created DESC LIMIT -1 OFFSET ?)", (MAX_PENDING,))
        state.db.commit()
    return key, record


def bind(state, key, message_id):
    with state.lock:
        row = state.db.execute("SELECT payload FROM clarifications WHERE id=? AND status='pending'", (key,)).fetchone()
        if row:
            payload = json.loads(row[0]); payload["picker_id"] = str(message_id)
            state.db.execute("UPDATE clarifications SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), key))
            state.db.commit()


def discard(state, key):
    with state.lock:
        state.db.execute("DELETE FROM clarifications WHERE id=? AND status='pending'", (key,))
        state.db.commit()


def _language(record):
    return "ru" if str(os.environ.get("BOT_LANG") or record["job"].get("locale") or "ru").startswith("ru") else "en"


def offered(record):
    if record.get("reason") == "evidence":
        return set()
    job = record["job"]
    if not has_material(job) and not job.get("context_messages") and reason(record["request"]) and len(record["request"].split()) <= 4:
        return set()  # Choosing a format cannot fix a missing subject; ask the user to supply it first.
    return {"video", "text", "3d", "research"} | ({"transcript", "summary"} if speech_possible(record["job"]) else set())


def card(key, record):
    lang = _language(record)
    question = record["request"].replace("`", "'").replace("\n", " ")[:350]
    if lang == "ru":
        content = ("**Запрос уточнён.**\nВыбери действие для него." if record.get("reason") == "edited" else
                   "**Что нужно сделать?**\nЗапрос можно понять по-разному. Выбери действие или уточни вопрос.")
        content += ("\n\nЗапрос: `" + question + "`") if question else "\n\nМожно разобрать текущее обсуждение или уточнить, какой материал нужен."
        content += "\n\nВыбор доступен только автору, 15 минут. До выбора обработка и расход лимита не начинаются."
    else:
        content = ("**Request updated.**\nChoose an action for it." if record.get("reason") == "edited" else
                   "**What would you like me to do?**\nThis request has several possible interpretations. Choose an action or edit the question.")
        content += ("\n\nRequest: `" + question + "`") if question else "\n\nChoose an action for this discussion, or specify the source in your question."
        content += "\n\nOnly the requester can choose, within 15 minutes. Processing and quota usage start after selection."
    actions = offered(record)
    if record.get("reason") == "evidence":
        content = ((record.get("detail") + "\n\n") if record.get("detail") else "")
        content += ("**Нужно уточнение**\n" if lang == "ru" else "**A clarification is needed**\n") + record.get("prompt", "")
        content += ("\n\nНажми «Уточнить запрос», чтобы добавить контекст или ссылку. Рендер не запущен; лимит повторно не спишется."
                    if lang == "ru" else "\n\nEdit the request to add context or a source. No video was rendered; your quota won't be charged again.")
    elif not actions:
        content = (("**Что нужно разобрать?**\nУточни тему или пришли материал. Кнопка «Уточнить запрос» откроет поле для вопроса."
                    if lang == "ru" else "**What should I work on?**\nSpecify the topic or provide the source. Edit request opens a question field.")
                   + ("\n\nЗапрос: `" + question + "`" if question and lang == "ru" else ""))
    rows = [["video", "text"]] if actions else []
    if "transcript" in actions:
        rows.append(["transcript", "summary"])
    rows.append((["research", "3d"] if actions else []) + ["edit", "cancel"])
    components = [{"type": 1, "components": [
        {"type": 2, "style": 4 if action == "cancel" else 2, "label": LABELS[lang][action],
         "custom_id": f"clarify:{key}:{record['revision']}:{action}"} for action in row]} for row in rows]
    return {"content": content, "components": components, "allowed_mentions": {"parse": []}}


class ChoiceError(Exception):
    pass


def _load_locked(state, key, actor, channel, message_id, revision):
    row = state.db.execute("SELECT owner,channel,expires,status,payload FROM clarifications WHERE id=?", (key,)).fetchone()
    if not row or row[2] <= time.time():
        raise ChoiceError("expired")
    if row[0] != str(actor) or row[1] != str(channel):
        raise ChoiceError("owner")
    if row[3] != "pending":
        raise ChoiceError("used")
    record = json.loads(row[4])
    if record["revision"] != revision:
        raise ChoiceError("stale")
    if record.get("picker_id") and str(message_id) != record["picker_id"]:
        raise ChoiceError("owner")
    return record


def _error(code, locale="ru"):
    ru = str(os.environ.get("BOT_LANG") or locale).startswith("ru")
    messages = {
        "expired": ("Выбор устарел. Отправь запрос заново.", "This choice expired. Send the request again."),
        "owner": ("Выбрать может только автор исходного запроса в том же канале.", "Only the original requester can choose in the original channel."),
        "used": ("Этот запрос уже принят или отменён.", "This request was already accepted or cancelled."),
        "stale": ("Запрос изменился. Используй новые кнопки под ним.", "The request changed. Use its updated buttons."),
        "busy": ("Сейчас очередь занята. Выбор сохранён — нажми ещё раз чуть позже.", "The queue is full. Your choice is still available; try again shortly."),
        "limit": ("Дневной лимит исчерпан. Обработка не запущена.", "The daily quota is exhausted. Processing has not started."),
        "invalid": ("Этот вариант недоступен. Используй кнопки под запросом.", "That option is unavailable. Use the request's buttons."),
    }
    return 200, {"type": 4, "data": {"content": messages.get(code, messages["invalid"])[0 if ru else 1],
                                    "flags": 64, "allowed_mentions": {"parse": []}}}


def handle(interaction, enqueue, state):
    from interactions import _base, _channel, _find_value
    data = interaction.get("data") or {}
    match = re.fullmatch(r"clarify:([a-f0-9]{32}):(\d+):(video|text|transcript|summary|research|3d|edit|cancel|form)", str(data.get("custom_id") or ""))
    if not match:
        return _error("invalid", interaction.get("locale", "ru"))
    key, revision, action = match[1], int(match[2]), match[3]
    if (action == "form") != (interaction.get("type") == 5):
        return _error("invalid")
    actor = ((interaction.get("member") or {}).get("user") or interaction.get("user") or {}).get("id")
    channel = _channel(interaction)
    picker_id = str((interaction.get("message") or {}).get("id") or "")
    try:
        with state.lock:
            record = _load_locked(state, key, actor, channel, picker_id, revision)
            lang, job = _language(record), record["job"]
            if not picker_id:
                return _error("invalid", lang)
            if not record.get("picker_id"):
                record["picker_id"] = picker_id
                state.db.execute("UPDATE clarifications SET payload=? WHERE id=?", (json.dumps(record, ensure_ascii=False), key)); state.db.commit()
            if action == "cancel":
                state.db.execute("UPDATE clarifications SET status='cancelled' WHERE id=?", (key,)); state.db.commit()
                return 200, {"type": 7, "data": {"content": "Отменено." if lang == "ru" else "Cancelled.", "components": [], "allowed_mentions": {"parse": []}}}
            if action == "edit":
                return 200, {"type": 9, "data": {
                    "custom_id": f"clarify:{key}:{revision}:form", "title": "Уточни запрос" if lang == "ru" else "Clarify your request",
                    "components": [{"type": 1, "components": [{"type": 4, "custom_id": "q", "style": 2,
                        "label": "Что сделать с этим материалом?" if lang == "ru" else "What should I do with this source?",
                        "value": record["request"][:4000], "required": True, "max_length": 4000}]}]}}
            if action == "form":
                question = (_find_value(data.get("components"), "q", limit=4000) or "").strip()
                if not question:
                    return _error("invalid")
                question, mode = mention_mode(question)
                job["question" if job.get("kind") == "message" else "text"] = question
                job.update(render_mode=mode, response_format=response_format(question), text_task=text_task(question))
                record.update(request=question, revision=revision + 1, reason="edited")
                state.db.execute("UPDATE clarifications SET payload=?,expires=? WHERE id=?", (json.dumps(record, ensure_ascii=False), time.time()+TTL, key)); state.db.commit()
                return 200, {"type": 7, "data": card(key, record)}
            if action not in offered(record) or not picker_id:
                return _error("invalid")
            limit = int(os.environ.get("DAILY_LIMIT") or 0)
            day = time.strftime("%Y-%m-%d", time.gmtime())
            quota = state.db.execute("SELECT n FROM quota WHERE user=? AND day=?", (str(actor), day)).fetchone()
            count = quota[0] if quota else 0
            pay = limit > 0 and not record.get("quota_credit")
            if pay and count >= limit:
                return _error("limit", lang)
            job = dict(job)
            job["response_format"], job["text_task"], job["render_mode"] = ACTIONS[action]
            if action == "research":
                job["response_format"] = response_format(record["request"])
            job["quota_paid"] = bool(record.get("quota_credit") or pay)
            job["clarification_rounds"] = int(job.get("clarification_rounds") or 0) + 1
            if record["origin"] == "mention":
                job.update(response_channel_id=str(channel), response_message_id=picker_id)
            else:
                job.update(_base(interaction))  # The click supplies a fresh token; none was persisted in SQLite.
            claim = uuid.uuid4().hex
            job.update(_clarification_id=key, _clarification_claim=claim)
            if not enqueue(job):
                return _error("busy", lang)
            try:
                if pay:
                    state.db.execute("INSERT OR REPLACE INTO quota VALUES (?,?,?)", (str(actor), day, count + 1))
                    state.db.execute("DELETE FROM quota WHERE day < date(?, '-2 day')", (day,))
                state.db.execute("UPDATE clarifications SET status='accepted',claim=?,charged_day=?,charged=? WHERE id=?",
                                 (claim, day, int(pay), key))
                state.db.commit()
            except Exception:
                state.db.rollback()
                raise
            content = ("Принято: " if lang == "ru" else "Selected: ") + LABELS[lang][action] + "."
            return 200, {"type": 7, "data": {"content": content, "components": [], "allowed_mentions": {"parse": []}}}
    except ChoiceError as exc:
        return _error(str(exc), interaction.get("locale", "ru"))


def rollback(state, job):
    """Release an accepted choice when Discord acknowledgement/submission failed before work began."""
    with state.lock:
        row = state.db.execute("SELECT owner,charged_day,charged FROM clarifications WHERE id=? AND status='accepted' AND claim=?",
                               (job.get("_clarification_id"), job.get("_clarification_claim"))).fetchone()
        if not row:
            return
        if row[2]:
            state.db.execute("UPDATE quota SET n=MAX(0,n-1) WHERE user=? AND day=?", (row[0], row[1]))
        state.db.execute("UPDATE clarifications SET status='pending',claim=NULL,charged=0 WHERE id=?", (job["_clarification_id"],))
        state.db.commit()


def pending_card(state, key):
    with state.lock:
        row = state.db.execute("SELECT payload FROM clarifications WHERE id=? AND status='pending' AND expires>?", (key, time.time())).fetchone()
        return card(key, json.loads(row[0])) if row else None
