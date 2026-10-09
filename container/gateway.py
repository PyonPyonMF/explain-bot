"""Discord Gateway entry point: outbound connections only, no HTTP listener."""
import asyncio
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import threading
import urllib.request

import discord

import interactions as I

log = logging.getLogger("gateway")


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
        super().__init__(intents=discord.Intents.none(), max_messages=None)
        self.bot_token = token
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

    def render(self, job):
        try:
            from pipeline import handle_job
            handle_job(job)
        finally:
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
