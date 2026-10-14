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
import job_journal as J


def interaction(data, kind=discord.InteractionType.application_command, *, message_id=None, user_id=42):
    return SimpleNamespace(
        id=123, application_id=456, token="test-token", type=kind,
        locale=discord.Locale.russian, channel_id=789, user=SimpleNamespace(id=user_id), data=data,
        message=SimpleNamespace(id=message_id) if message_id else None,
        edit_original_response=AsyncMock(),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: False),
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
    async def test_text_format_after_video_question_uses_text_worker(self):
        target = message("Video", mid=100)
        target.attachments = [SimpleNamespace(to_dict=lambda: {"id": "1", "filename": "clip.mp4",
            "url": "https://cdn.example/clip.mp4", "content_type": "video/mp4", "size": 100})]
        ref = discord.MessageReference(message_id=100, channel_id=789)
        ref.resolved = target
        msg = message("<@999> объясни что тут говорят текстом", reference=ref)
        await self.bot.on_message(msg)
        self.submit.assert_not_called()
        self.text_submit.assert_called_once()
        job = self.text_submit.call_args.args[1]
        self.assertEqual(job["response_format"], "text")
        self.assertEqual(job["message"]["attachments"][0]["filename"], "clip.mp4")
        self.assertEqual(msg.reply.call_args.args[0], gateway.mention_strings()["text"])

    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"DAILY_LIMIT": "5", "BOT_LANG": "ru", "MAX_QUEUED_JOBS": "1"})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = I._state
        I._state = I.State(str(Path(self.tmp.name) / "bot.sqlite"))
        self.old_journal, J._journal = J._journal, J.Journal(str(Path(self.tmp.name) / "jobs.sqlite"))
        self.bot = gateway.ExplainBot("test-token")
        self.bot._connection.user = SimpleNamespace(id=999)
        self.submit = Mock()
        self.bot.renderer.submit = self.submit
        self.text_submit = Mock()
        self.bot.text_responder.submit = self.text_submit

    async def asyncTearDown(self):
        await self.bot.close()
        I._state.db.close()
        I._state = self.old_state
        J._journal.db.close()
        J._journal = self.old_journal
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

    async def test_plain_mention_offers_a_choice_before_reading_the_discussion(self):
        msg = message("<@999>")
        await self.bot.on_message(msg)
        self.submit.assert_not_called()
        self.text_submit.assert_not_called()
        self.assertIn("Что нужно сделать", msg.reply.call_args.args[0])
        record = json.loads(I.state().db.execute("SELECT payload FROM clarifications").fetchone()[0])
        self.assertIn("обсуждают", record["job"]["text"])
        self.assertEqual(record["job"]["context_messages"], 10)
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM quota").fetchone()[0], 0)

    async def test_ambiguous_video_request_waits_then_starts_only_after_owner_choice_ack(self):
        target = message("Video", mid=100)
        target.attachments = [SimpleNamespace(to_dict=lambda: {"filename": "clip.mp4", "content_type": "video/mp4",
                                                             "url": "https://cdn.example/clip.mp4"})]
        ref = discord.MessageReference(message_id=100, channel_id=789); ref.resolved = target
        msg = message("<@999> разбери это", reference=ref)
        await self.bot.on_message(msg)
        self.submit.assert_not_called(); self.text_submit.assert_not_called()
        view = msg.reply.call_args.kwargs["view"]
        button = next(b for b in view.children if b.custom_id.endswith(":summary"))
        click = interaction({"custom_id": button.custom_id}, discord.InteractionType.component, message_id=400)
        self.text_submit.side_effect = lambda *args: click.response.edit_message.assert_awaited_once()
        await self.bot.on_interaction(click)
        job = self.text_submit.call_args.args[1]
        self.assertEqual((job["response_format"], job["text_task"]), ("text", "summary"))
        self.assertEqual(job["message"]["id"], "100")
        self.assertEqual(job["response_message_id"], "400")
        self.assertIsNone(click.response.edit_message.call_args.kwargs["view"])
        await self.bot.on_interaction(click)
        self.text_submit.assert_called_once()

    async def test_choice_can_be_requested_when_generation_queue_is_full(self):
        self.bot.slots.acquire(); self.bot.slots.acquire()
        msg = message("<@999> разбери это")
        await self.bot.on_message(msg)
        self.assertTrue(msg.reply.call_args.kwargs["view"].children)
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM quota").fetchone()[0], 0)
        self.submit.assert_not_called(); self.text_submit.assert_not_called()

    async def test_choice_ack_failure_returns_reservation_and_quota(self):
        msg = message("<@999> разбери это")
        await self.bot.on_message(msg)
        button = next(b for b in msg.reply.call_args.kwargs["view"].children if b.custom_id.endswith(":text"))
        click = interaction({"custom_id": button.custom_id}, discord.InteractionType.component, message_id=400)
        click.response.edit_message.side_effect = RuntimeError("ack failed")
        with self.assertLogs("gateway", level="ERROR"):
            await self.bot.on_interaction(click)
        self.text_submit.assert_not_called()
        self.assertEqual(I.state().db.execute("SELECT COALESCE(SUM(n),0) FROM quota").fetchone()[0], 0)
        self.assertEqual(I.state().db.execute("SELECT status FROM clarifications").fetchone()[0], "pending")
        self.assertTrue(self.bot.slots.acquire(blocking=False)); self.assertTrue(self.bot.slots.acquire(blocking=False))

    async def test_choice_edit_opens_a_native_modal_and_updates_controls(self):
        msg = message("<@999> разбери это")
        await self.bot.on_message(msg)
        button = next(b for b in msg.reply.call_args.kwargs["view"].children if b.custom_id.endswith(":edit"))
        click = interaction({"custom_id": button.custom_id}, discord.InteractionType.component, message_id=400)
        await self.bot.on_interaction(click)
        modal = click.response.send_modal.call_args.args[0]
        self.assertEqual(modal.to_dict()["components"][0]["components"][0]["value"], "разбери это")
        submit = interaction({"custom_id": modal.custom_id, "components": [{"type": 1, "components": [
            {"custom_id": "q", "value": "Объясни на примере текстом"}]}]}, discord.InteractionType.modal_submit, message_id=400)
        await self.bot.on_interaction(submit)
        self.assertIn("уточнён", submit.response.edit_message.call_args.kwargs["content"])
        self.assertIsNotNone(submit.response.edit_message.call_args.kwargs["view"])
        self.text_submit.assert_not_called(); self.submit.assert_not_called()
        modal.stop()

    async def test_failed_worker_submission_restores_choice_buttons(self):
        msg = message("<@999> разбери это")
        await self.bot.on_message(msg)
        button = next(b for b in msg.reply.call_args.kwargs["view"].children if b.custom_id.endswith(":text"))
        click = interaction({"custom_id": button.custom_id}, discord.InteractionType.component, message_id=400)
        click.response.is_done = lambda: True
        self.text_submit.side_effect = RuntimeError("pool unavailable")
        with self.assertLogs("gateway", level="ERROR"):
            await self.bot.on_interaction(click)
        click.edit_original_response.assert_awaited_once()
        self.assertTrue(click.edit_original_response.call_args.kwargs["view"].children)
        self.assertEqual(I.state().db.execute("SELECT COALESCE(SUM(n),0) FROM quota").fetchone()[0], 0)

    async def test_bare_mention_with_pdf_explains_attachment_not_discussion(self):
        msg = message("<@999>")
        msg.attachments = [SimpleNamespace(to_dict=lambda: {"filename": "article.pdf", "content_type": "application/pdf",
                                                          "url": "https://example.com/article.pdf"})]
        await self.bot.on_message(msg)
        job = self.submit.call_args.args[1]
        self.assertEqual(job["text"], gateway.mention_strings()["attachment"])
        self.assertEqual(job["message"]["attachments"][0]["filename"], "article.pdf")

    async def test_own_attachment_does_not_depend_on_reading_an_unavailable_reply(self):
        msg = message("<@999>", reference=discord.MessageReference(message_id=200, channel_id=789))
        msg.attachments = [SimpleNamespace(to_dict=lambda: {"filename": "article.pdf", "content_type": "application/pdf"})]
        msg.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
        await self.bot.on_message(msg)
        self.assertEqual(self.submit.call_args.args[1]["kind"], "query")

    async def test_reply_to_bot_without_explicit_mention_is_ignored(self):
        target = message("Previous video", mid=200)
        target.author = SimpleNamespace(id=999, name="Bot", global_name=None, bot=True)
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = target
        msg = message("Show a concrete example", reference=ref)
        await self.bot.on_message(msg)
        self.submit.assert_not_called()
        msg.reply.assert_not_awaited()
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM quota").fetchone()[0], 0)

    async def test_text_request_uses_text_worker_and_acknowledges_text(self):
        msg = message("<@999> обьясни текстом, что такое редьюсер")
        self.text_submit.side_effect = lambda *args: msg.reply.assert_awaited_once()
        await self.bot.on_message(msg)
        self.submit.assert_not_called()
        self.assertEqual(self.text_submit.call_args.args[1]["response_format"], "text")
        self.assertIn("текстом", msg.reply.call_args.args[0])
        self.assertNotIn("видео", msg.reply.call_args.args[0])

    async def test_image_request_acknowledges_image_and_preserves_replied_to_source(self):
        target=message('Remove the magnetic track end cap.',mid=100)
        ref=discord.MessageReference(message_id=100,channel_id=789);ref.resolved=target
        msg=message('<@999> сгенерируй картинку',reference=ref)
        await self.bot.on_message(msg)
        job=self.submit.call_args.args[1]
        self.assertEqual(job['response_format'],'image')
        self.assertEqual(job['message']['content'],target.content)
        self.assertIn('Codex ImageGen',msg.reply.call_args.args[0])
        self.text_submit.assert_not_called()

    async def test_transcript_command_keeps_the_video_and_selects_text_worker(self):
        msg = message("<@999> Сделай транскрипцию видео текстом")
        msg.attachments = [SimpleNamespace(to_dict=lambda: {"filename": "clip.mp4", "content_type": "video/mp4",
                                                           "url": "https://cdn.example/clip.mp4"})]
        await self.bot.on_message(msg)
        job = self.text_submit.call_args.args[1]
        self.assertEqual((job["response_format"], job["text_task"]), ("text", "transcript"))
        self.assertEqual(job["message"]["attachments"][0]["filename"], "clip.mp4")
        self.assertEqual(msg.reply.call_args.args[0], gateway.mention_strings()["transcript"])
        self.submit.assert_not_called()

    async def test_summary_command_reuses_the_existing_video_source(self):
        saved = {"source": {"texts": ["Root transcript"]}, "history": [{"narration": "Earlier explanation"}]}
        I.state().put_explanation("789", "200", saved)
        target = message("Video reply", mid=200)
        target.author = SimpleNamespace(id=999, name="Bot", global_name=None, bot=True)
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = target
        msg = message("<@999> summarize видео", reference=ref)
        await self.bot.on_message(msg)
        job = self.text_submit.call_args.args[1]
        self.assertEqual((job["response_format"], job["text_task"]), ("text", "summary"))
        self.assertEqual(job["followup"], saved)
        self.assertEqual(msg.reply.call_args.args[0], gateway.mention_strings()["summary"])
        self.submit.assert_not_called()

    async def test_summary_slash_query_is_a_private_text_task(self):
        i = interaction({"type": 1, "name": "explain", "options": [
            {"name": "query", "value": "summarize видео"}, {"name": "private", "value": True}]})
        await self.bot.on_interaction(i)
        job = self.text_submit.call_args.args[1]
        self.assertEqual((job["response_format"], job["text_task"]), ("text", "summary"))
        self.assertTrue(job["private"])
        self.submit.assert_not_called()

    async def test_text_request_still_requires_a_mention(self):
        msg = message("Объясни текстом, что такое редьюсер")
        await self.bot.on_message(msg)
        self.submit.assert_not_called()
        self.text_submit.assert_not_called()
        msg.reply.assert_not_awaited()

    async def test_help_mention_works_when_busy_without_quota_or_reading_the_reply(self):
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        msg = message("<@999> помощь видео", reference=discord.MessageReference(message_id=100, channel_id=789))
        with patch.object(I, "state", side_effect=AssertionError("Help must not consume quota")):
            await self.bot.on_message(msg)
        msg.reply.assert_awaited_once()
        self.assertIn("Видео, голосовые", msg.reply.call_args.args[0])
        self.assertEqual(msg.reply.call_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        msg.channel.fetch_message.assert_not_awaited()
        self.submit.assert_not_called()
        self.text_submit.assert_not_called()

    async def test_slash_help_is_immediate_private_and_works_when_busy(self):
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        self.assertTrue(self.bot.slots.acquire(blocking=False))
        i = interaction({"type": 1, "name": "help"})
        with patch.object(I, "state", side_effect=AssertionError("Help must not consume quota")):
            await self.bot.on_interaction(i)
        i.response.defer.assert_not_awaited()
        i.response.send_message.assert_awaited_once()
        self.assertTrue(i.response.send_message.call_args.kwargs["ephemeral"])
        self.assertIn("Что я умею", i.response.send_message.call_args.args[0])
        self.submit.assert_not_called()
        self.text_submit.assert_not_called()

    async def test_text_followup_reuses_the_video_context(self):
        saved = {"source": {"texts": ["Root source"]}, "history": [{"title": "Reducer", "narration": "Old narration"}]}
        I.state().put_explanation("789", "200", saved)
        target = message("Video reply", mid=200)
        target.author = SimpleNamespace(id=999, name="Bot", global_name=None, bot=True)
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = target
        await self.bot.on_message(message("<@999> Объясни текстом на примере", reference=ref))
        job = self.text_submit.call_args.args[1]
        self.assertEqual(job["followup"], saved)
        self.assertEqual(job["response_format"], "text")
        self.submit.assert_not_called()

    async def test_format_is_per_request_and_text_can_be_followed_by_video(self):
        saved = {"source": {"texts": ["Root source"]}, "history": [{"format": "text", "text": "Earlier text answer"}]}
        I.state().put_explanation("789", "200", saved)
        target = message("Earlier text answer", mid=200)
        target.author = SimpleNamespace(id=999, name="Bot", global_name=None, bot=True)
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = target
        await self.bot.on_message(message("<@999> Покажи другой пример", reference=ref))
        job = self.submit.call_args.args[1]
        self.assertEqual(job["response_format"], "video")
        self.assertEqual(job["followup"]["history"][0]["text"], "Earlier text answer")
        self.text_submit.assert_not_called()

    async def test_private_slash_query_can_request_text_even_with_3d_option(self):
        i = interaction({"type": 1, "name": "explain", "options": [{"name": "query", "value": "Объясни текстом маятник"},
                        {"name": "mode", "value": "3d"}, {"name": "private", "value": True}]})
        await self.bot.on_interaction(i)
        job = self.text_submit.call_args.args[1]
        self.assertTrue(job["private"])
        self.assertEqual(job["response_format"], "text")
        i.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        self.submit.assert_not_called()

    async def test_research_slash_and_natural_forum_study_select_video_worker(self):
        i = interaction({"type": 1, "name": "research", "options": [{"name": "query", "value": "Почему закрылся сервер? https://forum.example/"},
                        {"name": "private", "value": True}]})
        await self.bot.on_interaction(i)
        job = self.submit.call_args.args[1]
        self.assertEqual(job["text_task"], "research")
        self.assertTrue(job["private"])
        self.text_submit.assert_not_called()
        self.submit.reset_mock()
        msg = message("<@999> какие причины закрытия сервера, изучи https://forum.ss13.ru")
        await self.bot.on_message(msg)
        self.assertEqual(self.submit.call_args.args[1]["text_task"], "research")
        self.assertIn("исследование", msg.reply.call_args.args[0])
        self.text_submit.assert_not_called()

    async def test_research_slash_explicit_text_option_selects_text_worker(self):
        i = interaction({"type": 1, "name": "research", "options": [{"name": "query", "value": "История сервера"},
                        {"name": "format", "value": "text"}]})
        await self.bot.on_interaction(i)
        self.assertEqual(self.text_submit.call_args.args[1]["text_task"], "research")
        self.submit.assert_not_called()

    async def test_text_directive_inside_target_post_is_not_a_format_request(self):
        i = interaction({"type": 3, "name": "Explain (video)", "target_id": "91", "resolved": {"messages": {
            "91": {"id": "91", "content": "объясни текстом", "attachments": []}}}})
        await self.bot.on_interaction(i)
        self.assertEqual(self.submit.call_args.args[1]["response_format"], "video")
        self.text_submit.assert_not_called()

    async def test_mentioned_reply_uses_saved_script_and_accepts_another_video(self):
        saved = {"root_id": "200", "source": {"texts": ["Original source"], "images": []},
                 "history": [{"question": "Explain reducers", "title": "Reducer", "narration": "State plus event produces new state."}]}
        I.state().put_explanation("789", "200", saved)
        target = message("Previous video", mid=200)
        target.author = SimpleNamespace(id=999, name="Bot", global_name=None, bot=True)
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = target
        msg = message("<@999> Show a concrete project", reference=ref)
        await self.bot.on_message(msg)
        job = self.submit.call_args.args[1]
        self.assertEqual(job["followup"], saved)
        self.assertEqual(job["text"], "Show a concrete project")
        self.assertEqual(job["context_messages"], 0)
        self.assertEqual(job["followup_message_id"], "200")
        self.assertEqual(job["response_message_id"], "400")
        self.assertIn("видео", msg.reply.call_args.args[0])

    async def test_followups_keep_separate_video_branches(self):
        for mid, subject in [(200, "Reducer"), (201, "Fork and flush")]:
            saved = {"source": {"texts": [subject]}, "history": [{"title": subject}]}
            I.state().put_explanation("789", str(mid), saved)
            target = message(subject, mid=mid)
            target.author = SimpleNamespace(id=999, name="Bot", global_name=None, bot=True)
            ref = discord.MessageReference(message_id=mid, channel_id=789)
            ref.resolved = target
            msg = message("<@999> Give an example", mid=mid + 100, reference=ref)
            await self.bot.on_message(msg)
            self.assertEqual(self.submit.call_args.args[1]["followup"]["history"][0]["title"], subject)

    async def test_old_video_recovers_original_request_without_inventing_narration(self):
        original = message("<@999> Explain https://example.com/source", mid=100)
        parent = discord.MessageReference(message_id=100, channel_id=789)
        parent.resolved = original
        target = message("**Old explanation**", mid=200, reference=parent)
        target.author = SimpleNamespace(id=999, name="Bot", global_name=None, bot=True)
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = target
        msg = message("<@999> Give an example", reference=ref)
        await self.bot.on_message(msg)
        context = self.submit.call_args.args[1]["followup"]
        self.assertIn("https://example.com/source", context["source_job"]["text"])
        self.assertFalse(context["history"][0]["script_available"])
        self.assertEqual(context["history"][0]["narration"], "")

    async def test_missing_old_source_does_not_render_the_bot_video_as_the_subject(self):
        target = message("**Old explanation**", mid=200)
        target.author = SimpleNamespace(id=999, name="Bot", global_name=None, bot=True)
        ref = discord.MessageReference(message_id=200, channel_id=789)
        ref.resolved = target
        msg = message("<@999> Explain more", reference=ref)
        await self.bot.on_message(msg)
        self.submit.assert_not_called()
        self.assertIn("исходную ссылку", msg.reply.call_args.args[0])
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM quota").fetchone()[0], 0)

    async def test_followup_cannot_read_memory_from_another_channel(self):
        msg = message("<@999> Explain more", reference=discord.MessageReference(message_id=200, channel_id=456))
        with patch.object(I.state(), "get_explanation") as memory:
            await self.bot.on_message(msg)
        memory.assert_not_called()
        msg.channel.fetch_message.assert_not_awaited()
        self.submit.assert_not_called()

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

    async def test_object_library_browse_does_not_render_or_consume_quota(self):
        i=interaction({"type":1,"name":"objects","options":[{"name":"query","value":"маятник"}]})
        with patch("object_library.describe",return_value="Found pendulum") as describe:
            await self.bot.on_interaction(i)
        describe.assert_called_once_with("маятник")
        i.response.send_message.assert_awaited_once()
        self.assertTrue(i.response.send_message.call_args.kwargs["ephemeral"])
        self.submit.assert_not_called()
        self.assertEqual(I.state().db.execute("SELECT count(*) FROM quota").fetchone()[0],0)

    async def test_single_object_search_attaches_private_preview(self):
        preview=Path(self.tmp.name)/"preview.png"
        preview.write_bytes(b'\x89PNG\r\n\x1a\n')
        i=interaction({"type":1,"name":"objects","options":[{"name":"query","value":"маятник"}]})
        with patch("object_library.describe",return_value="Маятник"), patch("object_library.search",return_value=[{"preview":str(preview)}]):
            await self.bot.on_interaction(i)
        sent=i.response.send_message.call_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(sent["file"].filename,"object-preview.png")
        self.assertTrue(sent["file"].fp.closed)
        self.submit.assert_not_called()

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
        self.assertEqual({c["name"] for c in commands}, {"help", "research", "explain", "objects", "Explain (video)", I.ASK_COMMAND})
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
