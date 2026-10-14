"""Jev decision client: wire format, local validation, answer normalization and fail-fast retries; no network."""
import http.server
import json
import os
import threading
import unittest
import urllib.error
from unittest.mock import patch

import jev

KEY = "ts-test-secret-4c1f"
STATE = "user 1234 asks: explain the private screenshot"
SECRET = "question text that must stay private"
ENV = ("TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL", "JEV_ROUTER", "JEV_TIMEOUT", "JEV_MAX_STATE_CHARS")

QUESTIONS = {
    "route": jev.choice("Which pipeline fits the request?", {"video": "an animated explainer", "text": None}),
    "depth": jev.score(None, ["one sentence", "a paragraph", "a long report"]),
    "unsafe": jev.noul("Does the request ask for dangerous instructions?"),
}
ANSWERS = {
    "route": {"type": "choice", "choice": "video", "confidence": 0.91, "probabilities": {"video": 0.91, "text": 0.09}},
    "depth": {"type": "score", "score": 1.2, "confidence": 0.6,
              "legend": {"0": "one sentence", "1": "a paragraph", "2": "a long report"},
              "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}},
    "unsafe": {"type": "noul", "noul": 0.02},
}


def reply(answers=None, status=200, request_id="req_1", **extra):
    body = {"model": "jev-test", "usage": {"input_tokens": 42, "output_tokens": 3},
            "answers": ANSWERS if answers is None else answers, **extra}
    return status, {"x-typesafe-request-id": request_id}, json.dumps(body).encode()


def with_answer(name, **changes):
    answers = json.loads(json.dumps(ANSWERS))
    answers[name].update(changes)
    return answers


class JevTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY})
        env.start()
        self.addCleanup(env.stop)
        for name in ENV:
            os.environ.pop(name, None)
        sleep = patch.object(jev.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def assertPrivate(self, text):
        for secret in (KEY, STATE, SECRET):
            self.assertNotIn(secret, text)

    # ------------------------------------------------------------ request
    def test_request_body_and_headers_match_wire_protocol(self):
        with patch.object(jev, "_http", return_value=reply()) as http:
            jev.system_one(STATE, QUESTIONS)
        url, data, headers, method, timeout = http.call_args.args
        self.assertEqual(url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(method, "POST")
        self.assertEqual(headers, {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                                   "Accept": "application/json"})
        self.assertEqual(json.loads(data), {"state": STATE, "model": "jev-latest", "questions": {
            "route": {"type": "choice", "instructions": "Which pipeline fits the request?",
                      "criteria": {"video": "an animated explainer", "text": None}},
            "depth": {"type": "score", "criteria": ["one sentence", "a paragraph", "a long report"]},
            "unsafe": {"type": "noul", "instructions": "Does the request ask for dangerous instructions?"}}})
        self.assertTrue(9 < timeout <= 10)

    def test_environment_and_arguments_override_defaults(self):
        os.environ.update({"TYPESAFE_BASE_URL": "https://jev.example.test/", "TYPESAFE_DEFAULT_MODEL": "jev-env",
                           "JEV_TIMEOUT": "3"})
        state = {"request": STATE, "attachments": ["image"]}
        with patch.object(jev, "_http", return_value=reply()) as http:
            jev.system_one(state, QUESTIONS)
            jev.system_one(state, QUESTIONS, model="jev-arg", timeout=1.5)
        (url, first, _, _, timeout), (_, second, _, _, explicit) = (c.args for c in http.call_args_list)
        self.assertEqual(url, "https://jev.example.test/v1/systemone")
        self.assertEqual(json.loads(first)["state"], state)
        self.assertEqual((json.loads(first)["model"], json.loads(second)["model"]), ("jev-env", "jev-arg"))
        self.assertTrue(2 < timeout <= 3 and 1 < explicit <= 1.5)

    def test_helpers_and_hand_built_questions_omit_none_fields(self):
        self.assertEqual(jev.choice(None, {"a": None}), {"type": "choice", "criteria": {"a": None}})
        self.assertEqual(jev.score({"rubric": "length"}, ("short", "long")),
                         {"type": "score", "instructions": {"rubric": "length"}, "criteria": ["short", "long"]})
        self.assertEqual(jev.noul("Is it spam?"), {"type": "noul", "instructions": "Is it spam?"})
        self.assertEqual(jev.noul(None, true="an advert"),
                         {"type": "noul", "criteria": {"true": "an advert", "false": None}})
        questions = {"route": {"type": "choice", "instructions": None, "criteria": {"video": None, "text": None}},
                     "unsafe": {"type": "noul", "instructions": "Unsafe?", "criteria": None}}
        answers = {"route": ANSWERS["route"], "unsafe": ANSWERS["unsafe"]}
        with patch.object(jev, "_http", return_value=reply(answers)) as http:
            jev.system_one(STATE, questions)
        self.assertEqual(json.loads(http.call_args.args[1])["questions"], {
            "route": {"type": "choice", "criteria": {"video": None, "text": None}},
            "unsafe": {"type": "noul", "instructions": "Unsafe?"}})

    def test_invalid_input_raises_before_any_request(self):
        cases = {
            "no questions": (STATE, {}), "questions not an object": (STATE, [jev.noul(SECRET)]),
            "empty name": (STATE, {"": jev.noul(SECRET)}), "non-string name": (STATE, {1: jev.noul(SECRET)}),
            "question not an object": (STATE, {"q": SECRET}),
            "unknown type": (STATE, {"q": {"type": "rank", "instructions": SECRET, "criteria": [SECRET]}}),
            "unknown field": (STATE, {"q": {"type": "noul", "instructions": SECRET, "criterion": {"true": SECRET}}}),
            "instructions not content": (STATE, {"q": {"type": "noul", "instructions": 5}}),
            "choice without criteria": (STATE, {"q": {"type": "choice", "instructions": SECRET}}),
            "choice empty criteria": (STATE, {"q": {"type": "choice", "instructions": SECRET, "criteria": {}}}),
            "choice criteria list": (STATE, {"q": {"type": "choice", "instructions": SECRET, "criteria": [SECRET]}}),
            "choice empty label": (STATE, {"q": {"type": "choice", "criteria": {"": SECRET}}}),
            "choice bad description": (STATE, {"q": {"type": "choice", "criteria": {"a": 3}}}),
            "score empty levels": (STATE, {"q": {"type": "score", "instructions": SECRET, "criteria": []}}),
            "score levels object": (STATE, {"q": {"type": "score", "criteria": {"0": SECRET}}}),
            "score null level": (STATE, {"q": {"type": "score", "criteria": [SECRET, None]}}),
            "noul with nothing to judge": (STATE, {"q": {"type": "noul"}}),
            "noul only null outcomes": (STATE, {"q": {"type": "noul", "criteria": {"true": None, "false": None}}}),
            "noul criteria not an object": (STATE, {"q": {"type": "noul", "instructions": SECRET, "criteria": [SECRET]}}),
            "noul unknown outcome": (STATE, {"q": {"type": "noul", "instructions": SECRET, "criteria": {"maybe": SECRET}}}),
            "noul bad outcome": (STATE, {"q": {"type": "noul", "criteria": {"true": 1.5}}}),
            "state missing": (None, QUESTIONS), "state number": (5, QUESTIONS),
            "state not serializable": ({"text": STATE, "ids": {1, 2}}, QUESTIONS),
            "state NaN": ({"text": STATE, "x": float("nan")}, QUESTIONS),
        }
        with patch.object(jev, "_http") as http:
            for name, (state, questions) in cases.items():
                with self.subTest(name), self.assertRaises(jev.JevError) as caught:
                    jev.system_one(state, questions)
                self.assertPrivate(str(caught.exception))
            for kwargs in ({"model": ""}, {"model": 7}, {"timeout": 0}, {"timeout": -1}, {"timeout": "10"},
                           {"timeout": float("inf")}):
                with self.subTest(**kwargs), self.assertRaises(jev.JevError):
                    jev.system_one(STATE, QUESTIONS, **kwargs)
            for build in (lambda: jev.choice(SECRET, {}), lambda: jev.score(SECRET, []), lambda: jev.noul(None),
                          lambda: jev.noul(None, None, None)):
                with self.assertRaises(jev.JevError) as caught:
                    build()
                self.assertPrivate(str(caught.exception))
        http.assert_not_called()

    def test_serialized_request_size_is_capped(self):
        with patch.object(jev, "_http", return_value=reply()) as http:
            with self.assertRaises(jev.JevError) as caught:
                jev.system_one("x" * 24001, QUESTIONS)
            self.assertIn("JEV_MAX_STATE_CHARS=24000", str(caught.exception))
            os.environ["JEV_MAX_STATE_CHARS"] = "800"
            with self.assertRaises(jev.JevError) as caught:
                jev.system_one(STATE + "y" * 600, QUESTIONS)
            self.assertPrivate(str(caught.exception))
            http.assert_not_called()
            jev.system_one(STATE, QUESTIONS)
            self.assertLessEqual(len(http.call_args.args[1].decode()), 800)
            os.environ["JEV_MAX_STATE_CHARS"] = "lots"  # an unparseable value falls back to the default
            jev.system_one("z" * 20000, QUESTIONS)
        self.assertEqual(http.call_count, 2)

    def test_base_url_must_be_https_except_loopback(self):
        with patch.object(jev, "_http") as http:
            for base in ("http://api.typesafe.ai", "ftp://api.typesafe.ai", "https://"):
                os.environ["TYPESAFE_BASE_URL"] = base
                with self.subTest(base), self.assertRaises(jev.JevError):
                    jev.system_one(STATE, QUESTIONS)
        http.assert_not_called()

    # ----------------------------------------------------------- response
    def test_answers_are_normalized(self):
        questions = {**QUESTIONS, "tone": jev.noul("Is it a joke?")}
        answers = with_answer("depth", score=2.0000004, confidence=1.0002,
                              probabilities={"2": 0.3000001, "0": -0.0000002, "1": 0.7})
        answers["unsafe"]["noul"] = 1.0000003
        answers["tone"] = {"type": "verdict", "verdict": "joke"}         # a newer answer type is ignored
        answers["unrequested"] = {"type": "noul", "noul": 0.5}           # answers nobody asked for are ignored
        with patch.object(jev, "_http", return_value=reply(answers)):
            result = jev.system_one(STATE, questions)
        self.assertEqual(result, {"model": "jev-test", "usage": {"input_tokens": 42, "output_tokens": 3},
                                  "request_id": "req_1", "answers": {
            "route": {"type": "choice", "choice": "video", "confidence": 0.91,
                      "probabilities": {"video": 0.91, "text": 0.09}},
            "depth": {"type": "score", "score": 2.0, "confidence": 1.0,
                      "legend": {0: "one sentence", 1: "a paragraph", 2: "a long report"},
                      "probabilities": {0: 0.0, 1: 0.7, 2: 0.3000001}},
            "unsafe": {"type": "noul", "noul": 1.0}}})
        self.assertEqual(list(result["answers"]["depth"]["probabilities"]), [0, 1, 2])

    def test_optional_accounting_fields_and_legend_fall_back(self):
        answers = json.loads(json.dumps(ANSWERS))
        del answers["depth"]["legend"]
        body = json.dumps({"answers": answers}).encode()
        with patch.object(jev, "_http", return_value=(200, {}, body)):
            result = jev.system_one(STATE, QUESTIONS, model="jev-arg")
        self.assertEqual((result["model"], result["usage"], result["request_id"]),
                         ("jev-arg", {"input_tokens": 0, "output_tokens": 0}, None))
        self.assertEqual(result["answers"]["depth"]["legend"], {0: "one sentence", 1: "a paragraph", 2: "a long report"})

    def test_malformed_responses_raise(self):
        def body(value):
            return 200, {"x-typesafe-request-id": "req_bad"}, value
        missing = dict(ANSWERS)
        del missing["unsafe"]
        cases = {
            "not JSON": body(b"<html>bad gateway</html>"), "not an object": body(b"[]"),
            "no answers": body(json.dumps({"model": "jev-test"}).encode()),
            "missing answer": reply(missing), "answer not an object": reply({**ANSWERS, "route": "video"}),
            "untyped answer": reply({**ANSWERS, "unsafe": {"noul": 0.2}}),
            "type mismatch": reply({**ANSWERS, "route": {"type": "noul", "noul": 0.9}}),
            "choice not requested": reply(with_answer("route", choice="audio")),
            "choice not a string": reply(with_answer("route", choice=1)),
            "probability is text": reply(with_answer("route", probabilities={"video": "0.9", "text": 0.1})),
            "probability is bool": reply(with_answer("route", probabilities={"video": True})),
            "probability for unknown label": reply(with_answer("route", probabilities={"video": 0.9, "audio": 0.1})),
            "no probabilities": reply(with_answer("route", probabilities={})),
            "confidence out of range": reply(with_answer("route", confidence=5.0)),
            "confidence missing": reply(with_answer("depth", confidence=None)),
            "score key out of range": reply(with_answer("depth", probabilities={"0": 0.5, "3": 0.5})),
            "score key not a level": reply(with_answer("depth", probabilities={"low": 1.0})),
            "score key leading zero": reply(with_answer("depth", legend={"01": "a paragraph"})),
            "score beyond levels": reply(with_answer("depth", score=7.0)),
            "score legend not an object": reply(with_answer("depth", legend=["one sentence"])),
            "noul is text": reply(with_answer("unsafe", noul="yes")),
            "noul NaN": reply(with_answer("unsafe", noul=float("nan"))),
            "noul percentage": reply(with_answer("unsafe", noul=97)),
            "token count text": reply(usage={"input_tokens": "42", "output_tokens": 3}),
            "usage not an object": reply(usage=[42, 3]),
            "model not a string": reply(model=7),
        }
        for name, response in cases.items():
            with self.subTest(name), patch.object(jev, "_http", return_value=response):
                with self.assertRaises(jev.JevError) as caught:
                    jev.system_one(STATE, QUESTIONS)
                self.assertNotIsInstance(caught.exception, (jev.JevRejected, jev.JevUnavailable))
                self.assertIn(caught.exception.request_id, ("req_1", "req_bad"))
                self.assertPrivate(str(caught.exception))

    # -------------------------------------------------------------- retry
    def test_rate_limit_honors_retry_after_then_succeeds(self):
        limited = (429, {"retry-after": "1", "x-typesafe-request-id": "req_a"}, b'{"error":{"message":"slow down"}}')
        with patch.object(jev, "_http", side_effect=[limited, reply()]) as http, \
                self.assertLogs("jev", level="WARNING") as logs:
            result = jev.system_one(STATE, QUESTIONS)
        self.assertEqual((http.call_count, result["request_id"]), (2, "req_1"))
        self.sleep.assert_called_once_with(1.0)
        self.assertIn("HTTP 429", logs.output[0])
        self.assertIn("slow down", logs.output[0])
        self.assertPrivate("\n".join(logs.output))
        self.assertTrue(http.call_args.args[4] <= 10)

    def test_retry_after_ms_wins_and_backoff_covers_missing_hints(self):
        cases = [((503, {"retry-after-ms": "250", "retry-after": "3"}, b""), 0.25),
                 ((500, {}, b"internal"), 0.5), ((408, {"retry-after": "soon"}, b""), 0.5),
                 ((409, {"retry-after": "0"}, b""), 0.0)]
        for failure, wait in cases:
            self.sleep.reset_mock()
            with self.subTest(failure[0]), patch.object(jev, "_http", side_effect=[failure, reply()]) as http, \
                    self.assertLogs("jev", level="WARNING"):
                jev.system_one(STATE, QUESTIONS)
            self.assertEqual(http.call_count, 2)
            self.sleep.assert_called_once_with(wait)

    def test_connection_errors_are_retried(self):
        refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        with patch.object(jev, "_http", side_effect=[refused, ConnectionResetError(), reply()]) as http, \
                self.assertLogs("jev", level="WARNING"):
            jev.system_one(STATE, QUESTIONS)
        self.assertEqual(http.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5, 1.0])

    def test_retries_are_bounded(self):
        failure = (503, {"x-typesafe-request-id": "req_x"}, b'{"error":"overloaded"}')
        with patch.object(jev, "_http", return_value=failure) as http, self.assertLogs("jev", level="WARNING"):
            with self.assertRaises(jev.JevUnavailable) as caught:
                jev.system_one(STATE, QUESTIONS)
        self.assertEqual(http.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual((caught.exception.status, caught.exception.request_id), (503, "req_x"))

    def test_long_retry_after_fails_fast_instead_of_waiting(self):
        for headers in ({"retry-after": "30"}, {"retry-after-ms": "5001"}):
            with self.subTest(**headers), patch.object(jev, "_http", return_value=(429, headers, b"")) as http:
                with self.assertRaises(jev.JevUnavailable) as caught:
                    jev.system_one(STATE, QUESTIONS)
            self.assertEqual((http.call_count, caught.exception.status), (1, 429))
        # A hint that fits the cap but not the caller's remaining budget is not waited for either.
        with patch.object(jev, "_http", return_value=(429, {"retry-after": "2"}, b"")) as http:
            with self.assertRaises(jev.JevUnavailable):
                jev.system_one(STATE, QUESTIONS, timeout=2)
        self.assertEqual(http.call_count, 1)
        self.sleep.assert_not_called()

    def test_client_errors_are_rejected_without_retry(self):
        echo = json.dumps({"detail": [{"loc": ["body", "state"], "msg": "field invalid", "input": STATE}],
                           "hint": f"key {KEY} is not valid", "padding": "p" * 1000}).encode()
        for status in (400, 401, 403, 404, 422):
            response = (status, {"x-typesafe-request-id": "r"}, echo)
            with self.subTest(status), patch.object(jev, "_http", return_value=response) as http:
                with self.assertRaises(jev.JevRejected) as caught:
                    jev.system_one(STATE, QUESTIONS)
            message = str(caught.exception)
            self.assertEqual((http.call_count, caught.exception.status, caught.exception.request_id), (1, status, "r"))
            self.assertIn(f"HTTP {status}", message)
            self.assertIn("field invalid", message)
            self.assertLess(len(message), 260)
            self.assertPrivate(message + repr(caught.exception))
        self.sleep.assert_not_called()

    def test_timeouts_raise_unavailable_within_the_budget(self):
        clock = [1000.0]

        def slow(url, data, headers, method, timeout):
            clock[0] += timeout
            raise TimeoutError("timed out")

        with patch.object(jev.time, "monotonic", new=lambda: clock[0]), \
                patch.object(jev, "_http", side_effect=slow) as http:
            with self.assertRaises(jev.JevUnavailable) as caught:
                jev.system_one(STATE, QUESTIONS, timeout=2)
        self.assertEqual(http.call_count, 1)  # the budget is spent: no retry
        self.assertEqual(http.call_args.args[4], 2)
        self.assertIn("timed out", str(caught.exception))
        self.sleep.assert_not_called()

        connect_timeout = urllib.error.URLError(TimeoutError("timed out"))
        with patch.object(jev, "_http", side_effect=connect_timeout) as http, self.assertLogs("jev", level="WARNING"):
            with self.assertRaises(jev.JevUnavailable) as caught:
                jev.system_one(STATE, QUESTIONS)
        self.assertEqual(http.call_count, 3)
        self.assertIn("timed out", str(caught.exception))

    # ------------------------------------------------------ observability
    def test_usage_is_logged_as_counters_only(self):
        with patch.object(jev, "_http", return_value=reply()), self.assertLogs("jev_usage", level="INFO") as logs:
            jev.system_one({"request": STATE, "note": SECRET}, QUESTIONS)
        self.assertEqual(len(logs.output), 1)
        for expected in ("model=jev-test", "input=42", "output=3", "latency_ms=", "request_id=req_1"):
            self.assertIn(expected, logs.output[0])
        self.assertPrivate(logs.output[0])
        self.assertNotIn("Which pipeline", logs.output[0])

    def test_key_state_and_questions_never_reach_error_text(self):
        failures = [(401, {}, f"invalid api key {KEY}".encode()),
                    urllib.error.URLError(OSError("network unreachable")),
                    (200, {}, json.dumps({"answers": {"q": {"type": "noul", "noul": SECRET}}}).encode())]
        questions = {"q": jev.noul(SECRET)}
        for failure in failures:
            with self.subTest(failure), patch.object(jev, "_http", side_effect=[failure] * 3), \
                    self.assertLogs("jev", level="DEBUG") as logs:
                jev.log.debug("start")
                with self.assertRaises(jev.JevError) as caught:
                    jev.system_one(STATE, questions)
            self.assertPrivate(str(caught.exception) + repr(caught.exception) + "\n".join(logs.output))
            self.assertIsNone(caught.exception.__cause__)

    def test_configured_requires_key_and_respects_kill_switch(self):
        self.assertTrue(jev.configured())
        for value, expected in (("0", False), (" 0 ", False), ("1", True), ("", True)):
            os.environ["JEV_ROUTER"] = value
            self.assertEqual(jev.configured(), expected, value)
        os.environ.pop("JEV_ROUTER")
        for key in ("", "   "):
            os.environ["TYPESAFE_API_KEY"] = key
            self.assertFalse(jev.configured())
        del os.environ["TYPESAFE_API_KEY"]
        self.assertFalse(jev.configured())
        with patch.object(jev, "_http") as http:
            with self.assertRaises(jev.JevError):
                jev.system_one(STATE, QUESTIONS)
            os.environ.update({"TYPESAFE_API_KEY": KEY, "JEV_ROUTER": "0"})
            with self.assertRaises(jev.JevError):
                jev.system_one(STATE, QUESTIONS)
        http.assert_not_called()

    def test_models_lists_available_models(self):
        listed = {"models": [{"name": "jev-test", "description": "decisions", "release_date": "2026-09-01"},
                             {"name": "jev-small"}]}
        with patch.object(jev, "_http", return_value=(200, {}, json.dumps(listed).encode())) as http:
            models = jev.models()
        url, data, headers, method, _ = http.call_args.args
        self.assertEqual((url, data, method), ("https://api.typesafe.ai/v1/models", None, "GET"))
        self.assertEqual(headers, {"Authorization": f"Bearer {KEY}", "Accept": "application/json"})
        self.assertEqual(models, [listed["models"][0], {"name": "jev-small", "description": None, "release_date": None}])
        for bad in (b"{}", b"[]", b'{"models": [{"description": "no name"}]}'):
            with self.subTest(bad), patch.object(jev, "_http", return_value=(200, {}, bad)):
                with self.assertRaises(jev.JevError):
                    jev.models()


class Handler(http.server.BaseHTTPRequestHandler):
    seen = []
    script = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Handler.seen.append((self.path, dict(self.headers), body))
        status, headers, payload = Handler.script.pop(0)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST

    def log_message(self, *args):
        pass


class LoopbackTransportTests(unittest.TestCase):
    """The real urllib transport against a local server: headers, error statuses and redirects."""

    def setUp(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        env = patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY, "TYPESAFE_BASE_URL": self.base,
                                      "JEV_ROUTER": "1", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        env.start()
        self.addCleanup(env.stop)
        sleep = patch.object(jev.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        Handler.seen, Handler.script = [], []

    def test_round_trip_with_retry_on_real_transport(self):
        _, _, body = reply()
        Handler.script = [(429, {"Retry-After-Ms": "20"}, b'{"error":"busy"}'),
                          (200, {"Content-Type": "application/json", "X-TypeSafe-Request-Id": "req_live"}, body)]
        with self.assertLogs("jev", level="WARNING"):
            result = jev.system_one(STATE, QUESTIONS)
        self.assertEqual(result["request_id"], "req_live")
        self.sleep.assert_called_once_with(0.02)
        path, headers, sent = Handler.seen[-1]
        self.assertEqual(path, "/v1/systemone")
        self.assertEqual((headers["Authorization"], headers["Content-Type"], headers["Accept"]),
                         (f"Bearer {KEY}", "application/json", "application/json"))
        self.assertEqual(json.loads(sent)["state"], STATE)

    def test_redirects_are_not_followed_with_the_key(self):
        Handler.script = [(302, {"Location": f"{self.base}/elsewhere"}, b"")]
        with self.assertRaises(jev.JevRejected) as caught:
            jev.system_one(STATE, QUESTIONS)
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual([path for path, _, _ in Handler.seen], ["/v1/systemone"])


if __name__ == "__main__":
    unittest.main()
