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
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse

BOT_ID, USER_ID, CHANNEL_ID = 990000000000000001, 990000000000000002, 990000000000000003
OWNER_ID, DM_CHANNEL_ID = 990000000000000004, 990000000000000005
FIXTURES = Path(__file__).resolve().parent / "e2e_fixtures"
# A capability the bot lacks; the forge-built version is e2e_fixtures/length_convert (see learning_loop).
LOOP_REQUESTS = ["объясни текстом, сколько метров в 10 футах", "переведи текстом 3 мили в километры"]
DEFAULT_SCENARIOS = [
    {"request": "объясни текстом, сколько дней осталось до нового года и какой это будет день недели", "expect": "text",
     "route": "covered:date_calc", "tool": "date_calc", "days_to_new_year": True},
    {"request": "ответь текстом: что такое градиентный спуск, в двух абзацах", "expect": "text",
     "route": "covered:wikipedia_summary"},
    {"request": "объясни текстом, как устроены QR-коды, и сгенерируй мне QR-код со ссылкой https://example.com/invite",
     "expect": "text", "route": "gap"},
    {"request": "объясни текстом, почему кошки мурлыкают", "expect": "text", "route": "builtin"},
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
        kind, stop, calls = phase(body), "end_turn", []
        if kind == "research":
            calls = self.date_calls(body) or self.length_calls(body)
            text = "[e2e-brain] Посчитаю даты инструментом." if calls else json.dumps(
                {"findings": [], "answer_status": "not_needed", "direct_answer": "", "evidence_urls": [],
                 "clarification_required": False, "clarification_question": ""})
            stop = "tool_use" if calls else stop
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
            quoted = []
            for tool, data in re.findall(r'<tool_result tool="([^"]+)" version="[^"]+">\s*(.*?)\s*</tool_result>', user_text(body), re.S):
                output = json.loads(data)["output"]
                quoted.append(f"{tool}: " + ", ".join(f"{k}={v}" for k, v in output.items()
                                                       if isinstance(v, (str, int, float)) and not isinstance(v, bool)))
            if quoted:
                text += "\n\nПо данным инструментов: " + "; ".join(quoted) + "."
        else:
            text = "[e2e-brain] unexpected phase"
        return {"id": "msg_e2e", "type": "message", "role": "assistant", "model": body.get("model"), "stop_reason": stop,
                "content": [{"type": "text", "text": text}] + calls,
                "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": len(text) // 4}}

    @staticmethod
    def date_calls(body):
        """Like a model that prefers exact arithmetic: date_calc for a days/weekday question, once, from the request date."""
        offered = {t.get("name") for t in body.get("tools") or []}
        answered = any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for m in body.get("messages") or [] if isinstance(m.get("content"), list) for b in m["content"])
        request = re.search(r"<(request|question)>\s*(.*?)\s*</\1>", user_text(body), re.S)
        dated = re.search(r"Date of the request \(UTC\): (\d{4})-(\d\d-\d\d)", user_text(body))
        if answered or "date_calc" not in offered or not request or not dated or \
                not re.search(r"дн(?:ей|я)\b|день недели|\bdays?\b|weekday", request[2], re.I):
            return []
        today, new_year = f"{dated[1]}-{dated[2]}", f"{int(dated[1]) + 1}-01-01"
        if not re.search(r"нов(?:ого|ый|ым) год|new year", request[2], re.I):
            return [{"type": "tool_use", "id": "toolu_e2e_today", "name": "date_calc", "input": {"operation": "describe", "date": today}}]
        return [{"type": "tool_use", "id": "toolu_e2e_between", "name": "date_calc",
                 "input": {"operation": "between", "date": today, "other_date": new_year}},
                {"type": "tool_use", "id": "toolu_e2e_weekday", "name": "date_calc", "input": {"operation": "describe", "date": new_year}}]

    @staticmethod
    def length_calls(body):
        """Once the forge-built length_convert is offered, a conversion question uses it instead of mental arithmetic."""
        offered = {t.get("name") for t in body.get("tools") or []}
        answered = any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for m in body.get("messages") or [] if isinstance(m.get("content"), list) for b in m["content"])
        request = re.search(r"<(request|question)>\s*(.*?)\s*</\1>", user_text(body), re.S)
        match = request and re.search(r"(\d+(?:[.,]\d+)?)\s*(фут|мил|дюйм)", request[2], re.I)
        if answered or "length_convert" not in offered or not match:
            return []
        unit = {"фут": "ft", "мил": "mi", "дюйм": "in"}[match[2].lower()]
        target = "km" if unit == "mi" else "cm" if unit == "in" else "m"
        return [{"type": "tool_use", "id": "toolu_e2e_length", "name": "length_convert",
                 "input": {"value": float(match[1].replace(",", ".")), "from_unit": unit, "to_unit": target}}]

    @staticmethod
    def route(body):
        """Keyword stand-in for System Two: a gap for artifacts/actions, a tool whose examples overlap, else builtin."""
        state = json.loads(re.search(r"<request_description>\s*(.*?)\s*</request_description>", user_text(body), re.S)[1])
        request = state["request"].casefold()
        tools = [json.loads(line) for line in re.findall(r"^\{.*\}$", system_text(body), re.M)]
        words = set(re.findall(r"\w{4,}", request))
        best = max(tools, key=lambda t: len(words & set(re.findall(r"\w{4,}", " ".join(t["examples"]).casefold()))), default=None)
        if best and len(words & set(re.findall(r"\w{4,}", " ".join(best["examples"]).casefold()))) >= 2:
            return {"verdict": "covered", "tools": [best["id"]], "confidence": 0.8, "reason": "e2e brain", "gap": None}
        if re.search(r"qr|напомни|напоминани|remind|mp3", request):
            return {"verdict": "gap", "tools": [], "confidence": 0.9, "reason": "e2e brain: an artifact the bot cannot make",
                    "gap": {"capability": "QR code", "summary": "Generate a QR code image for a URL or text.",
                            "inputs": "text or URL, size", "outputs": "PNG image", "category": "conversion",
                            "example_requests": ["сделай QR-код для ссылки", "qr code for this text"], "network_hosts": []}}
        if re.search(r"фут|метр|миль|мили|километр|дюйм|feet|foot|mile|inch", request):
            return {"verdict": "gap", "tools": [], "confidence": 0.85, "reason": "e2e brain: exact unit conversion",
                    "gap": {"capability": "length_convert", "summary": "Convert lengths exactly between metric and imperial units.",
                            "inputs": "value, from unit, to unit", "outputs": "converted value", "category": "computation",
                            "example_requests": ["сколько метров в футах", "convert miles to km"], "network_hosts": []}}
        return {"verdict": "builtin", "tools": [], "confidence": 0.85, "reason": "e2e brain", "gap": None}

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
                scores = [score ** 3 for score in scores]  # decisive, like a calibrated model on clear cases
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
        self.discord, self.model_calls, self.jev_calls, self.dms, self.next_id = [], [], [], [], 990000000000100000
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
                world.record_discord("edit", match[2], self._body(), self.headers.get("content-type", ""), match[1])
                self._reply(200, {"id": match[2], "channel_id": match[1]})

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                body = self._body()
                match = re.fullmatch(r"/api/v10/channels/(\d+)/messages", path)
                if match:
                    message_id = world.new_id()
                    world.record_discord("post", message_id, body, self.headers.get("content-type", ""), match[1])
                    return self._reply(200, {"id": message_id, "channel_id": match[1]})
                if path == "/api/v10/users/@me/channels":
                    recipient = json.loads(body).get("recipient_id")
                    with world.lock:
                        world.dms.append(recipient)
                    return self._reply(200, {"id": str(DM_CHANNEL_ID), "type": 1})
                if path == "/v1/messages" and world.hermetic:
                    request = json.loads(body)
                    reply = world.brain.messages(request)
                    with world.lock:
                        world.model_calls.append({"phase": phase(request), "model": request.get("model"),
                                                  "tools": [t.get("name") for t in request.get("tools") or []],
                                                  "chars": len(body), "body": body.decode("utf-8", "replace"),
                                                  "calls": [b["name"] for b in reply["content"] if b["type"] == "tool_use"]})
                    return self._reply(200, reply)
                if path == "/v1/systemone" and world.hermetic:
                    request = json.loads(body)
                    with world.lock:
                        world.jev_calls.append({"questions": list(request.get("questions") or {}), "chars": len(body),
                                                "body": body.decode("utf-8", "replace")})
                    return self._reply(200, world.brain.jev(request))
                self._reply(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def new_id(self):
        with self.lock:
            self.next_id += 1
            return str(self.next_id)

    def record_discord(self, op, message_id, body, content_type, channel=CHANNEL_ID):
        entry = {"op": op, "message_id": str(message_id), "channel": str(channel), "content": "", "components": [],
                 "attachment_bytes": 0, "t": time.monotonic()}
        if content_type.startswith("application/json"):
            payload = json.loads(body or b"{}")
            entry["content"], entry["components"] = payload.get("content") or "", payload.get("components") or []
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


def library_copy(data_dir):
    """A private copy of the tool library, so the learning loop can 'merge' a tool without touching the repository."""
    source = Path(__file__).resolve().parent.parent / "tools"
    source = source if source.is_dir() else Path(os.environ.get("TOOLS_DIR") or "/tools")
    target = Path(data_dir) / "tools"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def configure(world, live, data_dir, socket_path):
    forge = Path(data_dir) / "forge-queue"
    forge.mkdir(exist_ok=True)
    os.environ.update({"DISCORD_API_BASE": world.url + "/api/v10", "DISCORD_BOT_TOKEN": "e2e-discord-token",
                       "DATA_DIR": data_dir, "TOOLS_DIR": str(Path(data_dir) / "tools"), "TOOLS_RESCAN": "0",
                       "REGISTER_COMMANDS": "0", "DAILY_LIMIT": "0", "BOT_LANG": os.environ.get("BOT_LANG") or "ru",
                       "ROUTER_SHADOW": "1", "FORGE_QUEUE": str(forge), "FORGE_OWNER_ID": str(OWNER_ID),
                       "FORGE_MIN_REQUESTS": "2", "FORGE_UID": str(os.getuid()), "FORGE_GID": str(os.getgid())})
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


def tool_ok_calls(tool_id):
    import tool_registry
    tool = tool_registry.registry().get(tool_id, include_inactive=True)
    return tool["ok"] if tool else 0


def run_scenario(bot, world, scenario):
    import gateway
    started = len(world.discord), len(world.model_calls), len(world.jev_calls)
    ok_before = tool_ok_calls(scenario["tool"]) if scenario.get("tool") else 0
    message = FakeMessage(world, scenario["request"])
    clock = time.monotonic()
    asyncio.run(bot.on_message(message))
    # Wait for the job the handler submitted (text and video workers are single-threaded executors).
    for pool in (bot.text_responder, bot.renderer):
        pool.submit(lambda: None).result(timeout=float(os.environ.get("E2E_TIMEOUT") or 600))
    import router
    router.wait_idle(120)
    elapsed = time.monotonic() - clock
    decisions = [d for d in router.backlog().recent(50) if d["id"] > scenario.get("_after_decision", 0)]
    discord = [d for d in world.discord[started[0]:] if d["channel"] == str(CHANNEL_ID)]
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
        check("no failure message", final and not final["content"].lstrip().startswith("⚠️")
              and not any(f.split("\n")[0] in final["content"] for f in failures), final["content"][:200] if final else "")
        if scenario.get("expect") == "text":
            check("text request uploaded no video", not any(d["attachment_bytes"] for d in discord), "an attachment was uploaded")
            if world.hermetic:
                phases = [c["phase"] for c in world.model_calls[started[1]:]]
                check("text answer came from the text-answer stage", "text_answer" in phases, f"phases: {phases}")
                check("the request date reached the model", final and "НЕ ПЕРЕДАНА" not in final["content"],
                      "relative dates would be computed from the model's training data")
    if scenario.get("tool"):
        tool_id, answer = scenario["tool"], final["content"] if final else ""
        ok_after = tool_ok_calls(tool_id)
        check(f"{tool_id} ran in the tool executor during research", ok_after > ok_before, f"ok calls {ok_before} -> {ok_after}")
        if scenario.get("days_to_new_year"):
            asked = message.created_at.date()
            days = (asked.replace(year=asked.year + 1, month=1, day=1) - asked).days
            check(f"the answer contains the computed {days} days", re.search(rf"(?<!\d){days}(?!\d)", answer), answer[-300:])
        check("the new (probation) tool is disclosed under the answer", re.search(rf"(?m)^-# 🧪 .*\b{tool_id}\b", answer),
              answer[-300:])
        if world.hermetic:
            research = [json.loads(c["body"]) for c in world.model_calls[started[1]:] if c["phase"] == "research"]
            offered = [t for t in (research[0].get("tools") or [] if research else []) if t.get("name") == tool_id]
            check(f"research offered {tool_id} with a schema the Messages API accepts", offered and not re.search(
                r'"(?:minLength|maxLength|minimum|maximum|minItems|maxItems|default)"', json.dumps(offered[0]["input_schema"])),
                  json.dumps(offered)[:300])
            returned = [json.loads(b["content"]) for body in research for m in body["messages"] if isinstance(m["content"], list)
                        for b in m["content"] if b.get("type") == "tool_result"]
            check("the tool output went back to the research model", returned and all(r.get("outcome") == "ok" for r in returned),
                  str(returned)[:300])
    if scenario.get("expect") != "help":
        check("router recorded one shadow decision", len(decisions) == 1, f"{len(decisions)} decisions")
        if decisions and scenario.get("route"):
            verdict, _, tool = scenario["route"].partition(":")
            got = decisions[0]
            check(f"router verdict is {scenario['route']}",
                  got["verdict"] == verdict and (not tool or tool in json.loads(got["tools"])),
                  f"got {got['verdict']} {got['tools']} via {got['source']}")
            check("request text stored only for gaps", (got["request"] is not None) == (got["verdict"] in ("gap", "impossible", "clarify")),
                  f"verdict {got['verdict']} stored={got['request'] is not None}")
            paths = [urllib.parse.urlsplit(u).path for u in re.findall(r"https?://\S+", scenario["request"])]
            routed = [c["body"] for c in world.model_calls[started[1]:] if c["phase"] == "router"]
            routed += [c["body"] for c in world.jev_calls[started[2]:]]
            check("routing saw link hosts, not link paths", not any(p and p in body for p in paths for body in routed),
                  f"paths {paths} reached the router")
            if got["verdict"] == "gap":
                check("gap has a reusable specification", got["capability"] and json.loads(got["gap"] or "{}").get("summary"),
                      str(got["gap"])[:200])
    return {"scenario": scenario, "seconds": round(elapsed, 2), "status": status, "edits": edits, "final": final,
            "decisions": decisions,
            "extra_parts": extra_parts, "model_calls": world.model_calls[started[1]:],
            "jev_calls": world.jev_calls[started[2]:], "checks": checks}


def learning_loop(bot, world, capture):
    """The bot meets a capability it lacks twice, the owner approves the forge, the reviewed tool is merged
    (`git pull` on the server), and the same request is then covered by the new tool."""
    import forge_queue
    import interactions as I
    import router
    import tool_registry
    import tool_runtime
    print("\n━━━━ self-learning loop: a capability the bot lacks")
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print(f"   {'✓' if ok else '✗'} {name}" + ("" if ok else f" — {detail}"))

    def scenario(text, route):
        mark = len(capture.records)
        item = {"request": text, "expect": "text", "route": route,
                "_after_decision": max([d["id"] for d in router.backlog().recent(1)] or [0])}
        result = run_scenario(bot, world, item)
        show(result, capture.records[mark:])
        checks.extend(result["checks"])
        return result

    first = len(world.discord)
    for text in LOOP_REQUESTS:
        scenario(text, "gap")
    cards = [d for d in world.discord[first:] if d["channel"] == str(DM_CHANNEL_ID) and d["op"] == "post"]
    print("\n   owner DM card:\n      " + (cards[0]["content"].replace("\n", "\n      ") if cards else "(none)"))
    check("the owner got one card after the second request", len(cards) == 1 and world.dms[-1:] == [str(OWNER_ID)],
          f"{len(cards)} cards, DMs {world.dms}")
    buttons = [b["custom_id"] for row in (cards[0]["components"] if cards else []) for b in row["components"]]
    check("the card offers Build and Ignore", buttons == ["forge:length_convert:build", "forge:length_convert:ignore"], buttons)
    check("the card shows the specification, not users' requests",
          cards and not any(text in cards[0]["content"] for text in LOOP_REQUESTS), "")

    stranger = I.handle({"type": I.MESSAGE_COMPONENT, "user": {"id": str(USER_ID)},
                         "data": {"custom_id": "forge:length_convert:build"}}, lambda job: True)[1]
    check("only the owner can approve", stranger["data"]["content"] == forge_queue.strings()["owner_only"], stranger)
    approved = I.handle({"type": I.MESSAGE_COMPONENT, "user": {"id": str(OWNER_ID)},
                         "data": {"custom_id": "forge:length_convert:build"}}, lambda job: True)[1]
    print("   owner clicked Build → " + approved["data"]["content"].splitlines()[-1])
    jobs = [j for j in Path(os.environ["FORGE_QUEUE"]).iterdir() if (j / "request.json").is_file()]
    request = json.loads((jobs[0] / "request.json").read_text()) if jobs else {}
    check("one forge job with the specification only", len(jobs) == 1 and request.get("capability") == "length_convert"
          and not any(text in json.dumps(request, ensure_ascii=False) for text in LOOP_REQUESTS), request)

    # scripts/tool_forge.py builds, gates and opens the pull request (scripts/test_tool_forge.py covers it with a fake
    # builder). Here the reviewed result is merged and pulled: the fixture is what the forge produced.
    library = Path(os.environ["TOOLS_DIR"])
    shutil.copytree(FIXTURES / "length_convert", library / "length_convert")
    url = "https://github.com/PyonPyonMF/explain-bot/pull/1"
    if jobs:
        (jobs[0] / "response.json").write_text(json.dumps({"status": "pr_opened", "pr_url": url, "rounds": 2,
                                                            "tests": {"own": {"passed": 6, "total": 6}}}))
    forge_queue.collect()
    edits = [d for d in world.discord if d["channel"] == str(DM_CHANNEL_ID) and d["op"] == "edit"]
    check("the card links the pull request", edits and url in edits[-1]["content"], edits[-1]["content"] if edits else "")
    registry = tool_registry.registry()
    registry.sync(force=True)
    tool = registry.get("length_convert")
    report = tool_runtime.run_tests(tool) if tool else {"passed": 0, "total": 0}
    print(f"   merged + git pull → length_convert[{tool['status'] if tool else 'missing'}]: "
          f"{report['passed']}/{report['total']} declarative tests in the executor")
    check("the merged tool is live without a restart", tool and report["passed"] == report["total"] > 0, report)
    answered = scenario(LOOP_REQUESTS[0], "covered:length_convert")
    final = (answered.get("final") or {}).get("content", "")
    check("the bot answered the same request with the new tool", "value=3.048" in final, final[-300:])
    check("the answer discloses the new (probation) tool", "length_convert" in final and "🧪" in final, final[-200:])
    return {"checks": checks}


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
    for job_line in [r for r in records if r.name in ("router", "research", "tools", "jev_usage", "claude_usage")]:
        print(f"   log  {job_line.name}: {job_line.getMessage()[:220]}")
    for call in result["model_calls"]:
        print(f"   model {call['phase']:<12} tools={call['tools'] or '-'} request={call['chars']} chars"
              + (f" -> tool_use {call['calls']}" if call.get("calls") else ""))
    for call in result["jev_calls"]:
        print(f"   jev   questions={call['questions']} request={call['chars']} chars")
    for decision in result.get("decisions", []):
        jev = json.loads(decision["jev"]) if decision["jev"] else None
        print(f"   router {decision['verdict']} via {decision['source']} confidence={decision['confidence']:.2f} "
              f"tools={json.loads(decision['tools']) or '-'}" + (f" capability={decision['capability']}" if decision["capability"] else "")
              + (f" | jev: {jev['choice']} p={jev['confidence']:.2f} exact={jev['exact']}" if jev else ""))
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
    library_copy(data_dir)
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
        import router
        scenario["_after_decision"] = max([d["id"] for d in router.backlog().recent(1)] or [0])
        result = run_scenario(bot, world, scenario)
        results.append(result)
        show(result, capture.records[mark:])
        ok = ok and all(passed for _, passed, _ in result["checks"])
    if not args.live and not args.requests:
        loop = learning_loop(bot, world, capture)
        results.append(loop)
        ok = ok and all(passed for _, passed, _ in loop["checks"])
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
