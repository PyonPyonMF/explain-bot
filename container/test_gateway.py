"""Gateway command flow tests; no network, tokens, or paid API calls."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import gateway
import interactions as I


def interaction(data, kind=discord.InteractionType.application_command):
    return SimpleNamespace(
        id=123, application_id=456, token="test-token", type=kind,
        locale=discord.Locale.russian, channel_id=789, user=SimpleNamespace(id=42), data=data,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
    )


def message(content="<@999> explain this", mid=300, reference=None):
    msg = Mock(spec=discord.Message)
    msg.id, msg.content, msg.reference = mid, content, reference
    msg.created_at = datetime(2026, 10, 9, tzinfo=timezone.utc)
    msg.author = SimpleNamespace(id=42, name="Reader", global_name="Reader", bot=False)
    msg.channel = SimpleNamespace(id=789, fetch_message=AsyncMock())
    msg.attachments, msg.embeds, msg.message_snapshots = [], [], []
    msg.webhook_id = None
    msg.reply = AsyncMock(return_value=SimpleNamespace(id=400, edit=AsyncMock()))
    return msg


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"DAILY_LIMIT": "5", "BOT_LANG": "ru", "MAX_QUEUED_JOBS": "1"})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = I._state
        I._state = I.State(str(Path(self.tmp.name) / "bot.sqlite"))
        self.bot = gateway.ExplainBot("test-token")
        self.bot._connection.user = SimpleNamespace(id=999)
        self.submit = Mock()
        self.bot.renderer.submit = self.submit

    async def asyncTearDown(self):
        await self.bot.close()
        I._state.db.close()
        I._state = self.old_state
        self.tmp.cleanup()
        self.env.stop()

    async def test_private_query_acknowledged_before_render(self):
        i = interaction({"type": 1, "name": "explain", "options": [
            {"name": "query", "value": "gradient"}, {"name": "private", "value": True},
            {"name": "messages", "value": 20},
        ]})
        self.submit.side_effect = lambda *args: i.response.defer.assert_awaited_once()
        await self.bot.on_interaction(i)
        i.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        job = self.submit.call_args.args[1]
        self.assertEqual((job["text"], job["context_messages"], job["token"]), ("gradient", 20, "test-token"))
        self.assertEqual(job["application_id"], "456")

    async def test_message_command_keeps_images_and_context(self):
        message = {"id": "91", "content": "Explain this", "attachments": [{"url": "https://example.com/image.png"}]}
        i = interaction({"type": 3, "name": "Explain (video)", "target_id": "91", "resolved": {"messages": {"91": message}}})
        await self.bot.on_interaction(i)
        job = self.submit.call_args.args[1]
        self.assertEqual(job["message"]["content"], "Explain this")
        self.assertEqual(job["message"]["attachments"][0]["url"], "https://example.com/image.png")
        self.assertEqual(job["channel_id"], "789")

    async def test_question_modal_and_submission(self):
        i = interaction({"type": 3, "name": I.ASK_COMMAND, "target_id": "91", "resolved": {
            "messages": {"91": {"id": "91", "content": "some claim"}},
        }})
        await self.bot.on_interaction(i)
        self.submit.assert_not_called()
        modal = i.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, discord.ui.Modal)
        self.assertEqual(modal.to_dict()["components"][0]["components"][0]["custom_id"], "q")
        answer = interaction({"custom_id": modal.custom_id, "components": [
            {"type": 1, "components": [{"custom_id": "q", "value": "why?"}]},
        ]}, discord.InteractionType.modal_submit)
        await self.bot.on_interaction(answer)
        answer.response.defer.assert_awaited_once()
        self.assertEqual(self.submit.call_args.args[1]["question"], "why?")
        self.assertEqual(self.submit.call_args.args[1]["message"]["content"], "some claim")
        modal.stop()

    async def test_full_queue_replies_busy_without_rendering(self):
        for _ in range(2):
            self.assertTrue(self.bot.slots.acquire(blocking=False))
        i = interaction({"type": 1, "name": "explain", "options": [{"name": "query", "value": "x"}]})
        await self.bot.on_interaction(i)
        i.response.send_message.assert_awaited_once()
        self.assertIn("занят", i.response.send_message.call_args.args[0])
        i.response.defer.assert_not_awaited()
        self.submit.assert_not_called()

    async def test_failed_ack_releases_reservation_without_rendering(self):
        i = interaction({"type": 1, "name": "explain", "options": [{"name": "query", "value": "x"}]})
        i.response.defer.side_effect = RuntimeError("callback failed")
        with self.assertLogs("gateway", level="ERROR"):
            await self.bot.on_interaction(i)
        self.submit.assert_not_called()
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        self.assertFalse(self.bot.slots.acquire(blocking=False))

    async def test_render_failure_releases_capacity(self):
        self.bot.slots.acquire()
        with patch.dict("sys.modules", {"pipeline": SimpleNamespace(handle_job=Mock(side_effect=RuntimeError("render failed")))}):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                self.bot.render({})
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        self.assertTrue(self.bot.slots.acquire(blocking=False))

    async def test_existing_http_endpoint_blocks_gateway_startup(self):
        with patch.object(gateway, "bot_request", return_value={"id": "456", "interactions_endpoint_url": "https://old.example"}):
            with self.assertRaisesRegex(RuntimeError, "Clear Interactions Endpoint URL"):
                await self.bot.setup_hook()

    async def test_startup_registers_commands_once(self):
        with patch.object(gateway, "bot_request", return_value={"id": "456"}), patch.object(gateway, "register_commands") as register:
            await self.bot.setup_hook()
            register.assert_called_once_with("test-token", "456")

    async def test_mention_acknowledged_before_render_with_channel_delivery(self):
        msg = message("<@!999> explain gradients")
        self.submit.side_effect = lambda *args: msg.reply.assert_awaited_once()
        await self.bot.on_message(msg)
        job = self.submit.call_args.args[1]
        self.assertEqual(job["text"], "explain gradients")
        self.assertEqual(job["kind"], "query")
        self.assertEqual(job["context_before_id"], "300")
        self.assertEqual(job["response_message_id"], "400")
        self.assertEqual(job["response_channel_id"], "789")
        self.assertNotIn("token", job)
        self.assertFalse(msg.reply.call_args.kwargs["mention_author"])
        self.assertEqual(msg.reply.call_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertTrue(self.bot.intents.guild_messages and self.bot.intents.message_content)

    async def test_mention_reply_uses_quoted_post_and_question(self):
        target = message("A claim about gradients", mid=200)
        target.author = SimpleNamespace(name="Original author", global_name=None)
        target.attachments = [SimpleNamespace(to_dict=lambda: {"url": "https://example.com/a.png", "content_type": "image/png"})]
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = target
        msg = message("<@999> кто тут прав?", reference=ref)
        await self.bot.on_message(msg)
        job = self.submit.call_args.args[1]
        self.assertEqual((job["kind"], job["question"]), ("message", "кто тут прав?"))
        self.assertEqual(job["message"]["content"], target.content)
        self.assertEqual(job["message"]["author"]["username"], "Original author")
        self.assertEqual(job["message"]["attachments"][0]["content_type"], "image/png")
        self.assertEqual((job["message"]["id"], job["context_before_id"]), ("200", "300"))
        msg.channel.fetch_message.assert_not_awaited()

    async def test_uncached_reply_fetched(self):
        msg = message(reference=discord.MessageReference(message_id=200, channel_id=789))
        msg.channel.fetch_message.return_value = message("quoted text", mid=200)
        await self.bot.on_message(msg)
        msg.channel.fetch_message.assert_awaited_once_with(200)
        self.assertEqual(self.submit.call_args.args[1]["message"]["content"], "quoted text")

    async def test_deleted_reply_does_not_generate_wrong_video(self):
        msg = message(reference=discord.MessageReference(message_id=200, channel_id=789))
        msg.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
        await self.bot.on_message(msg)
        self.submit.assert_not_called()
        self.assertIn("цитируемое", msg.reply.call_args.args[0])
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        self.assertTrue(self.bot.slots.acquire(blocking=False))

    async def test_forwarded_snapshot_without_accessing_source_channel(self):
        msg = message("<@999> что это значит?", reference=discord.MessageReference(
            message_id=123, channel_id=456, type=discord.MessageReferenceType.forward))
        msg.message_snapshots = [SimpleNamespace(content="Forwarded claim", created_at=msg.created_at, attachments=[], embeds=[])]
        await self.bot.on_message(msg)
        job = self.submit.call_args.args[1]
        self.assertEqual(job["message"]["content"], "Forwarded claim")
        self.assertEqual(job["message"]["id"], "300")
        self.assertEqual(job["channel_id"], "789")
        msg.channel.fetch_message.assert_not_awaited()

    async def test_plain_mention_explains_recent_conversation(self):
        msg = message("<@999>")
        await self.bot.on_message(msg)
        job = self.submit.call_args.args[1]
        self.assertIn("обсуждают", job["text"])
        self.assertEqual(job["context_messages"], 10)

    async def test_3d_mention_keeps_reply_context(self):
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = message("A pendulum", mid=200)
        msg = message("<@999> 3d объясни почему качается", reference=ref)
        await self.bot.on_message(msg)
        job = self.submit.call_args.args[1]
        self.assertEqual(job["render_mode"], "3d")
        self.assertEqual(job["question"], "объясни почему качается")
        self.assertEqual(job["message"]["content"], "A pendulum")

    async def test_slash_3d_mode_is_explicit_and_preserves_private_flag(self):
        i = interaction({"type":1, "options":[{"name":"query","value":"маятник"},{"name":"mode","value":"3d"},{"name":"private","value":True}]})
        await self.bot.on_interaction(i)
        self.assertEqual(self.submit.call_args.args[1]["render_mode"], "3d")
        i.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)

    async def test_ignore_bots_and_non_mentions(self):
        messages = [message("hello"), message("@everyone"), message("<@123>"), message()]
        messages[-1].author.bot = True
        for msg in messages:
            await self.bot.on_message(msg)
            msg.reply.assert_not_awaited()
        self.submit.assert_not_called()

    async def test_duplicate_message_event_only_enqueues_once(self):
        msg = message()
        await self.bot.on_message(msg)
        await self.bot.on_message(msg)
        self.submit.assert_called_once()
        msg.reply.assert_awaited_once()

    async def test_mentions_share_quota_with_slash_commands(self):
        with patch.dict(os.environ, {"DAILY_LIMIT": "1"}):
            i = interaction({"type": 1, "options": [{"name": "query", "value": "x"}]})
            await self.bot.on_interaction(i)
            msg = message()
            await self.bot.on_message(msg)
        self.submit.assert_called_once()
        self.assertIn("Лимит", msg.reply.call_args.args[0])

    async def test_full_mention_queue_does_not_consume_quota(self):
        for _ in range(2):
            self.bot.slots.acquire()
        msg = message()
        await self.bot.on_message(msg)
        self.assertIn("занят", msg.reply.call_args.args[0])
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM quota").fetchone()[0], 0)
        self.submit.assert_not_called()

    async def test_failed_mention_ack_releases_capacity(self):
        msg = message()
        msg.reply.side_effect = [RuntimeError("send failed"), SimpleNamespace(id=400)]
        with self.assertLogs("gateway", level="ERROR"):
            await self.bot.on_message(msg)
        self.submit.assert_not_called()
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        self.assertTrue(self.bot.slots.acquire(blocking=False))


class ConfigurationTests(unittest.TestCase):
    def test_registration_uses_server_bot_commands(self):
        with patch.object(gateway, "bot_request") as request:
            gateway.register_commands("test-token", "456")
        commands = request.call_args.args[2]
        self.assertEqual({c["name"] for c in commands}, {"explain", "Explain (video)", I.ASK_COMMAND})
        self.assertTrue(all(c["integration_types"] == [0] and c["contexts"] == [0, 1] for c in commands))
        original = json.loads(Path(gateway.__file__).with_name("commands.json").read_text())
        self.assertEqual(original[0]["integration_types"], [0, 1])

    def test_missing_keys_fail_before_connecting(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(gateway, "ExplainBot") as bot:
            with self.assertRaisesRegex(SystemExit, "DISCORD_BOT_TOKEN, ANTHROPIC_API_KEY, ELEVENLABS_API_KEY"):
                gateway.main()
            bot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
