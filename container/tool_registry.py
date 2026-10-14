"""Hot-loaded tool library. The trusted bot only reads manifests and hashes files; tool code runs in tool_harness.py.

A tool is a directory TOOLS_DIR/<id>/ with manifest.json, tool.py (def run(args, ctx)) and tests.json.
Each distinct content digest is a version. Lifecycle state and usage statistics live in DATA_DIR/tools.sqlite,
so a merged tool appears without a rebuild or restart, and a changed tool never inherits its predecessor's record.
"""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

log = logging.getLogger("tools")
API = 1
ID_RE = re.compile(r"[a-z][a-z0-9_]{2,39}")
HOST_RE = re.compile(r"(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
FILE_RE = re.compile(r"[a-z0-9_]{1,40}\.(?:py|json|txt)")
MAX_TOOL_BYTES = 1_000_000
STATUSES = ("probation", "stable", "suspended", "archived", "superseded")
ACTIVE = ("probation", "stable")
LIMITS = {"timeout_s": (1, 60, 20), "max_fetches": (0, 20, 0), "max_fetch_kb": (1, 2048, 512),
          "max_output_chars": (100, 20000, 8000), "memory_mb": (64, 1024, 512)}
MANIFEST_KEYS = {"api", "id", "title", "description", "when_not_to_use", "examples", "input_schema",
                 "output_description", "network", "limits"}
SCHEMA_KEYS = {"type", "description", "properties", "required", "additionalProperties", "enum", "default",
               "minLength", "maxLength", "minimum", "maximum", "items", "minItems", "maxItems"}


class ManifestError(ValueError):
    pass


def tools_dir():
    return Path(os.environ.get("TOOLS_DIR") or "/tools")


# ------------------------------------------------------------ schemas
def check_schema(schema, path="input_schema", depth=0):
    """Accept a bounded JSON Schema subset that is also valid for strict Claude tool definitions.

    No `pattern`: an untrusted regular expression evaluated by the trusted process is a ReDoS risk.
    """
    if depth > 4 or not isinstance(schema, dict):
        raise ManifestError(f"{path}: schema must be an object, at most 4 levels deep")
    if set(schema) - SCHEMA_KEYS:
        raise ManifestError(f"{path}: unsupported keywords {sorted(set(schema) - SCHEMA_KEYS)}")
    kind = schema.get("type")
    if kind not in ("object", "string", "integer", "number", "boolean", "array"):
        raise ManifestError(f"{path}: unsupported type {kind!r}")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not 0 < len(schema["enum"]) <= 50):
        raise ManifestError(f"{path}: enum must be a list of 1-50 values")
    if kind == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not 0 < len(properties) <= 20 or schema.get("additionalProperties") is not False:
            raise ManifestError(f"{path}: objects need 1-20 properties and additionalProperties: false")
        required = schema.get("required", [])
        if not isinstance(required, list) or set(required) - set(properties):
            raise ManifestError(f"{path}: required must list declared properties")
        for name, sub in properties.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
                raise ManifestError(f"{path}: invalid property name {name!r}")
            check_schema(sub, f"{path}.{name}", depth + 1)
    elif kind == "string" and "enum" not in schema and not (isinstance(schema.get("maxLength"), int) and 0 < schema["maxLength"] <= 4000):
        raise ManifestError(f"{path}: strings need maxLength (1-4000) or enum")
    elif kind == "array":
        if not (isinstance(schema.get("maxItems"), int) and 0 < schema["maxItems"] <= 50):
            raise ManifestError(f"{path}: arrays need maxItems (1-50)")
        check_schema(schema.get("items"), f"{path}[]", depth + 1)
    elif kind in ("integer", "number"):
        for bound in ("minimum", "maximum"):
            if bound in schema and (isinstance(schema[bound], bool) or not isinstance(schema[bound], (int, float))):
                raise ManifestError(f"{path}: {bound} must be a number")


def validate(schema, value, path="args"):
    """Validate tool arguments against a checked schema. Returns a list of problems."""
    kind = schema.get("type")
    ok = {"object": lambda v: isinstance(v, dict), "string": lambda v: isinstance(v, str),
          "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
          "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
          "boolean": lambda v: isinstance(v, bool), "array": lambda v: isinstance(v, list)}[kind]
    if not ok(value):
        return [f"{path}: expected {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: must be one of {schema['enum']}"]
    problems = []
    if kind == "object":
        properties = schema["properties"]
        problems += [f"{path}.{key}: unexpected property" for key in value if key not in properties]
        problems += [f"{path}.{key}: required" for key in schema.get("required", []) if key not in value]
        for key, item in value.items():
            if key in properties:
                problems += validate(properties[key], item, f"{path}.{key}")
    elif kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 4000):
            problems.append(f"{path}: length must be {schema.get('minLength', 0)}-{schema.get('maxLength', 4000)}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema["maxItems"]:
            problems.append(f"{path}: must have {schema.get('minItems', 0)}-{schema['maxItems']} items")
        for index, item in enumerate(value):
            problems += validate(schema["items"], item, f"{path}[{index}]")
    elif kind in ("integer", "number"):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            problems.append(f"{path}: must be within {schema.get('minimum')}..{schema.get('maximum')}")
    return problems[:10]


# ----------------------------------------------------------- manifests
def _text(manifest, key, low, high, required=True):
    value = manifest.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise ManifestError(f"{key} must be a string of {low}-{high} characters")
    return value.strip()


def check_manifest(manifest, directory_name=None):
    """Return a normalized manifest or raise ManifestError. Nothing here executes tool code."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")
    if set(manifest) - MANIFEST_KEYS:
        raise ManifestError(f"unknown manifest keys {sorted(set(manifest) - MANIFEST_KEYS)}")
    if manifest.get("api") != API:
        raise ManifestError(f"api must be {API}")
    tool_id = manifest.get("id")
    if not isinstance(tool_id, str) or not ID_RE.fullmatch(tool_id) or (directory_name and tool_id != directory_name):
        raise ManifestError("id must match [a-z][a-z0-9_]{2,39} and the directory name")
    examples = manifest.get("examples")
    if (not isinstance(examples, list) or not 1 <= len(examples) <= 8
            or not all(isinstance(e, str) and 0 < len(e.strip()) <= 200 for e in examples)):
        raise ManifestError("examples must be 1-8 example user requests of at most 200 characters")
    schema = manifest.get("input_schema")
    check_schema(schema)
    if schema["type"] != "object":
        raise ManifestError("input_schema must describe an object")
    network = manifest.get("network", [])
    if not isinstance(network, list) or len(network) > 8 or not all(isinstance(h, str) and HOST_RE.fullmatch(h) for h in network):
        raise ManifestError("network must list at most 8 lowercase hostnames (optionally *.domain)")
    limits = manifest.get("limits", {})
    if not isinstance(limits, dict) or set(limits) - set(LIMITS):
        raise ManifestError(f"limits may only set {sorted(LIMITS)}")
    resolved = {}
    for key, (low, high, default) in LIMITS.items():
        value = limits.get(key, 5 if key == "max_fetches" and network else default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ManifestError(f"limits.{key} must be an integer {low}-{high}")
        resolved[key] = value
    if resolved["max_fetches"] and not network:
        raise ManifestError("max_fetches requires network hosts")
    return {"api": API, "id": tool_id, "title": _text(manifest, "title", 3, 80),
            "description": _text(manifest, "description", 40, 1200),
            "when_not_to_use": _text(manifest, "when_not_to_use", 0, 600, required=False),
            "examples": [e.strip() for e in examples], "input_schema": schema,
            "output_description": _text(manifest, "output_description", 10, 600),
            "network": sorted(set(network)), "limits": resolved}


def read_tool(directory):
    """Read one tool directory into memory: (manifest, files, digest). Rejects links and oversized trees."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ManifestError("tool must be a real directory")
    files, total = {}, 0
    for path in sorted(directory.iterdir()):
        if path.name == "__pycache__":
            continue
        if path.is_symlink() or not path.is_file() or not FILE_RE.fullmatch(path.name):
            raise ManifestError(f"unexpected entry {path.name!r}")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_TOOL_BYTES:
            raise ManifestError("tool directory exceeds 1 MB")
        files[path.name] = data.decode("utf-8")
    if "manifest.json" not in files or "tool.py" not in files:
        raise ManifestError("manifest.json and tool.py are required")
    try:
        manifest = check_manifest(json.loads(files["manifest.json"]), directory.name)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest.json is not JSON: {exc.msg}") from None
    digest = hashlib.sha256()
    for name, text in files.items():
        digest.update(name.encode() + b"\0" + text.encode() + b"\0")
    return manifest, files, digest.hexdigest()


def tests_of(files):
    """Declarative test cases (tests.json). They are data checked by the trusted process, never code."""
    if "tests.json" not in files:
        return []
    cases = json.loads(files["tests.json"])
    if not isinstance(cases, list) or len(cases) > 40:
        raise ManifestError("tests.json must be a list of at most 40 cases")
    for case in cases:
        if not isinstance(case, dict) or set(case) - {"name", "args", "fixtures", "expect", "expect_contains", "expect_error"}:
            raise ManifestError("test cases accept name, args, fixtures, expect, expect_contains, expect_error")
        if not isinstance(case.get("name"), str) or not isinstance(case.get("args"), dict):
            raise ManifestError("each test case needs a name and args")
    return cases


# ------------------------------------------------------------ registry
class Registry:
    """Tool versions found on disk plus their lifecycle state. Rescans at most every TOOLS_RESCAN seconds."""

    def __init__(self, root=None, db_path=None):
        self.root = Path(root) if root else tools_dir()
        data = os.environ.get("DATA_DIR") or "/data"
        self.db_path = Path(db_path) if db_path else Path(data) / "tools.sqlite"
        self.lock = threading.Lock()
        self.scanned, self.current, self.errors = 0.0, {}, {}

    def _db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS tool_versions (
            digest TEXT PRIMARY KEY, id TEXT NOT NULL, manifest TEXT NOT NULL, discovered REAL NOT NULL,
            status TEXT NOT NULL, uses INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0,
            declined INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
            last_used REAL, last_error TEXT, status_changed REAL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0)""")
        if "consecutive_failures" not in {row[1] for row in conn.execute("PRAGMA table_info(tool_versions)")}:
            conn.execute("ALTER TABLE tool_versions ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        if os.name == "posix":
            os.chmod(self.db_path, 0o600)  # Usage history is bot state, not readable by sandboxed code.
        return conn

    def sync(self, force=False):
        """Register new on-disk versions; a changed tool supersedes its previous version."""
        with self.lock:
            if not force and time.monotonic() - self.scanned < float(os.environ.get("TOOLS_RESCAN") or 30):
                return
            current, errors = {}, {}
            if self.root.is_dir():
                for directory in sorted(self.root.iterdir()):
                    if directory.name.startswith((".", "_")) or not directory.is_dir():
                        continue
                    try:
                        manifest, files, digest = read_tool(directory)
                        current[manifest["id"]] = {"manifest": manifest, "files": files, "digest": digest}
                    except (ManifestError, OSError, UnicodeDecodeError) as exc:
                        errors[directory.name] = str(exc)[:300]
            conn = self._db()
            try:
                now = time.time()
                for tool_id, entry in current.items():
                    if not conn.execute("SELECT 1 FROM tool_versions WHERE digest=?", (entry["digest"],)).fetchone():
                        conn.execute("UPDATE tool_versions SET status='superseded', status_changed=? WHERE id=? AND status IN "
                                     "('probation','stable','suspended')", (now, tool_id))
                        conn.execute("INSERT INTO tool_versions (digest, id, manifest, discovered, status, status_changed) "
                                     "VALUES (?, ?, ?, ?, 'probation', ?)",
                                     (entry["digest"], tool_id, json.dumps(entry["manifest"], ensure_ascii=False), now, now))
                conn.commit()
            finally:
                conn.close()
            self.current, self.errors, self.scanned = current, errors, time.monotonic()

    def _rows(self):
        conn = self._db()
        try:
            return {row["digest"]: dict(row) for row in conn.execute("SELECT * FROM tool_versions")}
        finally:
            conn.close()

    def tools(self, include_inactive=False):
        """Current on-disk versions with their state; by default only probation/stable ones."""
        self.sync()
        rows = self._rows()
        out = []
        for tool_id, entry in sorted(self.current.items()):
            row = rows.get(entry["digest"])
            if row and (include_inactive or row["status"] in ACTIVE):
                out.append({**row, "manifest": entry["manifest"], "files": entry["files"]})
        return out

    def get(self, tool_id, include_inactive=False):
        return next((t for t in self.tools(include_inactive) if t["id"] == tool_id), None)

    def set_status(self, tool_id, status):
        if status not in STATUSES:
            raise ValueError("unknown status")
        self.sync(force=True)
        entry = self.current.get(tool_id)
        if not entry:
            return False
        conn = self._db()
        try:
            # An operator's decision starts a new failure streak; the totals stay as the version's record.
            cursor = conn.execute("UPDATE tool_versions SET status=?, status_changed=?, consecutive_failures=0 WHERE digest=?",
                                  (status, time.time(), entry["digest"]))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def record(self, digest, outcome, error=""):
        """outcome: ok (answered), declined (the tool reported an expected failure), failed (crash/timeout/violation).

        The lifecycle policy is applied to this version in the same transaction; returns its new status or None.
        """
        if outcome not in ("ok", "declined", "failed"):
            raise ValueError("unknown outcome")
        conn = self._db()
        change = None
        try:
            conn.execute(f"UPDATE tool_versions SET uses=uses+1, {outcome}={outcome}+1, last_used=?, "
                         "last_error=CASE WHEN ?='failed' THEN ? ELSE last_error END, "
                         "consecutive_failures=CASE WHEN ?='failed' THEN consecutive_failures+1 ELSE 0 END WHERE digest=?",
                         (time.time(), outcome, str(error)[:300], outcome, digest))
            row = conn.execute("SELECT * FROM tool_versions WHERE digest=?", (digest,)).fetchone()
            change = lifecycle(dict(row), outcome) if row else None
            if change:
                conn.execute("UPDATE tool_versions SET status=?, status_changed=? WHERE digest=?", (change[0], time.time(), digest))
            conn.commit()
        finally:
            conn.close()
        if not change:
            return None
        (log.warning if change[0] == "suspended" else log.info)("tool %s %s %s", row["id"], digest[:12], change[1])
        return change[0]


def lifecycle(row, outcome):
    """(status, reason) a version earns with this call, or None. Only a failure can suspend and only a success can
    promote, so an operator's restore is not undone by the next good call."""
    if row["status"] not in ACTIVE:
        return None
    uses, failed = row["uses"], row["failed"]
    if outcome == "failed":
        streak = int(os.environ.get("TOOLS_SUSPEND_AFTER_FAILURES") or 3)
        if streak and row["consecutive_failures"] >= streak:
            return "suspended", f"suspended after {row['consecutive_failures']} consecutive failures"
        if uses >= 5 and failed > 0.3 * uses:
            return "suspended", f"suspended after {failed} of {uses} calls failed"
    elif row["status"] == "probation":
        promote_after = int(os.environ.get("TOOLS_PROMOTE_AFTER") or 20)
        if promote_after and uses >= promote_after and row["ok"] + row["declined"] >= 0.95 * uses:
            return "stable", f"promoted to stable after {uses} calls"
    return None


_registry = None


def registry():
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry


def summary(tool):
    return {key: tool[key] for key in ("id", "status", "uses", "ok", "declined", "failed", "last_error")} | {
        "digest": tool["digest"][:12], "title": tool["manifest"]["title"], "network": tool["manifest"]["network"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tool_registry")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for command in ("show", "archive", "restore", "promote", "suspend", "test"):
        sub.add_parser(command).add_argument("id")
    run = sub.add_parser("run")
    run.add_argument("id")
    run.add_argument("args", help="JSON object")
    check = sub.add_parser("check", help="validate tool directories without a database")
    check.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "check":
        failed = False
        for path in args.paths:
            try:
                manifest, files, digest = read_tool(path)
                tests_of(files)
                print(json.dumps({"path": path, "id": manifest["id"], "digest": digest[:12], "ok": True}))
            except (ManifestError, OSError, ValueError) as exc:
                failed = True
                print(json.dumps({"path": path, "ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1 if failed else 0
    reg = registry()
    reg.sync(force=True)
    if args.command == "list":
        for tool in reg.tools(include_inactive=True):
            print(json.dumps(summary(tool), ensure_ascii=False))
        for name, error in reg.errors.items():
            print(json.dumps({"id": name, "status": "invalid", "error": error}, ensure_ascii=False))
        return 0
    tool = reg.get(args.id, include_inactive=True)
    if not tool:
        print(json.dumps({"error": f"no tool {args.id!r}", "invalid": reg.errors.get(args.id)}, ensure_ascii=False))
        return 1
    if args.command == "show":
        print(json.dumps(summary(tool) | {"manifest": tool["manifest"]}, ensure_ascii=False, indent=2))
    elif args.command in ("archive", "restore", "promote", "suspend"):
        status = {"archive": "archived", "restore": "probation", "promote": "stable", "suspend": "suspended"}[args.command]
        print(json.dumps({"id": tool["id"], "status": status, "updated": reg.set_status(tool["id"], status)}))
    elif args.command == "test":
        import tool_runtime
        report = tool_runtime.run_tests(tool)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] == report["total"] else 1
    elif args.command == "run":
        import tool_runtime
        print(json.dumps(tool_runtime.call(tool, json.loads(args.args), record=False), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
