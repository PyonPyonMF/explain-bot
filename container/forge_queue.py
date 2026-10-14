"""Bot side of the tool forge: offer frequently requested missing capabilities to the owner, queue approved ones.

When the router records a gap whose capability was requested at least FORGE_MIN_REQUESTS times in 30 days, the bot
sends FORGE_OWNER_ID a direct message with the specification and Build / Ignore buttons. Build writes the
specification (never the users' requests) to FORGE_QUEUE for the host-side forge (scripts/tool_forge.py), which
opens a pull request; the card then shows the result. A merged tool appears after `git pull` (see Tool library).

  python -m forge_queue list                 proposals and their state
  python -m forge_queue propose <capability> queue the latest specification without the Discord card
  python -m forge_queue collect              read finished forge results
"""
import argparse
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid

log = logging.getLogger("forge")
STATES = ("offered", "queued", "pr_opened", "rejected", "failed", "ignored")
STRINGS = {
    "ru": {"title": "🔧 Боту не хватает возможности **{capability}** — просили {requests} раз(а) за 30 дней.",
           "build": "Собрать инструмент", "ignore": "Игнорировать", "queued": "🔨 Отправлено в кузницу. Пришлю ссылку на PR.",
           "ignored": "🙈 Пропущено.", "pr_opened": "✅ Инструмент собран и прошёл проверки. PR для ревью: {url}",
           "rejected": "❌ Кузница не приняла результат: {reason}", "failed": "⚠️ Кузница не смогла собрать инструмент: {reason}",
           "owner_only": "Решать может только владелец бота.", "unknown": "Это предложение больше не актуально."},
    "en": {"title": "🔧 The bot lacks **{capability}** — requested {requests} time(s) in 30 days.",
           "build": "Build the tool", "ignore": "Ignore", "queued": "🔨 Sent to the forge. I'll send the pull request link.",
           "ignored": "🙈 Ignored.", "pr_opened": "✅ The tool was built and passed the gates. Pull request: {url}",
           "rejected": "❌ The forge rejected the result: {reason}", "failed": "⚠️ The forge could not build the tool: {reason}",
           "owner_only": "Only the bot owner can decide.", "unknown": "This proposal is no longer current."},
}


def strings():
    return STRINGS.get(os.environ.get("BOT_LANG") or "ru", STRINGS["en"])


def queue_dir():
    path = os.environ.get("FORGE_QUEUE")
    return Path(path) if path and Path(path).is_dir() else None


def enabled():
    return bool(os.environ.get("FORGE_OWNER_ID") and queue_dir())


def _db():
    import router
    conn = router.backlog()._db()
    conn.execute("""CREATE TABLE IF NOT EXISTS proposals (capability TEXT PRIMARY KEY, state TEXT NOT NULL, spec TEXT NOT NULL,
        requests INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, queue_id TEXT, channel_id TEXT,
        message_id TEXT, result TEXT)""")
    return conn


def _discord(method, path, payload):
    import pipeline as P
    headers = {"authorization": "Bot " + os.environ["DISCORD_BOT_TOKEN"], "content-type": "application/json",
               "user-agent": "DiscordBot (explain-bot, 1.0)"}
    _, raw = P._retry(lambda: P._http(P.DISCORD_BASE + path, json.dumps(payload).encode(), headers, method=method, timeout=15),
                      "forge card", attempts=2)
    return json.loads(raw or b"{}")


def card(capability, spec, requests, state="offered", result=None):
    S = strings()
    lines = [S["title"].format(capability=capability, requests=requests), "> " + (spec.get("summary") or "")[:400]]
    for label, key in (("Вход" if os.environ.get("BOT_LANG", "ru") == "ru" else "Input", "inputs"),
                       ("Выход" if os.environ.get("BOT_LANG", "ru") == "ru" else "Output", "outputs")):
        if spec.get(key):
            lines.append(f"**{label}:** {spec[key][:300]}")
    if spec.get("network_hosts"):
        lines.append("**Сеть / Network:** " + ", ".join(spec["network_hosts"]))
    if spec.get("example_requests"):
        lines.append("**Примеры / Examples:** " + "; ".join(f"«{e[:120]}»" for e in spec["example_requests"][:3]))
    if state != "offered":
        result = result or {}
        lines.append(S[state].format(url=result.get("pr_url", ""), reason=str(result.get("reason") or "")[:300]))
    data = {"content": "\n".join(lines)[:1900], "allowed_mentions": {"parse": []}, "components": []}
    if state == "offered":
        data["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": S["build"], "custom_id": f"forge:{capability}:build"},
            {"type": 2, "style": 2, "label": S["ignore"], "custom_id": f"forge:{capability}:ignore"}]}]
    return data


def consider(decision):
    """After a gap decision: offer the capability to the owner once it has enough demand."""
    gap = decision.get("gap")
    if decision.get("verdict") != "gap" or not gap or not enabled():
        return False
    import router
    capability = gap["capability"]
    demand = next((g["requests"] for g in router.backlog().gaps() if g["capability"] == capability), 0)
    if demand < int(os.environ.get("FORGE_MIN_REQUESTS") or 2) or not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", capability):
        return False
    try:
        import tool_registry
        if tool_registry.registry().get(capability, include_inactive=True):
            return False
    except Exception:
        pass
    conn = _db()
    try:
        if conn.execute("SELECT 1 FROM proposals WHERE capability=?", (capability,)).fetchone():
            return False
        now = time.time()
        conn.execute("INSERT INTO proposals (capability, state, spec, requests, created, updated) VALUES (?, 'offered', ?, ?, ?, ?)",
                     (capability, json.dumps(gap, ensure_ascii=False), demand, now, now))
        conn.commit()
    finally:
        conn.close()
    try:
        channel = _discord("POST", "/users/@me/channels", {"recipient_id": str(os.environ["FORGE_OWNER_ID"])})["id"]
        message = _discord("POST", f"/channels/{channel}/messages", card(capability, gap, demand))["id"]
        _update(capability, channel_id=str(channel), message_id=str(message))
        log.info("forge: offered %s to the owner (%d requests)", capability, demand)
        return True
    except Exception as exc:
        log.warning("forge: could not send the proposal: %s", type(exc).__name__)
        return False


def _update(capability, **fields):
    conn = _db()
    try:
        assignments = ", ".join(f"{key}=?" for key in fields)
        conn.execute(f"UPDATE proposals SET {assignments}, updated=? WHERE capability=?", (*fields.values(), time.time(), capability))
        conn.commit()
    finally:
        conn.close()


def _proposal(capability):
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM proposals WHERE capability=?", (capability,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def enqueue(capability):
    """Write the approved specification to the forge queue; returns the job id."""
    proposal, queue = _proposal(capability), queue_dir()
    if not proposal or not queue:
        raise ValueError("unknown proposal or no forge queue")
    import tool_registry
    work = queue / uuid.uuid4().hex
    work.mkdir(mode=0o700)
    uid = int(os.environ.get("FORGE_UID") or 1000)
    if os.getuid() == 0:
        os.chown(work, uid, int(os.environ.get("FORGE_GID") or uid))
    request = {"api": 1, "capability": capability, "spec": json.loads(proposal["spec"]), "requests": proposal["requests"],
               "existing_tools": [t["id"] for t in tool_registry.registry().tools(include_inactive=True)]}
    temp = work / "request.tmp"
    temp.write_text(json.dumps(request, ensure_ascii=False))
    os.chmod(temp, 0o644)
    temp.replace(work / "request.json")
    _update(capability, state="queued", queue_id=work.name)
    log.info("forge: queued %s as %s", capability, work.name)
    return work.name


def handle(interaction):
    """Component click on a proposal card: owner-only; returns an interaction response (UPDATE_MESSAGE)."""
    import interactions as I
    S = strings()
    data = interaction.get("data") or {}
    match = re.fullmatch(r"forge:([a-z][a-z0-9_]{2,39}):(build|ignore)", str(data.get("custom_id") or ""))
    actor = ((interaction.get("member") or {}).get("user") or interaction.get("user") or {}).get("id")
    if str(actor) != str(os.environ.get("FORGE_OWNER_ID") or "-"):
        return 200, {"type": I.CHANNEL_MESSAGE, "data": {"content": S["owner_only"], "flags": I.EPHEMERAL}}
    proposal = _proposal(match[1]) if match else None
    if not proposal or proposal["state"] != "offered":
        return 200, {"type": I.CHANNEL_MESSAGE, "data": {"content": S["unknown"], "flags": I.EPHEMERAL}}
    spec = json.loads(proposal["spec"])
    if match[2] == "ignore":
        _update(match[1], state="ignored")
        return 200, {"type": I.UPDATE_MESSAGE, "data": card(match[1], spec, proposal["requests"], "ignored")}
    try:
        enqueue(match[1])
    except Exception as exc:
        log.warning("forge: could not queue %s: %s", match[1], type(exc).__name__)
        return 200, {"type": I.UPDATE_MESSAGE, "data": card(match[1], spec, proposal["requests"], "failed", {"reason": "queue"})}
    return 200, {"type": I.UPDATE_MESSAGE, "data": card(match[1], spec, proposal["requests"], "queued")}


def collect():
    """Read finished forge results and update the owner's card. Returns the number of finished proposals."""
    queue = queue_dir()
    if not queue:
        return 0
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM proposals WHERE state='queued'")]
    finally:
        conn.close()
    finished = 0
    for proposal in rows:
        response = queue / str(proposal["queue_id"]) / "response.json"
        if not response.is_file():
            continue
        try:
            result = json.loads(response.read_text())
        except (OSError, ValueError):
            continue
        state = result.get("status") if result.get("status") in ("pr_opened", "rejected", "failed") else "failed"
        url = str(result.get("pr_url") or "")
        if state == "pr_opened" and not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", url):
            state, result = "failed", {"reason": "invalid pull request URL"}
        _update(proposal["capability"], state=state, result=json.dumps(result, ensure_ascii=False)[:4000])
        finished += 1
        log.info("forge: %s -> %s", proposal["capability"], state)
        if proposal["channel_id"] and proposal["message_id"]:
            try:
                _discord("PATCH", f"/channels/{proposal['channel_id']}/messages/{proposal['message_id']}",
                         card(proposal["capability"], json.loads(proposal["spec"]), proposal["requests"], state, result))
            except Exception as exc:
                log.warning("forge: could not update the card: %s", type(exc).__name__)
    return finished


def main(argv=None):
    parser = argparse.ArgumentParser(prog="forge_queue")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("collect")
    sub.add_parser("propose").add_argument("capability")
    args = parser.parse_args(argv)
    if args.command == "list":
        conn = _db()
        try:
            for row in conn.execute("SELECT capability, state, requests, queue_id, result FROM proposals ORDER BY updated DESC"):
                print(json.dumps(dict(row), ensure_ascii=False))
        finally:
            conn.close()
    elif args.command == "collect":
        print(json.dumps({"finished": collect()}))
    else:
        import router
        gap = next((g for g in router.backlog().gaps() if g["capability"] == args.capability), None)
        if not gap or not gap["spec"]:
            print(json.dumps({"error": f"no gap {args.capability!r} in the backlog"}))
            return 1
        if not _proposal(args.capability):
            conn = _db()
            try:
                now = time.time()
                conn.execute("INSERT INTO proposals (capability, state, spec, requests, created, updated) VALUES (?, 'offered', ?, ?, ?, ?)",
                             (args.capability, json.dumps(gap["spec"], ensure_ascii=False), gap["requests"], now, now))
                conn.commit()
            finally:
                conn.close()
        print(json.dumps({"queued": enqueue(args.capability)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
