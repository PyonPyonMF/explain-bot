"""Discord Gateway entry point: outbound connections only, no HTTP listener."""
import asyncio
from collections import OrderedDict
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time
import urllib.request

import discord

import interactions as I
import job_journal as J
from render_modes import mention_mode, response_format, text_task

log = logging.getLogger("gateway")

MENTION_STRINGS = {
    "ru": {
        "accepted": "🎬 Сделаю видео с объяснением. Это займёт несколько минут…",
        "context": "Объясни, что здесь обсуждают, с учётом приложенных материалов.",
        "attachment": "Объясни содержание приложенного материала.",
        "followup": "🎬 Сделаю ещё одно видео с ответом на уточнение…",
        "text": "💬 Отвечу текстом…",
        "transcript": "📝 Сделаю текстовую расшифровку речи…",
        "summary": "📝 Расшифрую речь и подготовлю краткое резюме…",
        "research": "🔎 Начинаю глубокое исследование. Соберу хронологию и версии сторон, затем сделаю видео с озвучкой…",
        "research_text": "🔎 Начинаю глубокое исследование и подготовлю текстовый разбор по твоей просьбе…",
        "enhance": "🪄 Улучшу изображение. Это займёт пару минут…",
        "followup_question": "Объясни предыдущий ответ подробнее и проще.",
        "followup_unavailable": "Не удалось восстановить материал предыдущего ролика. Пришли исходную ссылку или файл вместе с вопросом и упоминанием бота.",
        "unavailable": "Не могу прочитать цитируемое сообщение. Оно удалено или у меня нет доступа. Скопируй текст сюда либо дай мне доступ к истории сообщений.",
        "failed": "⚠️ Не получилось принять запрос. Попробуй ещё раз.",
    },
    "en": {
        "accepted": "🎬 I'll make an explainer video. This will take a few minutes…",
        "context": "Explain the discussion here, taking the attached material into account.",
        "attachment": "Explain the content of the attached material.",
        "followup": "🎬 I'll make another video answering your follow-up…",
        "text": "💬 I'll answer in text…",
        "transcript": "📝 I'll transcribe the speech into text…",
        "summary": "📝 I'll transcribe the speech and prepare a short summary…",
        "research": "🔎 Starting deep research. I'll turn the chronology and competing accounts into a narrated video…",
        "research_text": "🔎 Starting deep research and preparing the text report you requested…",
        "enhance": "🪄 I'll enhance the image. This takes a couple of minutes…",
        "followup_question": "Explain the previous answer more clearly and in more detail.",
        "followup_unavailable": "I couldn't recover the previous video's source. Send the original link or file with your question and mention the bot.",
        "unavailable": "I can't read the quoted message. It was deleted or I don't have access. Copy its text here or give me Read Message History permission.",
        "failed": "⚠️ Could not accept this request. Please try again.",
    },
}


def mention_strings():
    return MENTION_STRINGS.get(os.environ.get("BOT_LANG") or "ru", MENTION_STRINGS["en"])


def _list(value):
    return value if isinstance(value, (list, tuple)) else []


def message_payload(message, depth=0):
    """Snapshot only the text/media needed by the renderer, including one cached reply."""
    author = getattr(message, "author", None)
    ref = getattr(message, "reference", None)
    resolved = ref.resolved if ref else None
    created = getattr(message, "created_at", None)
    return I.trim_message({
        "id": str(message.id) if hasattr(message, "id") else None,
        "content": message.content,
        "timestamp": created.isoformat() if created else None,
        "author": {"id": str(author.id) if getattr(author, "id", None) is not None else None,
                   "username": author.name if author else "forwarded post", "global_name": getattr(author, "global_name", None),
                   "display_name": getattr(author, "display_name", None)},
        "attachments": [a.to_dict() for a in message.attachments],
        "embeds": [e.to_dict() for e in message.embeds],
        "sticker_items": [{"id": str(st.id), "name": st.name, "format_type": st.format.value}
                          for st in _list(getattr(message, "stickers", None))],
        "message_reference": ref.to_dict() if ref else None,
        "referenced_message": message_payload(resolved, 1)
        if depth == 0 and isinstance(resolved, discord.Message) else None,
    })


class QuotedMessageUnavailable(Exception):
    pass


class FollowupUnavailable(Exception):
    pass


async def _referenced(message):
    ref = message.reference
    if not ref or ref.type != discord.MessageReferenceType.default or not ref.message_id:
        return None
    if ref.channel_id and ref.channel_id != message.channel.id:
        raise QuotedMessageUnavailable()
    if isinstance(ref.resolved, discord.Message):
        return ref.resolved
    try:
        return await message.channel.fetch_message(ref.message_id)
    except (discord.NotFound, discord.Forbidden) as exc:
        raise QuotedMessageUnavailable() from exc


async def mention_job(message, bot_id, depth=0):
    text = re.sub(rf"<@!?{bot_id}>", "", message.content).strip()[:I.MAX_TEXT]
    text, mode = mention_mode(text)
    own = message_payload(message)
    own["content"] = text
    job = {"channel_id": str(message.channel.id), "context_before_id": str(message.id),
           "request_user_id": str(message.author.id), "request_origin": "mention",
           "context_messages": max(0, min(50, int(os.environ.get("MENTION_CONTEXT_MESSAGES") or 10))),
           "bot_user_id": str(bot_id), "locale": os.environ.get("BOT_LANG") or "ru", "render_mode": mode,
           "response_format": response_format(text), "text_task": text_task(text)}
    target = None
    ref = message.reference
    if ref and ref.type == discord.MessageReferenceType.default and ref.message_id:
        try:
            target = await _referenced(message)
        except QuotedMessageUnavailable:
            if not own.get("attachments"):
                raise
    elif message.message_snapshots:
        target = message

    if target is not None and str(getattr(target.author, "id", "")) == str(bot_id):
        memory = await asyncio.to_thread(I.state().get_explanation, str(message.channel.id), str(target.id))
        if memory is None:
            if depth >= 3:
                raise FollowupUnavailable()
            try:
                original = await _referenced(target)
                if original is None or original.id == message.id:
                    raise FollowupUnavailable()
                source_job = await mention_job(original, bot_id, depth + 1)
            except QuotedMessageUnavailable as exc:
                raise FollowupUnavailable() from exc
            prior = source_job.get("followup") or {}
            history = list(prior.get("history") or []) + [{"question": source_job.get("question") or source_job.get("text") or "",
                "title": target.content.splitlines()[0][:300] if target.content else "Earlier video",
                "narration": "", "script_available": False}]
            memory = {"root_id": prior.get("root_id") or str(target.id), "source_job": source_job, "history": history[-8:]}
        return {**job, "kind": "query", "text": text or mention_strings()["followup_question"], "message": own,
                "context_messages": 0, "followup": memory, "followup_message_id": str(target.id)}

    if own.get("attachments"):
        return {**job, "kind": "query", "text": text or mention_strings()["attachment"], "message": own}

    if target is not None:
        if target.message_snapshots:
            post = message_payload(target.message_snapshots[0])
            post["id"] = str(target.id)  # Context belongs to the visible forwarded copy.
        else:
            post = message_payload(target)
        if not I._has_content(post):
            raise QuotedMessageUnavailable()
        return {**job, "kind": "message", "text": "", "message": post, "question": text, "request_message": own}
    return {**job, "kind": "query", "text": text or mention_strings()["context"], "message": own}


def bot_request(token, path, payload=None):
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bot " + token, "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (explain-bot, 1.0)"},
        method="PUT" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def register_commands(token, application_id):
    commands = json.loads(Path(__file__).with_name("commands.json").read_text())
    for command in commands:
        command["integration_types"] = [0]  # Bot installed in a server.
        command["contexts"] = [0, 1]  # Server or DM with the bot.
    bot_request(token, f"/applications/{application_id}/commands", commands)
    log.info("registered %d commands", len(commands))


def payload(interaction):
    """Keep resolved message data intact for the existing command handler."""
    return {
        "id": str(interaction.id), "application_id": str(interaction.application_id),
        "token": interaction.token, "type": interaction.type.value,
        "locale": interaction.locale.value,
        "channel_id": str(interaction.channel_id) if interaction.channel_id else None,
        "user": {"id": str(interaction.user.id)}, "data": interaction.data or {},
        "message": {"id": str(interaction.message.id)} if getattr(interaction, "message", None) is not None else None,
    }


class QuestionModal(discord.ui.Modal):
    def __init__(self, data):
        super().__init__(title=data["title"], custom_id=data["custom_id"], timeout=I.FORM_TTL)
        for row in data["components"]:
            for field in row["components"]:
                self.add_item(discord.ui.TextInput(
                    custom_id=field["custom_id"], label=field["label"],
                    style=discord.TextStyle(field["style"]),
                    placeholder=field.get("placeholder"), required=field.get("required", True),
                    default=field.get("value"),
                    max_length=field.get("max_length"),
                ))

    # All submissions go through on_interaction, including forms opened before a restart.
    async def on_submit(self, interaction):
        pass


def component_view(components):
    if not components:
        return None
    view = discord.ui.View(timeout=I.FORM_TTL)
    for row, group in enumerate(components):
        for button in group.get("components") or []:
            view.add_item(discord.ui.Button(label=button["label"], custom_id=button["custom_id"],
                style=discord.ButtonStyle(button.get("style", 2)), row=row, disabled=button.get("disabled", False)))
    return view


async def respond(interaction, response):
    data = response.get("data", {})
    if response["type"] == I.DEFERRED:
        await interaction.response.defer(thinking=True, ephemeral=bool(data.get("flags", 0) & I.EPHEMERAL))
    elif response["type"] == I.CHANNEL_MESSAGE:
        await interaction.response.send_message(
            data["content"], ephemeral=bool(data.get("flags", 0) & I.EPHEMERAL),
            allowed_mentions=discord.AllowedMentions.none(),
            **({"view": component_view(data["components"])} if data.get("components") else {}),
        )
    elif response["type"] == I.UPDATE_MESSAGE:
        await interaction.response.edit_message(content=data["content"], view=component_view(data.get("components")),
                                               allowed_mentions=discord.AllowedMentions.none())
    elif response["type"] == I.MODAL:
        await interaction.response.send_modal(QuestionModal(data))
    else:
        raise ValueError("unsupported interaction response")


class ExplainBot(discord.Client):
    def __init__(self, token):
        mentions = os.environ.get("ENABLE_MENTIONS", "1") == "1"
        intents = discord.Intents(guilds=True, messages=mentions, message_content=mentions)
        super().__init__(intents=intents, max_messages=None)
        self.bot_token = token
        self.seen_mentions = OrderedDict()
        # One active render plus a bounded waiting queue. Reserve before acknowledging,
        # but do not start work until Discord has accepted the deferred response.
        self.slots = threading.BoundedSemaphore(max(0, int(os.environ.get("MAX_QUEUED_JOBS") or 4)) + 1)
        self.renderer = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
        self.text_responder = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="text")
        # Graceful restart: after SIGTERM no new job starts; running jobs finish, queued ones stay in the journal.
        self.jobs_lock = threading.Lock()
        self.running, self.waiting = 0, {}  # waiting: id(job) -> (job, journal key) until a worker starts it
        self.draining, self.drain_task, self.drain_deadline = False, None, 0.0

    async def setup_hook(self):
        app = await asyncio.to_thread(bot_request, self.bot_token, "/applications/@me")
        if app.get("interactions_endpoint_url"):
            raise RuntimeError("Clear Interactions Endpoint URL in Discord Developer Portal before using Gateway mode")
        if os.environ.get("REGISTER_COMMANDS", "1") == "1":
            await asyncio.to_thread(register_commands, self.bot_token, app["id"])
        # Once per process (on_ready repeats after reconnects); jobs reply over REST, so the Gateway can wait.
        await asyncio.to_thread(self.resume)

    async def on_ready(self):
        log.info("connected to Discord as %s (%s)", self.user, self.user.id)
        log.info("mention commands enabled=%s", self.intents.messages)

    def resume(self):
        try:
            J.resume(lambda: self.slots.acquire(blocking=False), self.submit)
        except Exception:
            log.exception("could not resume journaled jobs")
        self.publish()

    def publish(self):
        J.get().activity(self.running, len(self.waiting), self.draining)

    def render(self, job, key=None):
        with self.jobs_lock:
            queued = self.waiting.pop(id(job), None) is not None
            started = not self.draining
            self.running += started
        if not started:
            if queued:  # Accepted after the drain emptied the queue.
                self.drop(job, key)
            return
        try:
            self.publish()
            J.get().start(key)
            from pipeline import handle_job
            handle_job(job)
        finally:
            J.get().finish(key)
            with self.jobs_lock:
                self.running -= 1
            self.publish()
            self.slots.release()

    def submit(self, job, key=None):
        """Queue a job. Channel-reply jobs are journaled first so that a restart can resume them."""
        key = key or J.get().record(job)
        worker = self.text_responder if job.get("response_format") == "text" else self.renderer
        with self.jobs_lock:
            self.waiting[id(job)] = (job, key)
        try:
            worker.submit(self.render, job, key)
        except Exception:
            with self.jobs_lock:
                self.waiting.pop(id(job), None)
            J.get().finish(key)
            raise
        self.publish()

    def drop(self, job, key):
        """A queued job that will not run in this process. A journaled one resumes after the restart."""
        if key is None:  # Interaction-token jobs cannot outlive the process.
            J.abandon(job, J.strings()["restarting"])
        self.slots.release()

    def cancel_waiting(self):
        with self.jobs_lock:
            dropped, self.waiting = list(self.waiting.values()), {}
        for job, key in dropped:
            self.drop(job, key)
        self.publish()

    def request_stop(self):
        """Signal handler: the first signal drains, a second one stops waiting for running jobs."""
        if self.drain_task:
            self.drain_deadline = 0.0
        else:
            self.drain_task = asyncio.get_running_loop().create_task(self.drain())

    async def drain(self):
        with self.jobs_lock:
            self.draining = True
        self.drain_deadline = time.monotonic() + float(os.environ.get("DRAIN_TIMEOUT") or 900)
        log.info("draining before shutdown: %d running, %d waiting", self.running, len(self.waiting))
        await asyncio.to_thread(self.cancel_waiting)
        while self.running and time.monotonic() < self.drain_deadline:
            await asyncio.sleep(0.2)
        await asyncio.to_thread(self.cancel_waiting)  # Accepted while the drain was waiting.
        if self.running:
            log.warning("drain timed out with %d running; journaled jobs resume after restart", self.running)
        await self.close()

    async def on_message(self, message):
        if (not self.intents.messages or not self.user or message.author.bot or message.webhook_id
                or not re.search(rf"<@!?{self.user.id}>", message.content)):
            return
        if message.id in self.seen_mentions:
            return
        self.seen_mentions[message.id] = None
        if len(self.seen_mentions) > 2048:
            self.seen_mentions.popitem(last=False)

        async def reply(text):
            return await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

        from bot_help import help_text, mention_section
        own_text = re.sub(rf"<@!?{self.user.id}>", "", message.content).strip()
        section = mention_section(own_text)
        if section is not None:
            await reply(help_text(section, os.environ.get("BOT_LANG") or "ru", self.user.id))
            return

        import clarifications as C
        has_source = bool(message.reference or message.attachments or message.embeds or re.search(r"https?://", own_text))
        why = C.reason(own_text, has_source)
        if why:
            key = None
            try:
                job = await mention_job(message, self.user.id)
                key, record = await asyncio.to_thread(C.create, I.state(), job, str(message.author.id),
                                                     str(message.channel.id), own_text, why)
                data = C.card(key, record)
                status = await message.reply(data["content"], view=component_view(data["components"]),
                                             mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                await asyncio.to_thread(C.bind, I.state(), key, str(status.id))
            except QuotedMessageUnavailable:
                await reply(mention_strings()["unavailable"])
            except FollowupUnavailable:
                await reply(mention_strings()["followup_unavailable"])
            except Exception:
                if key:
                    await asyncio.to_thread(C.discard, I.state(), key)
                log.exception("could not prepare clarification for %s", message.id)
                await reply(mention_strings()["failed"])
            return

        if self.draining:
            await reply(J.strings()["restarting"])
            return
        if not self.slots.acquire(blocking=False):
            await reply(I._S()["busy"])
            return
        submitted, status = False, None
        try:
            job = await mention_job(message, self.user.id)
            limit = int(os.environ.get("DAILY_LIMIT") or 0)
            if limit > 0 and not await asyncio.to_thread(I.state().take_quota, str(message.author.id), limit):
                await reply(I._S()["limit"].format(limit))
                return
            job["quota_paid"] = limit > 0
            accepted = job["text_task"] if job.get("text_task") in ("transcript", "summary", "research", "enhance") else "text" if job.get("response_format") == "text" else "followup" if job.get("followup") else "accepted"
            if accepted == "research" and job.get("response_format") == "text":
                accepted = "research_text"
            status = await reply(mention_strings()[accepted])
            job.update(response_channel_id=str(message.channel.id), response_message_id=str(status.id))
            await asyncio.to_thread(self.submit, job)
            submitted = True
        except QuotedMessageUnavailable:
            await reply(mention_strings()["unavailable"])
        except FollowupUnavailable:
            await reply(mention_strings()["followup_unavailable"])
        except Exception:
            log.exception("mention request %s failed", message.id)
            try:
                if status:
                    await status.edit(content=mention_strings()["failed"], allowed_mentions=discord.AllowedMentions.none())
                else:
                    await reply(mention_strings()["failed"])
            except discord.HTTPException:
                pass
        finally:
            if not submitted:
                self.slots.release()

    async def on_interaction(self, interaction):
        if interaction.type not in (discord.InteractionType.application_command, discord.InteractionType.modal_submit,
                                    discord.InteractionType.component):
            return
        reserved, refused = [], []

        def reserve(job):
            if self.draining or not self.slots.acquire(blocking=False):
                refused.append(job)
                return False
            reserved.append(job)
            return True

        try:
            # SQLite and optional message lookups must not block Gateway heartbeats.
            code, response = await asyncio.to_thread(I.handle, payload(interaction), reserve)
            if code != 200:
                response = {"type": I.CHANNEL_MESSAGE, "data": {"content": "Unsupported command.", "flags": I.EPHEMERAL}}
            if refused and self.draining:
                response = {"type": I.CHANNEL_MESSAGE, "data": {"content": J.strings()["restarting"], "flags": I.EPHEMERAL}}
            if (interaction.data or {}).get("name") == "objects":
                query = str(I._option(payload(interaction), "query") or "")[:200]
                if query:
                    from object_library import search
                    matches = await asyncio.to_thread(search, query, 2)
                    if len(matches) == 1 and matches[0].get("preview") and Path(matches[0]["preview"]).is_file():
                        file = discord.File(matches[0]["preview"], filename="object-preview.png")
                        try:
                            await interaction.response.send_message(response["data"]["content"], file=file,
                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                        finally:
                            file.close()
                        return
            await respond(interaction, response)
            for job in reserved[:]:
                await asyncio.to_thread(self.submit, job)
                reserved.remove(job)
        except Exception:
            # Avoid logging payloads, which contain interaction tokens and user content.
            log.exception("interaction %s failed", interaction.id)
        finally:
            for job in reserved:
                if job.get("_clarification_id"):
                    from clarifications import rollback, pending_card
                    await asyncio.to_thread(rollback, I.state(), job)
                    if getattr(interaction.response, "is_done", lambda: False)():
                        data = await asyncio.to_thread(pending_card, I.state(), job["_clarification_id"])
                        if data:
                            try:
                                await interaction.edit_original_response(content=data["content"], view=component_view(data["components"]),
                                                                         allowed_mentions=discord.AllowedMentions.none())
                            except Exception:
                                log.warning("could not restore clarification controls after submission failure")
                self.slots.release()

    async def close(self):
        await super().close()
        self.renderer.shutdown(wait=False, cancel_futures=True)
        self.text_responder.shutdown(wait=False, cancel_futures=True)


def main():
    required = ("DISCORD_BOT_TOKEN", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")
    import speech_backend
    if speech_backend.configured():
        required = ("DISCORD_BOT_TOKEN", "ANTHROPIC_API_KEY")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit("Missing required settings: " + ", ".join(missing))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ["DISCORD_BOT_TOKEN"].strip()
    bot = ExplainBot(token)
    asyncio.run(serve(bot, token))
    if bot.running:  # Drain timed out: render threads would block exit; the journal resumes their jobs.
        logging.shutdown()
        os._exit(0)


async def serve(bot, token):
    # Client.run() installs no SIGTERM handler, and as PID 1 in a container an unhandled SIGTERM is ignored
    # until Docker sends SIGKILL. Handle it on the loop so that a stop drains instead.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bot.request_stop)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    main()
