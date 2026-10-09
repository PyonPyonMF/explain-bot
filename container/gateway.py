"""Discord Gateway entry point: outbound connections only, no HTTP listener."""
import asyncio
from collections import OrderedDict
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import re
import threading
import urllib.request

import discord

import interactions as I

log = logging.getLogger("gateway")

MENTION_STRINGS = {
    "ru": {
        "accepted": "🎬 Сделаю видео с объяснением. Это займёт несколько минут…",
        "context": "Объясни, что здесь обсуждают, с учётом приложенных материалов.",
        "unavailable": "Не могу прочитать цитируемое сообщение. Оно удалено или у меня нет доступа. Скопируй текст сюда либо дай мне доступ к истории сообщений.",
        "failed": "⚠️ Не получилось принять запрос. Попробуй ещё раз.",
    },
    "en": {
        "accepted": "🎬 I'll make an explainer video. This will take a few minutes…",
        "context": "Explain the discussion here, taking the attached material into account.",
        "unavailable": "I can't read the quoted message. It was deleted or I don't have access. Copy its text here or give me Read Message History permission.",
        "failed": "⚠️ Could not accept this request. Please try again.",
    },
}


def mention_strings():
    return MENTION_STRINGS.get(os.environ.get("BOT_LANG") or "ru", MENTION_STRINGS["en"])


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
        "author": {"username": author.name if author else "forwarded post",
                   "global_name": getattr(author, "global_name", None)},
        "attachments": [a.to_dict() for a in message.attachments],
        "embeds": [e.to_dict() for e in message.embeds],
        "message_reference": ref.to_dict() if ref else None,
        "referenced_message": message_payload(resolved, 1)
        if depth == 0 and isinstance(resolved, discord.Message) else None,
    })


class QuotedMessageUnavailable(Exception):
    pass


async def mention_job(message, bot_id):
    text = re.sub(rf"<@!?{bot_id}>", "", message.content).strip()[:I.MAX_TEXT]
    own = message_payload(message)
    own["content"] = text
    job = {"channel_id": str(message.channel.id), "context_before_id": str(message.id),
           "context_messages": max(0, min(50, int(os.environ.get("MENTION_CONTEXT_MESSAGES") or 10))),
           "bot_user_id": str(bot_id), "locale": os.environ.get("BOT_LANG") or "ru"}
    target = None
    ref = message.reference
    if ref and ref.type == discord.MessageReferenceType.default and ref.message_id:
        # Never use a bot's wider permissions to copy a different channel's history here.
        if ref.channel_id and ref.channel_id != message.channel.id:
            raise QuotedMessageUnavailable()
        target = ref.resolved
        if not isinstance(target, discord.Message):
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise QuotedMessageUnavailable() from exc
    elif message.message_snapshots:
        target = message

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
                    max_length=field.get("max_length"),
                ))

    # All submissions go through on_interaction, including forms opened before a restart.
    async def on_submit(self, interaction):
        pass


async def respond(interaction, response):
    data = response.get("data", {})
    if response["type"] == I.DEFERRED:
        await interaction.response.defer(thinking=True, ephemeral=bool(data.get("flags", 0) & I.EPHEMERAL))
    elif response["type"] == I.CHANNEL_MESSAGE:
        await interaction.response.send_message(
            data["content"], ephemeral=bool(data.get("flags", 0) & I.EPHEMERAL),
            allowed_mentions=discord.AllowedMentions.none(),
        )
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

    async def setup_hook(self):
        app = await asyncio.to_thread(bot_request, self.bot_token, "/applications/@me")
        if app.get("interactions_endpoint_url"):
            raise RuntimeError("Clear Interactions Endpoint URL in Discord Developer Portal before using Gateway mode")
        if os.environ.get("REGISTER_COMMANDS", "1") == "1":
            await asyncio.to_thread(register_commands, self.bot_token, app["id"])

    async def on_ready(self):
        log.info("connected to Discord as %s (%s)", self.user, self.user.id)
        log.info("mention commands enabled=%s", self.intents.messages)

    def render(self, job):
        try:
            from pipeline import handle_job
            handle_job(job)
        finally:
            self.slots.release()

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
            status = await reply(mention_strings()["accepted"])
            job.update(response_channel_id=str(message.channel.id), response_message_id=str(status.id))
            self.renderer.submit(self.render, job)
            submitted = True
        except QuotedMessageUnavailable:
            await reply(mention_strings()["unavailable"])
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
        if interaction.type not in (discord.InteractionType.application_command, discord.InteractionType.modal_submit):
            return
        reserved = []

        def reserve(job):
            if not self.slots.acquire(blocking=False):
                return False
            reserved.append(job)
            return True

        try:
            # SQLite and optional message lookups must not block Gateway heartbeats.
            code, response = await asyncio.to_thread(I.handle, payload(interaction), reserve)
            if code != 200:
                response = {"type": I.CHANNEL_MESSAGE, "data": {"content": "Unsupported command.", "flags": I.EPHEMERAL}}
            await respond(interaction, response)
            for job in reserved[:]:
                self.renderer.submit(self.render, job)
                reserved.remove(job)
        except Exception:
            # Avoid logging payloads, which contain interaction tokens and user content.
            log.exception("interaction %s failed", interaction.id)
        finally:
            for _ in reserved:
                self.slots.release()

    async def close(self):
        await super().close()
        self.renderer.shutdown(wait=False, cancel_futures=True)


def main():
    required = ("DISCORD_BOT_TOKEN", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit("Missing required settings: " + ", ".join(missing))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ExplainBot(os.environ["DISCORD_BOT_TOKEN"].strip()).run(os.environ["DISCORD_BOT_TOKEN"].strip(), log_handler=None)


if __name__ == "__main__":
    main()
