"""End-to-end self-test: the bot sends itself a mention and the real code handles it.

    python e2e_selftest.py                                   # default scenarios, hermetic
    python e2e_selftest.py "объясни текстом, что такое энтропия"
    python e2e_selftest.py --live "..."                      # real Messages API (+ Jev when TYPESAFE_API_KEY is set)

The request goes through ExplainBot.on_message -> mention_job -> pipeline.handle_job and the tool library over the
executor socket. Only the outside world is replaced, on 127.0.0.1: Discord REST (always fake, so nothing is posted
to a real channel), and in hermetic mode the Messages API and Jev with a scripted, deterministic "brain". The message
author is a synthetic account because the bot ignores bot authors; the content is written by this script.
Prints an observation transcript and exits 1 when a check fails. Run it after every change (see CLAUDE.md).
"""
import argparse
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import urllib.parse

BOT_ID, USER_ID, CHANNEL_ID = 990000000000000001, 990000000000000002, 990000000000000003
DEFAULT_SCENARIOS = [
    {"request": "объясни текстом, сколько дней осталось до нового года и какой это будет день недели", "expect": "text"},
    {"request": "ответь текстом: что такое градиентный спуск, в двух абзацах", "expect": "text"},
    {"request": "помощь", "expect": "help"},
]


def system_text(body):
    system = body.get("system") or ""
    return system if isinstance(system, str) else "".join(b.get("text", "") for b in system if isinstance(b, dict))


def user_text(body):
    parts = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(parts)


def phase(body):
    tools = [t.get("name") for t in body.get("tools") or []]
    system = system_text(body)
    if "web_search" in tools:
        return "research"
    if "capability router" in system.lower():
        return "router"
    if system.startswith("You answer questions for a Discord explanation bot"):
        return "text_answer"
    if system.startswith("Summarize the supplied video/audio"):
        return "summary"
    return "other"


class Brain:
    """Deterministic stand-in for the model: enough to drive every branch the scenarios need."""

    def messages(self, body):
        kind = phase(body)
        if kind == "research":
            text = json.dumps({"findings": [], "answer_status": "not_needed", "direct_answer": "", "evidence_urls": [],
                               "clarification_required": False, "clarification_question": ""})
        elif kind == "router":
            text = json.dumps(self.route(body))
        elif kind == "text_answer":
            request = re.search(r"<(request|question)>\s*(.*?)\s*</\1>", user_text(body), re.S)
            dated = re.search(r"Date of the request \(UTC\): (\S+)", user_text(body))
            asked = (request[2] if request else user_text(body))[:200].strip()
            text = (f"[e2e-brain] Текстовый ответ на запрос: «{asked}» (дата запроса в промпте: "
                    f"{dated[1] if dated else 'НЕ ПЕРЕДАНА'}).\n\n"
                    "Это детерминированный ответ тестового мозга: он проверяет маршрут запроса, доставку и формат, "
                    "а не качество объяснения.")
        else:
            text = "[e2e-brain] unexpected phase"
        return {"id": "msg_e2e", "type": "message", "role": "assistant", "model": body.get("model"), "stop_reason": "end_turn",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": len(text) // 4}}

    @staticmethod
    def route(body):
        """The router answers with the first tool whose examples share words with the request, else no tool."""
        catalog = re.findall(r'"id":\s*"([a-z0-9_]+)"', system_text(body) + user_text(body))
        return {"verdict": "covered" if catalog else "not_needed", "tools": catalog[:1], "confidence": 0.6,
                "gap": None, "reason": "e2e brain"}

    @staticmethod
    def jev(body):
        state = json.dumps(body.get("state"), ensure_ascii=False).casefold()
        words = set(re.findall(r"\w{4,}", state))
        answers = {}
        for name, question in (body.get("questions") or {}).items():
            if question["type"] == "choice":
                labels = list(question["criteria"])
                scores = [1 + len(words & set(re.findall(r"\w{4,}", json.dumps(question["criteria"][label] or label,
                                                                                ensure_ascii=False).casefold())))
                          for label in labels]
                total = sum(scores)
                best = max(range(len(labels)), key=scores.__getitem__)
                answers[name] = {"type": "choice", "choice": labels[best], "confidence": round(scores[best] / total, 3),
                                 "probabilities": {label: round(s / total, 3) for label, s in zip(labels, scores)}}
            elif question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.5}
            else:
                levels = len(question["criteria"])
                answers[name] = {"type": "score", "score": (levels - 1) / 2, "confidence": 0.5,
                                 "legend": {str(i): c for i, c in enumerate(question["criteria"])},
                                 "probabilities": {str(i): round(1 / levels, 3) for i in range(levels)}}
        return {"model": body.get("model"), "usage": {"input_tokens": len(state) // 4, "output_tokens": 1}, "answers": answers}


class World:
    """Fake Discord REST (+ Messages API and Jev in hermetic mode) on one loopback server."""

    def __init__(self, hermetic):
        self.hermetic, self.brain, self.lock = hermetic, Brain(), threading.Lock()
        self.discord, self.model_calls, self.jev_calls, self.next_id = [], [], [], 990000000000100000
        world = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                return self.rfile.read(int(self.headers.get("content-length") or 0))

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if re.fullmatch(r"/api/v10/channels/\d+/messages", path):
                    return self._reply(200, [])
                self._reply(404, {"message": "Unknown Message", "code": 10008})

            def do_PATCH(self):
                match = re.fullmatch(r"/api/v10/channels/(\d+)/messages/(\d+)", urllib.parse.urlsplit(self.path).path)
                if not match:
                    return self._reply(404, {})
                world.record_discord("edit", match[2], self._body(), self.headers.get("content-type", ""))
                self._reply(200, {"id": match[2], "channel_id": match[1]})

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                body = self._body()
                if re.fullmatch(r"/api/v10/channels/\d+/messages", path):
                    message_id = world.new_id()
                    world.record_discord("post", message_id, body, self.headers.get("content-type", ""))
                    return self._reply(200, {"id": message_id, "channel_id": str(CHANNEL_ID)})
                if path == "/v1/messages" and world.hermetic:
                    request = json.loads(body)
                    with world.lock:
                        world.model_calls.append({"phase": phase(request), "model": request.get("model"),
                                                  "tools": [t.get("name") for t in request.get("tools") or []],
                                                  "chars": len(body)})
                    return self._reply(200, world.brain.messages(request))
                if path == "/v1/systemone" and world.hermetic:
                    request = json.loads(body)
                    with world.lock:
                        world.jev_calls.append({"questions": list(request.get("questions") or {}), "chars": len(body)})
                    return self._reply(200, world.brain.jev(request))
                self._reply(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def new_id(self):
        with self.lock:
            self.next_id += 1
            return str(self.next_id)

    def record_discord(self, op, message_id, body, content_type):
        entry = {"op": op, "message_id": str(message_id), "content": "", "attachment_bytes": 0, "t": time.monotonic()}
        if content_type.startswith("application/json"):
            entry["content"] = json.loads(body or b"{}").get("content") or ""
        else:
            entry["attachment_bytes"] = len(body)
            match = re.search(rb'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
            entry["content"] = json.loads(b'"' + match[1] + b'"') if match else ""
        with self.lock:
            self.discord.append(entry)


class FakeUser:
    def __init__(self, user_id, name, bot):
        self.id, self.name, self.global_name, self.display_name, self.bot = user_id, name, name, name, bot

    def __str__(self):
        return self.name


class FakeChannel:
    id = CHANNEL_ID

    async def fetch_message(self, message_id):
        raise LookupError("the self-test channel has no earlier messages")


class FakeStatus:
    def __init__(self, world, message_id):
        self.world, self.id = world, int(message_id)

    async def edit(self, content=None, **kwargs):
        self.world.record_discord("edit", self.id, json.dumps({"content": content}).encode(), "application/json")


class FakeMessage:
    """The minimal discord.Message surface used by on_message/mention_job."""

    def __init__(self, world, text):
        self.world, self.id = world, int(world.new_id())
        self.content, self.channel = f"<@{BOT_ID}> {text}", FakeChannel()
        self.author = FakeUser(USER_ID, "e2e-selftest", bot=False)
        self.created_at = datetime.now(timezone.utc)
        self.attachments, self.embeds, self.stickers, self.message_snapshots = [], [], [], []
        self.reference = self.webhook_id = None

    async def reply(self, content=None, **kwargs):
        message_id = self.world.new_id()
        self.world.record_discord("post", message_id, json.dumps({"content": content}).encode(), "application/json")
        return FakeStatus(self.world, message_id)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def configure(world, live, data_dir, socket_path):
    os.environ.update({"DISCORD_API_BASE": world.url + "/api/v10", "DISCORD_BOT_TOKEN": "e2e-discord-token",
                       "DATA_DIR": data_dir, "TOOLS_DIR": str(Path(__file__).resolve().parent.parent / "tools")
                       if (Path(__file__).resolve().parent.parent / "tools").is_dir() else os.environ.get("TOOLS_DIR", "/tools"),
                       "REGISTER_COMMANDS": "0", "DAILY_LIMIT": "0", "BOT_LANG": os.environ.get("BOT_LANG") or "ru"})
    if live:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("--live needs ANTHROPIC_API_KEY")
        # Never inherit a base URL from the surrounding tooling: the bot talks to the public API or to the fake.
        os.environ["ANTHROPIC_BASE_URL"] = os.environ.get("E2E_ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    else:
        os.environ.update({"ANTHROPIC_BASE_URL": world.url, "ANTHROPIC_API_KEY": "e2e-anthropic-key",
                           "TYPESAFE_BASE_URL": world.url, "TYPESAFE_API_KEY": "e2e-typesafe-key"})
    if socket_path:
        os.environ["TOOLS_EXECUTOR_SOCKET"] = socket_path
        os.environ.pop("TOOLS_LOCAL_SANDBOX", None)


def start_executor():
    """Use the deployment's executor when its socket exists, else run one in-process on a temporary socket."""
    existing = os.environ.get("TOOLS_EXECUTOR_SOCKET")
    if existing and os.path.exists(existing):
        return existing, "deployment executor"
    import tool_executor
    path = os.path.join(tempfile.mkdtemp(prefix="e2e-tools-"), "executor.sock")
    threading.Thread(target=tool_executor.serve, args=(path,), daemon=True).start()
    for _ in range(100):
        if os.path.exists(path):
            break
        time.sleep(0.02)
    return path, "in-process executor (test only)"


def run_scenario(bot, world, scenario):
    import gateway
    started = len(world.discord), len(world.model_calls), len(world.jev_calls)
    message = FakeMessage(world, scenario["request"])
    clock = time.monotonic()
    asyncio.run(bot.on_message(message))
    # Wait for the job the handler submitted (text and video workers are single-threaded executors).
    for pool in (bot.text_responder, bot.renderer):
        pool.submit(lambda: None).result(timeout=float(os.environ.get("E2E_TIMEOUT") or 600))
    elapsed = time.monotonic() - clock
    discord = world.discord[started[0]:]
    strings = gateway.mention_strings()
    status = next((d for d in discord if d["op"] == "post"), None)
    edits = [d for d in discord if status and d["op"] == "edit" and d["message_id"] == status["message_id"]]
    final = edits[-1] if edits else status
    extra_parts = [d for d in discord if d["op"] == "post" and d is not status]
    import pipeline
    failures = [pipeline.S["failed"], pipeline.S["text_failed"], strings["failed"]]
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    check("bot replied in the channel", status, "no message was posted")
    if scenario.get("expect") == "help":
        check("help is immediate (no model calls)", len(world.model_calls) == started[1], "a model was called for help")
    else:
        expected = {"text": strings["text"], "video": strings["accepted"]}.get(scenario.get("expect"))
        check("status matches the requested format",
              status and (status["content"] == expected if expected else status["content"] in strings.values()),
              f"got {status['content'][:80]!r}, expected {expected!r}" if status else "")
        check("final answer delivered", final and final is not status and final["content"].strip(), "status was never replaced")
        check("no failure message", final and not any(f.split("\n")[0] in final["content"] for f in failures),
              final["content"][:200] if final else "")
        if scenario.get("expect") == "text":
            check("text request uploaded no video", not any(d["attachment_bytes"] for d in discord), "an attachment was uploaded")
            if world.hermetic:
                phases = [c["phase"] for c in world.model_calls[started[1]:]]
                check("text answer came from the text-answer stage", "text_answer" in phases, f"phases: {phases}")
                check("the request date reached the model", final and "НЕ ПЕРЕДАНА" not in final["content"],
                      "relative dates would be computed from the model's training data")
    return {"scenario": scenario, "seconds": round(elapsed, 2), "status": status, "edits": edits, "final": final,
            "extra_parts": extra_parts, "model_calls": world.model_calls[started[1]:],
            "jev_calls": world.jev_calls[started[2]:], "checks": checks}


def tool_probe():
    import tool_registry
    import tool_runtime
    registry = tool_registry.registry()
    registry.sync(force=True)
    reports = [tool_runtime.run_tests(tool) for tool in registry.tools()]
    return {"tools": [tool_registry.summary(t) for t in registry.tools(include_inactive=True)],
            "invalid": registry.errors, "tests": [{k: r[k] for k in ("tool", "digest", "passed", "total")} |
                                                  {"failed": [c["name"] for c in r["cases"] if not c["passed"]]} for r in reports]}


def show(result, records):
    scenario = result["scenario"]
    print(f"\n━━ request: «{scenario['request']}»  (expect: {scenario.get('expect', '?')}, {result['seconds']}s)")
    for job_line in [r for r in records if r.name in ("router", "tools", "jev_usage", "claude_usage")]:
        print(f"   log  {job_line.name}: {job_line.getMessage()[:220]}")
    for call in result["model_calls"]:
        print(f"   model {call['phase']:<12} tools={call['tools'] or '-'} request={call['chars']} chars")
    for call in result["jev_calls"]:
        print(f"   jev   questions={call['questions']} request={call['chars']} chars")
    if result["status"]:
        print(f"   discord status : {result['status']['content'][:200]}")
    for edit in result["edits"][:-1]:
        print(f"   discord update : {edit['content'][:200]}")
    if result["final"] and result["final"] is not result["status"]:
        print("   discord final  : " + result["final"]["content"][:1500].replace("\n", "\n                    "))
    for part in result["extra_parts"]:
        print(f"   discord part   : {part['content'][:200]}")
    for name, ok, detail in result["checks"]:
        print(f"   {'✓' if ok else '✗'} {name}" + ("" if ok else f" — {detail}"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("requests", nargs="*", help="mention text without the bot mention")
    parser.add_argument("--expect", choices=["text", "video", "help", "clarify", "any"], default="any")
    parser.add_argument("--live", action="store_true", help="real Messages API (and Jev if TYPESAFE_API_KEY is set)")
    parser.add_argument("--allow-video", action="store_true", help="also run requests that render a video")
    parser.add_argument("--json", action="store_true", help="print the raw observations as JSON")
    args = parser.parse_args(argv)
    if "pipeline" in sys.modules or "gateway" in sys.modules:
        # Endpoints are read at import time; an already imported bot would talk to the real services.
        raise SystemExit("run e2e_selftest in a fresh process")
    world = World(hermetic=not args.live)
    data_dir = tempfile.mkdtemp(prefix="e2e-data-")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    configure(world, args.live, data_dir, None)
    socket_path, executor = start_executor()
    configure(world, args.live, data_dir, socket_path)
    capture = Capture()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)  # the console shows warnings; the capture keeps INFO for the transcript
    logging.getLogger().addHandler(capture)
    logging.getLogger().setLevel(logging.INFO)
    for noisy in ("discord", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    import gateway
    from render_modes import response_format
    scenarios = [{"request": r, "expect": args.expect} for r in args.requests] or DEFAULT_SCENARIOS
    bot = gateway.ExplainBot("e2e-discord-token")
    bot._connection.user = FakeUser(BOT_ID, "BigBro", bot=True)
    print(f"e2e self-test: {'LIVE Messages API' if args.live else 'hermetic (scripted brain)'}; Discord: fake; tools: {executor}")
    probe = tool_probe()
    print("tools: " + ", ".join(f"{t['id']}[{t['status']}]" for t in probe["tools"]) + (f"; invalid: {probe['invalid']}" if probe["invalid"] else ""))
    for report in probe["tests"]:
        print(f"   {report['tool']} {report['digest']}: {report['passed']}/{report['total']} declarative tests in the executor"
              + (f" — failed: {report['failed']}" if report["failed"] else ""))
    results, ok = [], all(r["passed"] == r["total"] for r in probe["tests"]) and not probe["invalid"]
    for scenario in scenarios:
        if not args.live and not args.allow_video and scenario.get("expect") != "help" and \
                response_format(scenario["request"]) == "video":
            print(f"\n━━ skipped «{scenario['request']}»: renders a video; use --live --allow-video")
            continue
        mark = len(capture.records)
        result = run_scenario(bot, world, scenario)
        results.append(result)
        show(result, capture.records[mark:])
        ok = ok and all(passed for _, passed, _ in result["checks"])
    errors = [r for r in capture.records if r.levelno >= logging.ERROR]
    for record in errors:
        print(f"ERROR {record.name}: {record.getMessage()[:300]}")
    ok = ok and not errors
    if args.json:
        print(json.dumps({"probe": probe, "results": results}, ensure_ascii=False, default=str, indent=2))
    print(f"\n{'PASS' if ok else 'FAIL'}: {sum(len(r['checks']) for r in results)} checks in {len(results)} scenarios")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
