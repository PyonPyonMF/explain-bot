"""Client for Jev, TypeSafe AI's decision model: typed answers with calibrated probabilities, no generated text.

Low-level transport for the fast "System One" routing judge. Raw urllib like the rest of the bot
(pipeline._http, research._headers): stdlib only, no vendor SDK in the image.

Privacy: system_one() sends `state` and the questions to a third-party processor exactly as given. Callers must
pass only the minimized request text a decision needs, never credentials, tokens, private URLs or unrelated
conversation. The router is opt-in: nothing is sent unless TYPESAFE_API_KEY is set, and JEV_ROUTER=0 switches it
off even then (configured()). Logs and exception messages never contain the key, the state or question text;
usage is logged as counters only.

Environment: TYPESAFE_API_KEY, TYPESAFE_BASE_URL (default https://api.typesafe.ai), TYPESAFE_DEFAULT_MODEL
(default jev-latest), JEV_ROUTER (0 = off), JEV_TIMEOUT (seconds for the whole call including retries, default 10),
JEV_MAX_STATE_CHARS (cap on the serialized request body, default 24000).
"""
import http.client
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("jev")
usage_log = logging.getLogger("jev_usage")

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_CHARS = 24000
MAX_RETRIES = 2
RETRY_WAIT_CAP = 5.0       # a router asked to wait longer than this has already lost: fail and fall back
MIN_ATTEMPT = 0.5          # do not start a retry with less budget left than this
BACKOFF = 0.5
MAX_RESPONSE_BYTES = 1 << 20
EXCERPT_CHARS = 200
TOLERANCE = 1e-3           # float drift just outside [0, 1] is clamped; anything further is a malformed response
KINDS = ("choice", "score", "noul")


class JevError(Exception):
    """Invalid request, missing configuration or malformed response; base of every error raised here."""

    def __init__(self, message, status=None, request_id=None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class JevUnavailable(JevError):
    """Network failure, timeout, 5xx or rate limit after the bounded retries. Fall back."""


class JevRejected(JevError):
    """The API refused the request (4xx): key, permissions, unknown model, invalid questions."""


def configured():
    """True only when a key is set and the router is not switched off with JEV_ROUTER=0."""
    return bool(os.environ.get("TYPESAFE_API_KEY", "").strip()) and os.environ.get("JEV_ROUTER", "1").strip() != "0"


# -------------------------------------------------------------- questions
def _content(value):
    return isinstance(value, (str, dict, list, tuple))


def _question(question, where):
    """Validated wire form of one question; optional fields that are None are left out."""
    if not isinstance(question, dict):
        raise JevError(f"{where}: a question must be an object")
    kind = question.get("type")
    if kind not in KINDS:
        raise JevError(f"{where}: type must be one of {', '.join(KINDS)}")
    if set(question) - {"type", "instructions", "criteria"}:
        raise JevError(f"{where}: only type, instructions and criteria are allowed")
    instructions, criteria = question.get("instructions"), question.get("criteria")
    if instructions is not None and not _content(instructions):
        raise JevError(f"{where}: instructions must be a string, object or array")
    wire = {"type": kind}
    if instructions is not None:
        wire["instructions"] = instructions
    if kind == "choice":
        if not isinstance(criteria, dict) or not criteria:
            raise JevError(f"{where}: choice criteria must be a non-empty object of label -> description or null")
        if not all(isinstance(label, str) and label for label in criteria):
            raise JevError(f"{where}: choice labels must be non-empty strings")
        if not all(text is None or _content(text) for text in criteria.values()):
            raise JevError(f"{where}: a label description must be a string, object, array or null")
        wire["criteria"] = dict(criteria)
    elif kind == "score":
        if not isinstance(criteria, (list, tuple)) or not criteria:
            raise JevError(f"{where}: score criteria must be a non-empty list, one entry per level from 0")
        if not all(_content(level) for level in criteria):
            raise JevError(f"{where}: each score level must be a string, object or array")
        wire["criteria"] = list(criteria)
    else:
        if criteria is not None:
            if not isinstance(criteria, dict) or set(criteria) - {"true", "false"}:
                raise JevError(f"{where}: noul criteria must be an object with only 'true' and 'false' outcomes")
            if not all(text is None or _content(text) for text in criteria.values()):
                raise JevError(f"{where}: an outcome description must be a string, object, array or null")
            wire["criteria"] = dict(criteria)
        if instructions is None and all(text is None for text in (criteria or {}).values()):
            raise JevError(f"{where}: noul needs instructions or at least one described outcome")
    return wire


def _questions(questions):
    if not isinstance(questions, dict) or not questions:
        raise JevError("questions must be a non-empty object of name -> question")
    if not all(isinstance(name, str) and name for name in questions):
        raise JevError("question names must be non-empty strings")
    # Names are the caller's identifiers (e.g. "route"), never question text, so errors may show them.
    return {name: _question(q, f"question {name[:40]!r}") for name, q in questions.items()}


def choice(instructions, criteria):
    """Pick one label. criteria: {label: description or None}. Answer: choice, confidence, probabilities."""
    return _question({"type": "choice", "instructions": instructions, "criteria": criteria}, "choice()")


def score(instructions, levels):
    """Ordinal scale; levels[i] describes level i, from 0. Answer: expected level, confidence, probabilities."""
    return _question({"type": "score", "instructions": instructions, "criteria": levels}, "score()")


def noul(instructions, true=None, false=None):
    """Yes/no. true/false optionally describe the outcomes. Answer: noul = probability of true."""
    criteria = None if true is None and false is None else {"true": true, "false": false}
    return _question({"type": "noul", "instructions": instructions, "criteria": criteria}, "noul()")


# ---------------------------------------------------------------- answers
def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _between(value, high, bad, what):
    number = _number(value)
    if number is None or not -TOLERANCE <= number <= high + TOLERANCE:
        raise bad(f"{what} must be a number between 0 and {high:g}")
    return min(max(number, 0.0), float(high))


def _choice(answer, question, bad):
    labels = question["criteria"]
    probabilities = answer.get("probabilities")
    if not isinstance(answer.get("choice"), str) or answer["choice"] not in labels:
        raise bad("choice is not one of the requested labels")
    if not isinstance(probabilities, dict) or not probabilities or not set(probabilities) <= set(labels):
        raise bad("choice probabilities must map requested labels to probabilities")
    return {"type": "choice", "choice": answer["choice"],
            "confidence": _between(answer.get("confidence"), 1, bad, "confidence"),
            "probabilities": {label: _between(probabilities[label], 1, bad, "probability")
                              for label in labels if label in probabilities}}


def _score(answer, question, bad):
    levels = question["criteria"]
    probabilities = answer.get("probabilities")
    legend = answer.get("legend", {str(i): text for i, text in enumerate(levels)})
    if not isinstance(probabilities, dict) or not probabilities or not isinstance(legend, dict):
        raise bad("score probabilities and legend must be objects keyed by level")

    def level(key):
        # Wire keys are decimal strings ("0", "1", ...) of the requested levels.
        if not isinstance(key, str) or not key.isascii() or not key.isdigit() or key != str(int(key)) \
                or int(key) >= len(levels):
            raise bad("score keys must be the requested level numbers")
        return int(key)

    return {"type": "score", "score": _between(answer.get("score"), len(levels) - 1, bad, "score"),
            "confidence": _between(answer.get("confidence"), 1, bad, "confidence"),
            "legend": dict(sorted(((level(k), v) for k, v in legend.items()), key=lambda item: item[0])),
            "probabilities": dict(sorted(((level(k), _between(p, 1, bad, "probability"))
                                          for k, p in probabilities.items()), key=lambda item: item[0]))}


def _noul(answer, question, bad):
    return {"type": "noul", "noul": _between(answer.get("noul"), 1, bad, "noul")}


PARSERS = {"choice": _choice, "score": _score, "noul": _noul}


def _parse(raw, wire, model, request_id):
    def bad(why):
        return JevError(f"Jev returned a malformed response: {why}", request_id=request_id)

    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise bad("not JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise bad("no answers object")
    # Accounting fields may be absent; anything present must be well-typed. Decisions must be complete.
    model = data.get("model", model)
    usage = data.get("usage", {})
    if not isinstance(model, str) or not model or not isinstance(usage, dict):
        raise bad("model or usage has the wrong type")
    tokens = {name: usage.get(name, 0) for name in ("input_tokens", "output_tokens")}
    if not all(type(count) is int and count >= 0 for count in tokens.values()):
        raise bad("token counts must be non-negative integers")
    answers = {}
    for name, question in wire.items():
        answer = data["answers"].get(name)
        if not isinstance(answer, dict) or not isinstance(answer.get("type"), str):
            raise bad(f"no typed answer for question {name[:40]!r}")
        if answer["type"] not in PARSERS:
            continue  # a newer answer type: leave it out instead of failing the whole decision
        if answer["type"] != question["type"]:
            raise bad(f"{answer['type']} answer to {question['type']} question {name[:40]!r}")
        answers[name] = PARSERS[answer["type"]](answer, question, bad)
    return {"model": model, "usage": tokens, "answers": answers, "request_id": request_id}


# ------------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would forward the Authorization header to wherever a redirect points; surface the 3xx instead.
    def redirect_request(self, *args, **kwargs):
        return None


def _lower(headers):
    return {k.lower(): v for k, v in headers.items()} if headers else {}


def _http(url, data, headers, method, timeout):
    """One exchange -> (status, lower-cased headers, body). Error statuses are returned, not raised."""
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise JevError("Jev returned a malformed response: larger than 1 MiB")
            return response.status, _lower(response.headers), body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4096)
        except (OSError, http.client.HTTPException):
            body = b""
        finally:
            e.close()
        return e.code, _lower(e.headers), body


def _drop_inputs(value):
    # Validation errors often echo the offending input, which here would be Discord content.
    if isinstance(value, dict):
        return {k: _drop_inputs(v) for k, v in value.items() if k != "input"}
    return [_drop_inputs(v) for v in value] if isinstance(value, list) else value


def _excerpt(body, key):
    text = body[:4096].decode("utf-8", "replace")
    try:
        text = json.dumps(_drop_inputs(json.loads(text)), ensure_ascii=False)
    except (ValueError, RecursionError):
        pass
    return " ".join(text.replace(key, "[key]").split())[:EXCERPT_CHARS]


def _retry_after(headers):
    """Server-requested delay in seconds; retry-after-ms wins. None when absent (an HTTP-date counts as absent)."""
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        try:
            wait = float(headers.get(name) or "") * scale
        except ValueError:
            continue
        if math.isfinite(wait):
            return max(wait, 0.0)
    return None


def _env_number(name, default, kind=float):
    try:
        value = kind(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if 0 < value < math.inf else default


def _base_url():
    base = (os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    parts = urllib.parse.urlsplit(base)
    loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if not parts.hostname or not (parts.scheme == "https" or parts.scheme == "http" and loopback):
        raise JevError("TYPESAFE_BASE_URL must be an https URL (plain http only for a loopback test server)")
    return base


def _request(method, path, data, timeout):
    """One API call with at most MAX_RETRIES retries inside a total time budget. Returns (body, request id)."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JevError("TYPESAFE_API_KEY is not set")
    budget = _env_number("JEV_TIMEOUT", DEFAULT_TIMEOUT) if timeout is None else _number(timeout)
    if not budget or budget <= 0:
        raise JevError("timeout must be a positive number of seconds")
    url = _base_url() + path
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    deadline = time.monotonic() + budget
    for attempt in range(MAX_RETRIES + 1):
        try:
            status, response_headers, body = _http(url, data, headers, method, max(deadline - time.monotonic(), 0.01))
        except (OSError, http.client.HTTPException) as e:
            # Timeouts, DNS, TLS and resets; URLError and TimeoutError are OSErrors.
            reason = getattr(e, "reason", e)
            failure = JevUnavailable(f"Jev {path}: timed out" if isinstance(reason, TimeoutError) else
                                     f"Jev {path}: connection failed: {type(e).__name__}: {str(reason)[:120]}")
            wait = BACKOFF * 2 ** attempt
        else:
            request_id = response_headers.get("x-typesafe-request-id")
            if 200 <= status < 300:
                return body, request_id
            message = f"Jev {path}: HTTP {status}: {_excerpt(body, key)}"
            if status not in (408, 409, 429) and not 500 <= status < 600:
                raise JevRejected(message, status, request_id)
            failure = JevUnavailable(message, status, request_id)
            wait = _retry_after(response_headers)
            wait = BACKOFF * 2 ** attempt if wait is None else wait
        # Fail fast: never sleep past the cap or the caller's budget; a server asking for longer is not retried.
        if attempt == MAX_RETRIES or wait > RETRY_WAIT_CAP or time.monotonic() + wait + MIN_ATTEMPT > deadline:
            raise failure
        log.warning("%s; retry %d/%d in %.2fs", failure, attempt + 1, MAX_RETRIES, wait)
        time.sleep(wait)


# -------------------------------------------------------------------- API
def system_one(state, questions, model=None, timeout=None):
    """Ask typed questions about `state` (a string, object or array of minimized request text).

    Returns {"model": str, "usage": {"input_tokens": int, "output_tokens": int}, "answers": {name: answer},
    "request_id": str or None}, where an answer is one of
      {"type": "choice", "choice": label, "confidence": p, "probabilities": {label: p}}
      {"type": "score", "score": expected level, "confidence": p, "legend": {level: text}, "probabilities": {level: p}}
      {"type": "noul", "noul": probability of true}
    with int level keys and probabilities clamped to [0, 1]. Answers of types this client does not know are left out.
    Raises JevError for invalid input (before anything is sent), missing configuration or a malformed response;
    JevRejected for 4xx; JevUnavailable for network failures, timeouts, 5xx and exhausted rate-limit retries.
    """
    wire = _questions(questions)
    if not _content(state):
        raise JevError("state must be a string, object or array")
    if model is None:
        model = os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or DEFAULT_MODEL
    if not isinstance(model, str) or not model.strip():
        raise JevError("model must be a non-empty string")
    try:
        text = json.dumps({"state": state, "model": model, "questions": wire}, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise JevError("state and questions must be JSON-serializable (no NaN, sets or objects)") from None
    limit = _env_number("JEV_MAX_STATE_CHARS", DEFAULT_MAX_CHARS, int)
    if len(text) > limit:
        raise JevError(f"request is {len(text)} characters, over JEV_MAX_STATE_CHARS={limit}; trim the state")
    if not configured():
        raise JevError("Jev is not configured: TYPESAFE_API_KEY is empty or JEV_ROUTER=0")
    started = time.monotonic()
    raw, request_id = _request("POST", "/v1/systemone", text.encode("utf-8"), timeout)
    result = _parse(raw, wire, model, request_id)
    usage_log.info("jev tokens model=%s input=%d output=%d latency_ms=%d request_id=%s", result["model"],
                   result["usage"]["input_tokens"], result["usage"]["output_tokens"],
                   round((time.monotonic() - started) * 1000), request_id or "-")
    return result


def models(timeout=None):
    """Models this key can use: [{"name": str, "description": str or None, "release_date": str or None}]."""
    raw, request_id = _request("GET", "/v1/models", None, timeout)
    try:
        listed = json.loads(raw).get("models")
    except (ValueError, AttributeError):
        listed = None
    if not isinstance(listed, list) or not all(isinstance(m, dict) and isinstance(m.get("name"), str) for m in listed):
        raise JevError("Jev returned a malformed model list", request_id=request_id)
    return [{"name": m["name"], "description": m.get("description"), "release_date": m.get("release_date")}
            for m in listed]
