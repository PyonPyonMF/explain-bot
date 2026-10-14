"""Job journal, resume and graceful drain; no network, tokens, or paid API calls."""
import asyncio
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import gateway
import interactions as I
import job_journal as J


def mention_job(**extra):
    return {"kind": "query", "text": "explain gradients", "channel_id": "789", "context_before_id": "300",
            "request_user_id": "42", "request_origin": "mention", "context_messages": 10, "bot_user_id": "999",
            "locale": "ru", "render_mode": "2d", "response_format": "video", "text_task": "explain",
            "message": {"id": "300", "content": "explain gradients", "attachments": []},
            "quota_paid": True, "response_channel_id": "789", "response_message_id": "400", **extra}


def keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(v) for v in value)) if value else set()
    return set()


def interaction(data, kind=discord.InteractionType.application_command):
    return SimpleNamespace(
        id=123, application_id=456, token="interaction-secret", type=kind,
        locale=discord.Locale.russian, channel_id=789, user=SimpleNamespace(id=42), data=data, message=None,
        edit_original_response=AsyncMock(),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
                                 edit_message=AsyncMock(), is_done=lambda: False),
    )


def message(content="<@999> explain gradients", mid=300):
    msg = Mock(spec=discord.Message)
    msg.id, msg.content, msg.reference = mid, content, None
    msg.created_at = datetime(2026, 10, 9, tzinfo=timezone.utc)
    msg.author = SimpleNamespace(id=42, name="Reader", global_name="Reader", bot=False)
    msg.channel = SimpleNamespace(id=789, fetch_message=AsyncMock())
    msg.attachments, msg.embeds, msg.message_snapshots, msg.stickers = [], [], [], []
    msg.webhook_id = None
    msg.reply = AsyncMock(return_value=SimpleNamespace(id=mid + 100, edit=AsyncMock()))
    return msg


class Isolated:
    def isolate(self, **env):
        self.env = patch.dict(os.environ, {"DAILY_LIMIT": "5", "BOT_LANG": "ru", "MAX_QUEUED_JOBS": "1", **env})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.old = I._state, J._journal
        I._state = I.State(str(Path(self.tmp.name) / "bot.sqlite"))
        J._journal = J.Journal(str(Path(self.tmp.name) / "jobs.sqlite"))
        self.notify = patch.object(J, "notify")
        self.notified = self.notify.start()

    def restore(self):
        self.notify.stop()
        I._state.db.close()
        J._journal.db.close()
        I._state, J._journal = self.old
        self.tmp.cleanup()
        self.env.stop()

    def rows(self):
        return J.get().db.execute("SELECT id, state, attempts, created, payload FROM jobs ORDER BY created, rowid").fetchall()

    def quota(self):
        row = I.state().db.execute("SELECT n FROM quota WHERE user='42'").fetchone()
        return row[0] if row else 0


class JournalTests(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()

    def tearDown(self):
        self.restore()

    def test_token_bound_jobs_are_never_written(self):
        interaction_job = {"application_id": "456", "token": "interaction-secret", "kind": "query", "text": "x",
                           "channel_id": "789", "quota_paid": True}
        self.assertIsNone(J.get().record(interaction_job))
        self.assertIsNone(J.get().record({**mention_job(), "token": "interaction-secret"}))
        self.assertIsNone(J.get().record({**mention_job(), "response_message_id": None}))
        self.assertEqual(self.rows(), [])
        self.assertNotIn(b"interaction-secret", Path(self.tmp.name, "jobs.sqlite").read_bytes())

    def test_snapshot_has_no_credentials_or_derived_data(self):
        job = mention_job(application_id="456", animation_code="def construct(): pass",
                          prepared_inputs=([{"type": "image"}], ["<linked_page>"], []), assets=[{"path": "/tmp/a.png"}],
                          video_script={"narration": "spoken"}, _clarification_id="abc", _clarification_claim="def",
                          request_message={"id": "300", "content": "q", "token": "nested-secret", "api_key": "k"})
        key = J.get().record(job)
        self.assertTrue(key)
        payload = self.rows()[0][4]
        stored = json.loads(payload)
        self.assertFalse(keys(stored) & J.SECRETS)
        for leaked in ("nested-secret", "def construct", "prepared_inputs", "linked_page", "spoken", "_clarification"):
            self.assertNotIn(leaked, payload)
        self.assertEqual((stored["text"], stored["quota_paid"], stored["response_message_id"]), ("explain gradients", True, "400"))
        self.assertEqual(stat.S_IMODE(os.stat(Path(self.tmp.name, "jobs.sqlite")).st_mode), 0o600)

    def test_oversized_snapshot_is_not_journaled_or_logged(self):
        job = mention_job(message={"id": "300", "content": "секрет " * 400_000})
        with self.assertLogs("job_journal", level="WARNING") as logs:
            self.assertIsNone(J.get().record(job))
        self.assertNotIn("секрет", "\n".join(logs.output))
        self.assertEqual(self.rows(), [])

    def test_stored_followup_memory_is_journaled_by_reference(self):
        memory = {"root_id": "100", "history": [{"question": "q", "narration": "n"}],
                  "source": {"images": [], "texts": ["x" * 1_500_000]}}
        I.state().put_explanation("789", "100", memory)
        J.get().record(mention_job(followup=memory, followup_message_id="100"))
        self.assertLess(len(self.rows()[0][4]), 10_000)
        submit = Mock()
        self.assertEqual(J.resume(lambda: True, submit), 1)
        self.assertEqual(submit.call_args.args[0]["followup"], memory)

    def test_resume_counts_started_attempts_and_drops_exhausted_jobs(self):
        key = J.get().record(mention_job())
        submit = Mock()
        self.assertEqual(J.resume(lambda: True, submit), 1)  # Queued, never started: no attempt was used.
        self.assertEqual(self.rows()[0][1:3], ("queued", 0))
        J.get().start(key)  # Interrupted mid-render by a restart.
        self.assertEqual(self.rows()[0][1:3], ("running", 1))
        self.assertEqual(J.resume(lambda: True, submit), 1)
        self.assertEqual(self.rows()[0][1:3], ("queued", 1))
        self.assertEqual(self.notified.call_args.args[1], J.strings()["resumed"])
        self.assertEqual(submit.call_args.args[1], key)
        J.get().start(key)  # Interrupted again: JOB_MAX_ATTEMPTS=2 starts are used up.
        self.assertEqual(self.rows()[0][2], 2)
        self.assertEqual(J.resume(lambda: True, submit), 0)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.notified.call_args.args[1], J.strings()["expired"])
        self.assertEqual(self.rows(), [])

    def test_old_jobs_and_jobs_beyond_capacity_are_dropped_with_quota_returned(self):
        for _ in range(3):
            I.state().take_quota("42", 5)
        old, late, fits = (J.get().record(mention_job(response_message_id=str(400 + i))) for i in range(3))
        J.get().db.execute("UPDATE jobs SET created=created-? WHERE id=?", (7 * 3600, old))
        J.get().db.commit()
        capacity = iter([False, True])  # Not reached for the old job.
        submit = Mock()
        self.assertEqual(J.resume(lambda: next(capacity), submit), 1)
        self.assertEqual(submit.call_args.args[1], fits)
        texts = [c.args[1] for c in self.notified.call_args_list]
        self.assertEqual(texts.count(J.strings()["expired"]), 2)
        self.assertEqual(self.quota(), 1)
        self.assertEqual([r[0] for r in self.rows()], [fits])

    def test_corrupted_row_is_dropped_without_contacting_discord(self):
        J.get().db.execute("INSERT INTO jobs VALUES ('bad', 'queued', 0, ?, ?, 'not json')", (1e10, 1e10))
        J.get().db.commit()
        self.assertEqual(J.resume(lambda: True, Mock()), 0)
        self.notified.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_status_and_wait_idle_report_counts_only(self):
        key = J.get().record(mention_job())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(J.main(["status"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"journal": {"queued": 1, "running": 0},
                                                      "process": {"running": 0, "waiting": 0, "draining": False}})
        self.assertNotIn("gradients", out.getvalue())
        self.assertEqual(J.main(["wait-idle", "--timeout", "0"]), 0)
        J.get().start(key)
        self.assertEqual(J.main(["wait-idle", "--timeout", "0"]), 1)
        J.get().finish(key)
        J.get().activity(1, 0)  # A slash-command job runs without a journal row.
        self.assertEqual(J.main(["wait-idle", "--timeout", "0"]), 1)
        J.get().activity(0, 0)
        self.assertEqual(J.main(["wait-idle", "--timeout", "1"]), 0)

    def test_refused_interaction_returns_quota(self):
        i = {"type": 2, "application_id": "456", "token": "t", "channel_id": "789", "user": {"id": "42"},
             "data": {"type": 1, "name": "explain", "options": [{"name": "query", "value": "градиентный спуск"}]}}
        code, response = I.handle(i, lambda job: False)
        self.assertIn("занят", response["data"]["content"])
        self.assertEqual(self.quota(), 0)

    def test_http_mode_journals_only_resumable_jobs(self):
        import server
        self.addCleanup(lambda: [server.JOBS.get_nowait() for _ in range(server.JOBS.qsize())])
        self.assertTrue(server.enqueue({"application_id": "456", "token": "t", "kind": "query", "text": "x"}))
        self.assertEqual(server.JOBS.get_nowait()[1], None)
        key = J.get().record(mention_job())
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}):
            server.resume()
        self.assertTrue(server.JOBS.empty())
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "bot-token"}):
            server.resume()
        job, queued_key = server.JOBS.get_nowait()
        self.assertEqual((queued_key, job["response_message_id"]), (key, "400"))


class GatewayRestartTests(Isolated, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.isolate(MAX_QUEUED_JOBS="2", DRAIN_TIMEOUT="5")
        self.bot = self.new_bot()

    async def asyncTearDown(self):
        await self.bot.close()
        self.restore()

    def new_bot(self):
        bot = gateway.ExplainBot("test-token")
        bot._connection.user = SimpleNamespace(id=999)
        return bot

    async def test_mention_job_is_journaled_until_handle_job_returns(self):
        self.bot.renderer.submit = Mock()
        await self.bot.on_message(message())
        render, job, key = self.bot.renderer.submit.call_args.args
        self.assertEqual(self.rows()[0][0:2], (key, "queued"))
        states = []
        with patch.dict("sys.modules", {"pipeline": SimpleNamespace(handle_job=lambda j: states.append(self.rows()[0][1]))}):
            render(job, key)
        self.assertEqual(states, ["running"])
        self.assertEqual(self.rows(), [])

        slash = interaction({"type": 1, "name": "explain", "options": [{"name": "query", "value": "градиентный спуск"}]})
        await self.bot.on_interaction(slash)
        self.assertEqual(self.bot.renderer.submit.call_args.args[1]["token"], "interaction-secret")
        self.assertIsNone(self.bot.renderer.submit.call_args.args[2])
        self.assertEqual(self.rows(), [])

    async def test_restart_resumes_once_without_taking_quota_again(self):
        self.bot.renderer.submit = Mock()
        await self.bot.on_message(message())  # Accepted, then the process stops before rendering.
        self.assertEqual(self.quota(), 1)
        restarted = self.new_bot()
        restarted.renderer.submit = Mock()
        try:
            with patch.object(gateway, "bot_request", return_value={"id": "456"}), patch.object(gateway, "register_commands"), \
                    patch.object(I.State, "take_quota", side_effect=AssertionError("quota taken twice")):
                await restarted.setup_hook()
                await restarted.on_ready()
            job = restarted.renderer.submit.call_args.args[1]
            restarted.renderer.submit.assert_called_once()
            self.assertEqual((job["quota_paid"], job["response_message_id"]), (True, "400"))
            self.assertEqual(self.notified.call_args.args, (job, J.strings()["resumed"]))
            self.assertEqual(self.quota(), 1)
            self.assertEqual(J.get().counts()["process"]["waiting"], 1)
        finally:
            await restarted.close()

    async def test_drain_refuses_new_jobs_keeps_queued_and_waits_for_running(self):
        started, release, ran = threading.Event(), threading.Event(), []

        def handle_job(job):
            ran.append(job["context_before_id"])
            started.set()
            release.wait(10)

        with patch.dict("sys.modules", {"pipeline": SimpleNamespace(handle_job=handle_job)}):
            await self.bot.on_message(message(mid=300))
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            await self.bot.on_message(message(mid=301))  # Waits behind the running render.
            slash = interaction({"type": 1, "name": "explain", "options": [{"name": "query", "value": "градиентный спуск"}]})
            await self.bot.on_interaction(slash)  # Token-bound, waits too.
            self.assertEqual(self.quota(), 3)
            self.assertEqual([r[1] for r in self.rows()], ["running", "queued"])

            drain = asyncio.create_task(self.bot.drain())
            await asyncio.sleep(0.5)
            self.assertFalse(drain.done())
            self.assertTrue(J.get().counts()["process"]["draining"])
            late = message(mid=302)
            await self.bot.on_message(late)
            self.assertEqual(late.reply.call_args.args[0], J.strings()["restarting"])
            late_slash = interaction({"type": 1, "name": "explain", "options": [{"name": "query", "value": "градиентный спуск"}]})
            await self.bot.on_interaction(late_slash)
            self.assertEqual(late_slash.response.send_message.call_args.args[0], J.strings()["restarting"])
            late_slash.response.defer.assert_not_awaited()
            # The waiting slash job is told to resend and refunded; the waiting mention job stays journaled.
            dropped = [c.args for c in self.notified.call_args_list]
            self.assertEqual([(j.get("token"), text) for j, text in dropped], [("interaction-secret", J.strings()["restarting"])])
            self.assertEqual(self.quota(), 2)

            release.set()
            await asyncio.wait_for(drain, 5)
            await asyncio.to_thread(self.bot.renderer.shutdown, True)
        self.assertTrue(self.bot.is_closed())
        self.assertEqual(ran, ["300"])
        rows = self.rows()
        self.assertEqual([(r[1], json.loads(r[4])["context_before_id"]) for r in rows], [("queued", "301")])

    async def test_drain_timeout_leaves_running_job_for_resume(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with patch.dict(os.environ, {"DRAIN_TIMEOUT": "0.3"}), \
                patch.dict("sys.modules", {"pipeline": SimpleNamespace(handle_job=lambda job: release.wait(10))}):
            await self.bot.on_message(message())
            for _ in range(50):
                if self.bot.running:
                    break
                await asyncio.sleep(0.05)
            await asyncio.wait_for(self.bot.drain(), 5)
            self.assertEqual(self.bot.running, 1)
            self.assertEqual(self.rows()[0][1:3], ("running", 1))
            release.set()
            await asyncio.to_thread(self.bot.renderer.shutdown, True)
        self.assertEqual(self.rows(), [])  # Had it finished after all, the row is gone.

    async def test_sigterm_drains_and_a_second_signal_stops_waiting(self):
        self.bot.drain = AsyncMock()
        self.bot.request_stop()
        self.bot.request_stop()
        await self.bot.drain_task
        self.bot.drain.assert_awaited_once()
        self.assertEqual(self.bot.drain_deadline, 0.0)

        stopped = Mock()

        async def start(token):
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(50):
                if stopped.called:
                    return
                await asyncio.sleep(0.02)

        bot = self.new_bot()
        with patch.object(bot, "start", start), patch.object(bot, "request_stop", stopped):
            await gateway.serve(bot, "test-token")
        stopped.assert_called_once()
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
        asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
