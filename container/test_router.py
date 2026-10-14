"""Shadow capability router: minimized inputs, the Jev -> Messages API cascade, normalization and the gap backlog."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jev
import pipeline
import router

TOOLS = [{"id": "date_calc", "status": "probation", "title": "Calendar calculations", "description": "Exact date math.",
          "when_not_to_use": "", "examples": ["сколько дней до нового года?"]}]
STATE = {"request": "сколько дней до нового года?", "attachments": [], "link_hosts": [], "requested_format": "text",
         "task": "explain", "date": "2026-10-14"}


def jev_reply(choice, confidence, exact=0.2):
    return {"model": "jev-latest", "usage": {}, "request_id": None, "answers": {
        "route": {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": {choice: confidence}},
        "exact": {"type": "noul", "noul": exact}}}


def model_reply(verdict):
    return 200, json.dumps({"content": [{"type": "text", "text": json.dumps(verdict)}], "stop_reason": "end_turn"}).encode()


GAP = {"verdict": "gap", "tools": [], "confidence": 0.9, "reason": "no QR generator",
       "gap": {"capability": "QR Code!", "summary": "Make QR images.", "inputs": "url", "outputs": "png",
               "example_requests": ["сделай qr"], "network_hosts": ["api.example.org", "bad host", "x"], "category": "conversion"}}


class StateTests(unittest.TestCase):
    def test_only_the_users_own_words_attachment_kinds_and_link_hosts(self):
        job = {"kind": "message", "question": "что тут на https://secret.example.org/private/path?token=1 ?",
               "message": {"content": "someone else's post", "author": {"username": "alice"},
                           "attachments": [{"content_type": "image/png", "filename": "a.png"}]},
               "request_message": {"timestamp": "2026-10-14T10:00:00+00:00",
                                   "attachments": [{"content_type": "application/pdf", "filename": "doc.pdf"}]},
               "response_format": "text", "text_task": "explain", "followup": {"history": []}}
        state = router.request_state(job)
        text = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("someone else's post", text)
        self.assertNotIn("alice", text)
        self.assertNotIn("/private/path", text)
        self.assertNotIn("token=1", text)
        self.assertIn("<link:secret.example.org>", state["request"])
        self.assertEqual(state["link_hosts"], ["secret.example.org"])
        self.assertEqual(state["attachments"], ["pdf"])
        self.assertEqual(state["replying_to_post_with"], ["image", "text"])
        self.assertTrue(state["follow_up_to_an_earlier_answer"])
        self.assertEqual(state["date"], "2026-10-14")


class CascadeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "TYPESAFE_API_KEY": "t", "JEV_ROUTER": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_confident_system_one_needs_no_model_call(self):
        with patch.object(jev, "system_one", return_value=jev_reply("date_calc", 0.95)), \
                patch.object(pipeline, "_http") as http:
            decision = router.decide(STATE, TOOLS)
        http.assert_not_called()
        self.assertEqual((decision["verdict"], decision["tools"], decision["source"]), ("covered", ["date_calc"], "system_one"))

    def test_uncertain_gap_or_exact_builtin_escalate(self):
        for reply in (jev_reply("date_calc", 0.5), jev_reply("gap", 0.99), jev_reply("builtin", 0.95, exact=0.9)):
            with self.subTest(reply=reply["answers"]["route"]["choice"]), \
                    patch.object(jev, "system_one", return_value=reply), \
                    patch.object(pipeline, "_http", return_value=model_reply(GAP)) as http:
                decision = router.decide(STATE, TOOLS)
            http.assert_called_once()
            self.assertEqual((decision["verdict"], decision["source"]), ("gap", "system_two"))
            self.assertEqual(decision["jev"]["choice"], reply["answers"]["route"]["choice"])

    def test_jev_failure_or_absence_falls_back(self):
        with patch.object(jev, "system_one", side_effect=jev.JevUnavailable("down")), \
                patch.object(pipeline, "_http", return_value=model_reply({**GAP, "verdict": "builtin", "gap": None})):
            self.assertEqual(router.decide(STATE, TOOLS)["source"], "system_two")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), patch.object(jev, "system_one") as first, \
                patch.object(pipeline, "_http", return_value=model_reply({**GAP, "verdict": "builtin", "gap": None})):
            router.decide(STATE, TOOLS)
        first.assert_not_called()

    def test_system_two_request_shape(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), \
                patch.object(pipeline, "_http", return_value=model_reply(GAP)) as http:
            router.decide(STATE, TOOLS)
        body = json.loads(http.call_args.args[1])
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["system"][0]["cache_control"]["type"], "ephemeral")
        system = json.dumps(body["system"], ensure_ascii=False)
        self.assertIn("date_calc", system)  # the catalog is part of the cached instructions
        self.assertNotIn("нового года", system.split("<library_tools>")[0])  # the request is not
        self.assertIn("нового года", body["messages"][0]["content"])

    def test_refusal_is_recorded_not_raised(self):
        reply = 200, json.dumps({"content": [], "stop_reason": "refusal"}).encode()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), patch.object(pipeline, "_http", return_value=reply):
            self.assertEqual(router.decide(STATE, TOOLS)["verdict"], "refused")


class NormalizeTests(unittest.TestCase):
    def test_unknown_tools_names_and_hosts_are_cleaned(self):
        self.assertEqual(router.normalize({"verdict": "covered", "tools": ["nope"], "confidence": 3}, TOOLS)["verdict"], "builtin")
        gap = router.normalize(GAP, TOOLS)["gap"]
        self.assertEqual(gap["capability"], "qr_code")
        self.assertEqual(gap["network_hosts"], ["api.example.org"])
        self.assertEqual(router.normalize({"verdict": "whatever"}, TOOLS)["verdict"], "error")
        self.assertEqual(router.normalize({"verdict": "builtin", "confidence": 7}, TOOLS)["confidence"], 1.0)


class BacklogTests(unittest.TestCase):
    def test_requests_are_kept_only_for_gaps_and_grouped(self):
        path = Path(tempfile.mkdtemp()) / "router.sqlite"
        backlog = router.Backlog(path)
        gap = {**router.normalize(GAP, TOOLS), "source": "system_two", "jev": None}
        builtin = {**router.normalize({"verdict": "builtin", "confidence": 0.9}, TOOLS), "source": "system_one", "jev": None}
        backlog.add(gap, STATE, 12)
        backlog.add(gap, {**STATE, "request": "другой запрос про qr"}, 10)
        backlog.add(builtin, STATE, 5)
        rows = backlog.recent()
        self.assertIsNone(rows[0]["request"])
        self.assertEqual(rows[1]["request"], "другой запрос про qr")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        gaps = backlog.gaps()
        self.assertEqual((gaps[0]["capability"], gaps[0]["requests"]), ("qr_code", 2))
        self.assertEqual(gaps[0]["spec"]["summary"], "Make QR images.")


class ShadowTests(unittest.TestCase):
    def test_off_by_default_and_never_raises(self):
        with patch.dict(os.environ, {"ROUTER_SHADOW": "0"}), patch.object(router, "request_state") as state:
            self.assertFalse(router.observe({"text": "x"}))
        state.assert_not_called()
        with patch.dict(os.environ, {"ROUTER_SHADOW": "1"}), patch.object(router, "request_state", side_effect=KeyError):
            self.assertFalse(router.observe({"text": "x"}))

    def test_background_decision_is_recorded(self):
        backlog = router.Backlog(Path(tempfile.mkdtemp()) / "router.sqlite")
        decision = {**router.normalize({"verdict": "builtin", "confidence": 0.9}, TOOLS), "source": "system_two", "jev": None}
        with patch.dict(os.environ, {"ROUTER_SHADOW": "1"}), patch.object(router, "backlog", return_value=backlog), \
                patch.object(router, "decide", return_value=decision):
            self.assertTrue(router.observe({"kind": "query", "text": "почему кошки мурлыкают"}))
            self.assertTrue(router.wait_idle(10))
        self.assertEqual(backlog.recent()[0]["verdict"], "builtin")

    def test_a_failing_router_does_not_touch_the_job(self):
        job = {"kind": "query", "text": "объясни текстом x", "response_format": "text", "text_task": "explain"}
        with patch.object(router, "observe", side_effect=RuntimeError("boom")), \
                patch("followups.prepare", side_effect=pipeline.UserError("stop here")), \
                patch.object(pipeline, "_safe_text") as reply:
            pipeline.handle_job(job)
        self.assertEqual(reply.call_args.args[1], "stop here")


if __name__ == "__main__":
    unittest.main()
