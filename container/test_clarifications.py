"""Choice ownership, persistence, expiry, race safety, quotas and context retention; no network."""
import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import clarifications as C
import interactions as I


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "state.sqlite")
        self.state = I.State(self.path)
        self.old = I._state; I._state = self.state
        self.env = patch.dict(os.environ, {"BOT_LANG": "ru", "DAILY_LIMIT": "5", "CLARIFY_AMBIGUOUS": "1"})
        self.env.start()

    def tearDown(self):
        self.state.db.close(); I._state = self.old; self.env.stop(); self.tmp.cleanup()

    def put(self, origin="mention", **extra):
        job = {"kind": "message", "question": "разбери это", "channel_id": "789", "context_before_id": "300",
               "context_messages": 10, "locale": "ru", "message": {"id": "100", "content": "Original source",
               "attachments": [{"filename": "clip.mp4", "url": "https://cdn.example/clip.mp4", "content_type": "video/mp4"}]}, **extra}
        key, record = C.create(self.state, job, "42", "789", "разбери это", "action", origin)
        C.bind(self.state, key, "400")
        return key, record

    def click(self, key, action="text", revision=0, user="42", channel="789", mid="400", form=None):
        data = {"custom_id": f"clarify:{key}:{revision}:{action}"}
        if form is not None:
            data["components"] = [{"type": 1, "components": [{"custom_id": "q", "value": form}]}]
        return {"id": "901", "type": 5 if action == "form" else 3, "application_id": "456", "token": "fresh-token",
                "channel_id": channel, "locale": "ru", "user": {"id": user}, "message": {"id": mid}, "data": data}

    def quota(self):
        return self.state.db.execute("SELECT COALESCE(SUM(n),0) FROM quota").fetchone()[0]

    def test_vague_and_conflicting_requests_get_choices_without_changing_clear_defaults(self):
        for request in ("", "разбери это", "сделай что-нибудь", "текстом или видео", "объясни или сделай резюме",
                        "покажи что тут говорят текстом", "options"):
            self.assertIsNotNone(C.reason(request), request)
        for request in ("объясни градиентный спуск", "explain this", "градиент", "кто тут прав?", "why?",
                        "объясни что тут говорят текстом", "summarize видео", "сделай транскрипцию видео текстом",
                        "https://x.com/example/status/1", "3d маятник", "Объясни разницу между текстом и видео",
                        "Не объясняй текстом, сделай видео"):
            self.assertIsNone(C.reason(request), request)
        self.assertIsNone(C.reason("", has_material=True))
        with patch.dict(os.environ, {"CLARIFY_AMBIGUOUS": "0"}):
            self.assertIsNone(C.reason("разбери это"))

    def test_pending_snapshot_survives_restart_without_credentials(self):
        key, _ = self.put(origin="interaction", token="old-secret", application_id="456", private=True,
                          followup={"source": {"texts": ["Retained transcript"]}, "source_job": {"token": "nested-secret"}})
        stored = self.state.db.execute("SELECT payload FROM clarifications WHERE id=?", (key,)).fetchone()[0]
        self.assertNotIn("old-secret", stored); self.assertNotIn("nested-secret", stored)
        self.state.db.close(); self.state = I.State(self.path); I._state = self.state
        enqueue = Mock(return_value=True)
        _, response = I.handle(self.click(key, "summary"), enqueue)
        job = enqueue.call_args.args[0]
        self.assertEqual(response["type"], I.UPDATE_MESSAGE)
        self.assertTrue(job["private"])
        self.assertEqual(job["token"], "fresh-token")
        self.assertNotIn("response_message_id", job)
        self.assertEqual(job["followup"]["source"]["texts"], ["Retained transcript"])
        self.assertEqual(job["message"]["attachments"][0]["filename"], "clip.mp4")

    def test_waiting_is_free_and_choice_routes_to_same_public_picker(self):
        key, record = self.put()
        self.assertEqual(self.quota(), 0)
        choices = C.card(key, record)
        self.assertLessEqual(len(choices["content"]), 1900)
        self.assertTrue(all(len(b["custom_id"]) <= 100 for row in choices["components"] for b in row["components"]))
        enqueue = Mock(return_value=True)
        _, response = I.handle(self.click(key, "transcript"), enqueue)
        job = enqueue.call_args.args[0]
        self.assertEqual((job["response_format"], job["text_task"]), ("text", "transcript"))
        self.assertEqual((job["response_channel_id"], job["response_message_id"]), ("789", "400"))
        self.assertEqual(job["context_before_id"], "300")
        self.assertEqual(response["data"]["components"], [])
        self.assertEqual(self.quota(), 1)

    def test_different_user_channel_and_message_cannot_take_the_choice(self):
        key, _ = self.put()
        enqueue = Mock(return_value=True)
        for change in ({"user": "99"}, {"channel": "456"}, {"mid": "401"}):
            _, response = I.handle(self.click(key, **change), enqueue)
            self.assertEqual(response["data"]["flags"], I.EPHEMERAL)
            self.assertNotIn("Original source", response["data"]["content"])
        enqueue.assert_not_called(); self.assertEqual(self.quota(), 0)

    def test_research_choice_uses_deep_video_and_one_quota_unit(self):
        key, record = self.put(question="разбери историю https://forum.example/")
        choices = C.card(key, record)
        self.assertTrue(all(len(row["components"]) <= 5 for row in choices["components"]))
        self.assertIn("research", C.offered(record))
        enqueue = Mock(return_value=True)
        _, response = I.handle(self.click(key, "research"), enqueue)
        job = enqueue.call_args.args[0]
        self.assertEqual((job["response_format"], job["text_task"]), ("video", "research"))
        self.assertEqual(self.quota(), 1)
        self.assertEqual(response["data"]["components"], [])

    def test_cancel_expiry_and_duplicate_click_never_start_extra_work(self):
        key, _ = self.put(); enqueue = Mock(return_value=True)
        _, response = I.handle(self.click(key, "cancel"), enqueue)
        self.assertEqual(response["type"], I.UPDATE_MESSAGE)
        self.assertEqual(response["data"]["components"], [])
        I.handle(self.click(key), enqueue); enqueue.assert_not_called()
        key, _ = self.put()
        self.state.db.execute("UPDATE clarifications SET expires=? WHERE id=?", (time.time()-1, key)); self.state.db.commit()
        _, response = I.handle(self.click(key), enqueue)
        self.assertIn("устарел", response["data"]["content"])
        enqueue.assert_not_called(); self.assertEqual(self.quota(), 0)

    def test_parallel_clicks_accept_exactly_once(self):
        key, _ = self.put(); enqueue = Mock(return_value=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda action: I.handle(self.click(key, action), enqueue), ["text", "video"]))
        self.assertEqual(sorted(r[1]["type"] for r in results), [I.CHANNEL_MESSAGE, I.UPDATE_MESSAGE])
        enqueue.assert_called_once(); self.assertEqual(self.quota(), 1)

    def test_busy_queue_and_daily_limit_keep_pending_choice_without_charge(self):
        key, _ = self.put()
        _, response = I.handle(self.click(key), Mock(return_value=False))
        self.assertIn("очередь", response["data"]["content"]); self.assertEqual(self.quota(), 0)
        self.assertIsNotNone(C.pending_card(self.state, key))
        with patch.dict(os.environ, {"DAILY_LIMIT": "1"}):
            self.state.take_quota("42", 1)
            enqueue = Mock(return_value=True)
            _, response = I.handle(self.click(key), enqueue)
            self.assertIn("лимит", response["data"]["content"])
            enqueue.assert_not_called(); self.assertEqual(self.quota(), 1)

    def test_edit_modal_preserves_source_and_invalidates_old_buttons(self):
        key, _ = self.put(); enqueue = Mock(return_value=True)
        _, response = I.handle(self.click(key, "edit"), enqueue)
        self.assertEqual(response["type"], I.MODAL)
        question = "Объясни этот материал текстом. " + "Дополнительные детали. "*40
        _, edited = I.handle(self.click(key, "form", form=question), enqueue)
        self.assertEqual(edited["type"], I.UPDATE_MESSAGE)
        self.assertIn("уточнён", edited["data"]["content"])
        self.assertEqual(self.quota(), 0); enqueue.assert_not_called()
        _, stale = I.handle(self.click(key, "summary"), enqueue)
        self.assertIn("новые кнопки", stale["data"]["content"])
        I.handle(self.click(key, "text", revision=1), enqueue)
        job = enqueue.call_args.args[0]
        self.assertEqual(job["question"], question.strip())
        self.assertEqual(job["message"]["id"], "100")

    def test_ack_failure_rollback_is_idempotent_and_cannot_refund_a_later_claim(self):
        key, _ = self.put(); enqueue = Mock(return_value=True)
        I.handle(self.click(key), enqueue); first = enqueue.call_args.args[0]
        C.rollback(self.state, first); C.rollback(self.state, first)
        self.assertEqual(self.quota(), 0)
        I.handle(self.click(key), enqueue)
        C.rollback(self.state, first)
        self.assertEqual(self.quota(), 1)

    def test_non_media_choices_cannot_be_forged_into_transcription(self):
        key, record = self.put(message={"id": "100", "content": "A diagram", "attachments": [{"filename": "a.png", "content_type": "image/png"}]})
        self.assertNotIn("transcript", C.offered(record))
        enqueue = Mock(return_value=True)
        _, response = I.handle(self.click(key, "transcript"), enqueue)
        self.assertIn("недоступен", response["data"]["content"])
        enqueue.assert_not_called()

    def test_per_user_pending_storage_is_bounded(self):
        keys = [self.put()[0] for _ in range(C.MAX_PER_USER + 2)]
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM clarifications").fetchone()[0], C.MAX_PER_USER)
        self.assertIsNone(C.pending_card(self.state, keys[0]))

    def test_research_clarification_keeps_original_quota_credit_and_drops_transient_bytes(self):
        self.state.take_quota("42", 1)
        job = {"kind": "query", "text": "кто такой пример", "channel_id": "789", "quota_paid": True,
               "image_originals": [b"binary-image"], "prepared_inputs": ([], [], []), "context_messages": 10}
        key, record = C.create(self.state, job, "42", "789", job["text"], "evidence", prompt="Какое сообщество?", credit=True)
        C.bind(self.state, key, "400")
        self.assertEqual(C.offered(record), set())
        self.assertIn("Какое сообщество", C.card(key, record)["content"])
        I.handle(self.click(key, "form", form="Кто такой пример на форуме?"), Mock())
        enqueue = Mock(return_value=True)
        with patch.dict(os.environ, {"DAILY_LIMIT": "1"}):
            I.handle(self.click(key, "text", revision=1), enqueue)
        self.assertEqual(self.quota(), 1)
        self.assertNotIn("image_originals", enqueue.call_args.args[0])
        C.rollback(self.state, enqueue.call_args.args[0])
        self.assertEqual(self.quota(), 1)

    def test_pipeline_can_publish_a_late_context_card_without_persisting_tokens_or_pixels(self):
        import pipeline as P
        job = {"kind": "query", "text": "кто такой пример", "request_user_id": "42", "request_origin": "mention",
               "channel_id": "789", "response_channel_id": "789", "response_message_id": "400", "quota_paid": True,
               "token": "must-not-persist", "image_originals": [b"pixels"], "prepared_inputs": ([], [], []),
               "research_assessment": {"status": "ambiguous", "direct_answer": "", "question": "Какое сообщество?"}}
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test-key"}), patch.object(P, "_http", return_value=(200, b'{"id":"400"}')) as send:
            self.assertTrue(P.request_missing_context(job))
        payload = json.loads(send.call_args.args[1])
        self.assertIn("Какое сообщество", payload["content"])
        self.assertEqual({b["custom_id"].rsplit(":",1)[-1] for row in payload["components"] for b in row["components"]}, {"edit", "cancel"})
        saved = self.state.db.execute("SELECT payload FROM clarifications").fetchone()[0]
        self.assertNotIn("must-not-persist", saved)
        self.assertNotIn("pixels", saved)
        self.assertTrue(json.loads(saved)["quota_credit"])

    def test_private_slash_prompt_does_not_enqueue_or_charge(self):
        enqueue = Mock(return_value=True)
        _, response = I.handle({"type": I.APPLICATION_COMMAND, "application_id": "456", "token": "sensitive-old",
            "channel_id": "789", "user": {"id": "42"}, "data": {"type": 1, "name": "explain", "options": [
                {"name": "query", "value": "сделай что-нибудь"}, {"name": "private", "value": True}]}}, enqueue)
        self.assertEqual(response["type"], I.CHANNEL_MESSAGE)
        self.assertEqual(response["data"]["flags"], I.EPHEMERAL)
        self.assertTrue(response["data"]["components"])
        self.assertEqual({b["custom_id"].rsplit(":",1)[-1] for row in response["data"]["components"] for b in row["components"]}, {"edit", "cancel"})
        enqueue.assert_not_called(); self.assertEqual(self.quota(), 0)


if __name__ == "__main__":
    unittest.main()
