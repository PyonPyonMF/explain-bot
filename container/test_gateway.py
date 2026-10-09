"""Gateway command flow tests; no network, tokens, or paid API calls."""
import json
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


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"DAILY_LIMIT": "5", "BOT_LANG": "ru", "MAX_QUEUED_JOBS": "1"})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = I._state
        I._state = I.State(str(Path(self.tmp.name) / "bot.sqlite"))
        self.bot = gateway.ExplainBot("test-token")
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
