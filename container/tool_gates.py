"""Acceptance gates for a tool directory, applied before a generated tool may be proposed for merge.

    python -m tool_gates tools/<id> [--spec spec.json] [--hidden-tests hidden.json] [--existing tools/]

The static checks are lint-level defense in depth (the sandbox and the broker are the security boundary); they keep
generated tools to the documented contract so that reviewers read small, plain functions:
  1. manifest and directory contract (tool_registry.read_tool) and declarative tests (tests.json)
  2. code: only allow-listed imports; no open/exec/eval/compile/__import__/dunder attributes; run(args, ctx) defined
  3. scope: network hosts within the gap specification; a new id that does not shadow an existing tool
  4. tests: enough cases including an expected failure; all cases pass in the sandbox; hidden tests written by a
     separate author from the specification also pass
Prints a JSON report; exit status 0 only when every gate passes.
"""
import argparse
import ast
import json
from pathlib import Path
import re
import sys

import tool_registry as R

ALLOWED_IMPORTS = {
    "math", "cmath", "decimal", "fractions", "statistics", "numbers", "random", "datetime", "calendar", "zoneinfo",
    "time", "json", "re", "string", "textwrap", "unicodedata", "difflib", "collections", "itertools", "functools",
    "operator", "dataclasses", "enum", "typing", "abc", "copy", "bisect", "heapq", "array", "struct", "hashlib", "hmac",
    "base64", "binascii", "zlib", "uuid", "html", "html.parser", "urllib.parse", "xml.etree.ElementTree", "ipaddress",
    "colorsys", "csv", "io", "bs4", "numpy",
}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "breakpoint", "input",
                   "memoryview", "help", "exit", "quit", "getattr", "setattr", "delattr"}
FORBIDDEN_ATTRIBUTES = {"open", "system", "popen", "remove", "unlink", "rmdir", "chmod", "chown", "kill", "fork",
                        "spawn", "exec", "load", "loads_unsafe"}
ALLOWED_DUNDERS = {"__init__", "__name__", "__repr__", "__str__", "__eq__", "__lt__", "__hash__", "__len__", "__iter__",
                   "__post_init__", "__all__", "__doc__"}
MIN_CASES, MIN_ERROR_CASES = 4, 1


def check_code(files):
    """Problems found in the tool's Python files (helper modules may import each other)."""
    problems, helpers = [], {name[:-3] for name in files if name.endswith(".py")}
    for name, source in files.items():
        if not name.endswith(".py"):
            continue
        try:
            tree = ast.parse(source, filename=name)
        except SyntaxError as exc:
            problems.append(f"{name}:{exc.lineno}: syntax error")
            continue
        for node in ast.walk(tree):
            where = f"{name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    problems.append(f"{where}: relative imports are not supported; import helper modules by name")
                    continue
                modules = [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
                modules = [m for m in modules if m in ALLOWED_IMPORTS or m.split(".")[0] in helpers] or modules[:1]
            else:
                modules = []
            for module in modules:
                if module not in ALLOWED_IMPORTS and module.split(".")[0] not in helpers:
                    problems.append(f"{where}: import of {module!r} is not allowed")
            if isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
                problems.append(f"{where}: {node.id} is not allowed")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r"\{[^{}]*\.__", node.value):
                problems.append(f"{where}: format strings may not reach dunder attributes")
            if isinstance(node, ast.Attribute):
                if node.attr.startswith("__") and node.attr not in ALLOWED_DUNDERS:
                    problems.append(f"{where}: dunder attribute {node.attr!r} is not allowed")
                elif node.attr in FORBIDDEN_ATTRIBUTES:
                    problems.append(f"{where}: attribute {node.attr!r} is not allowed")
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                problems.append(f"{where}: global/nonlocal state is not allowed; tools are pure functions of their input")
            if isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
                problems.append(f"{where}: async code is not supported")
        if name == "tool.py" and not any(isinstance(n, ast.FunctionDef) and n.name == "run" and len(n.args.args) == 2
                                         for n in tree.body):
            problems.append("tool.py: define run(args, ctx) at module level")
    return problems


def check_scope(manifest, spec=None, existing=None, directory_name=None):
    problems = []
    if spec:
        allowed = set(spec.get("network_hosts") or [])
        extra = [host for host in manifest["network"] if host not in allowed]
        if extra:
            problems.append(f"network hosts {extra} are not in the specification {sorted(allowed)}")
    if existing:
        for other in Path(existing).iterdir():
            if other.is_dir() and other.name == manifest["id"] and other.resolve() != Path(directory_name or "").resolve():
                problems.append(f"tool id {manifest['id']!r} already exists")
    return problems


def check_tests(tool, hidden=None):
    import tool_runtime
    cases = R.tests_of(tool["files"])
    problems = []
    if len(cases) < MIN_CASES:
        problems.append(f"tests.json needs at least {MIN_CASES} cases (has {len(cases)})")
    if sum("expect_error" in case for case in cases) < MIN_ERROR_CASES:
        problems.append("tests.json needs at least one expect_error case")
    reports = {"own": tool_runtime.run_tests(tool)}
    if hidden is not None:
        hidden_tool = {**tool, "files": {**tool["files"], "tests.json": json.dumps(hidden, ensure_ascii=False)}}
        R.tests_of(hidden_tool["files"])
        reports["hidden"] = tool_runtime.run_tests(hidden_tool)
    for label, report in reports.items():
        if report["passed"] != report["total"]:
            failed = [f"{c['name']}: {'; '.join(c['problems'])[:300]}" for c in report["cases"] if not c["passed"]]
            problems.append(f"{label} tests: {report['passed']}/{report['total']} passed — " + " | ".join(failed[:6]))
    return problems, {label: {"passed": r["passed"], "total": r["total"]} for label, r in reports.items()}


def evaluate(directory, spec=None, hidden=None, existing=None):
    report = {"directory": str(directory), "gates": {}, "passed": False}
    try:
        manifest, files, digest = R.read_tool(directory)
        R.tests_of(files)
    except (R.ManifestError, OSError, ValueError) as exc:
        report["gates"]["manifest"] = [str(exc)]
        return report
    report.update(id=manifest["id"], digest=digest[:12])
    report["gates"]["manifest"] = []
    report["gates"]["code"] = check_code(files)
    report["gates"]["scope"] = check_scope(manifest, spec, existing, directory)
    if report["gates"]["code"]:
        report["gates"]["tests"] = ["skipped: code gate failed"]
    else:
        tool = {"id": manifest["id"], "manifest": manifest, "files": files, "digest": digest}
        report["gates"]["tests"], report["tests"] = check_tests(tool, hidden)
    report["passed"] = not any(report["gates"].values())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tool_gates")
    parser.add_argument("directory")
    parser.add_argument("--spec", help="gap specification JSON (network_hosts bound the manifest)")
    parser.add_argument("--hidden-tests", help="declarative test cases written separately from the tool")
    parser.add_argument("--existing", help="library directory whose ids the tool must not reuse")
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text()) if args.spec else None
    hidden = json.loads(Path(args.hidden_tests).read_text()) if args.hidden_tests else None
    report = evaluate(Path(args.directory), spec, hidden, args.existing)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
