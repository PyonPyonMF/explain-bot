"""Video plan: the JSON schema that Sonnet fills, and strict validation of its output.

Sonnet only chooses scenes and text. Every number shown in a chart is computed
by the renderer from the parameters below, never typed by the model.
"""
import ast
import math
import re

import numpy as np

MAX_SCENES = 8
MAX_NARRATION = 400          # characters per scene
MAX_TOTAL_NARRATION = 1600   # characters per video (about 90-110 s of speech)

# --------------------------------------------------------------------------
# Safe math expressions: "sin(x) * exp(-x/3)", "x^2 - 2*x", "sigmoid(x)"
# --------------------------------------------------------------------------
_FUNCS = {
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "exp": np.exp, "log": np.log, "ln": np.log,
    "log10": np.log10, "log2": np.log2, "sqrt": np.sqrt, "abs": np.abs, "tanh": np.tanh,
    "arctan": np.arctan, "atan": np.arctan, "arcsin": np.arcsin, "arccos": np.arccos,
    "sigmoid": lambda v: 1.0 / (1.0 + np.exp(-v)), "relu": lambda v: np.maximum(v, 0.0),
    "floor": np.floor, "ceil": np.ceil, "sign": np.sign,
}
_CONSTS = {"pi": math.pi, "e": math.e}
_BINOPS = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide,
           ast.Pow: np.power, ast.Mod: np.mod}


class PlanError(ValueError):
    pass


def compile_expr(src: str):
    """Return f(x: ndarray) -> ndarray for a restricted expression in x."""
    if not isinstance(src, str) or not src.strip() or len(src) > 200:
        raise PlanError("bad expression")
    src = src.replace("^", "**").replace("−", "-").replace("·", "*")
    tree = ast.parse(src, mode="eval")

    def ev(n, x):
        if isinstance(n, ast.Expression):
            return ev(n.body, x)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.Name):
            if n.id == "x":
                return x
            if n.id in _CONSTS:
                return _CONSTS[n.id]
            raise PlanError(f"unknown name {n.id}")
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            return _BINOPS[type(n.op)](ev(n.left, x), ev(n.right, x))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand, x)
            return -v if isinstance(n.op, ast.USub) else v
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS
                and len(n.args) == 1 and not n.keywords):
            return _FUNCS[n.func.id](ev(n.args[0], x))
        raise PlanError("unsupported syntax in expression")

    def f(x):
        x = np.asarray(x, dtype=float)
        with np.errstate(all="ignore"):
            y = ev(tree, x)
        y = np.broadcast_to(np.asarray(y, dtype=float), x.shape).copy()
        y[~np.isfinite(y)] = np.nan
        return y

    return f


# --------------------------------------------------------------------------
# JSON schema given to Sonnet as the input_schema of a forced tool call
# --------------------------------------------------------------------------
_S = {"type": "string"}
_N = {"type": "number"}


# Fields the model may leave out. Structured outputs allow at most 24 optional fields in total.
OPTIONAL = {"subtitle", "attribution", "where", "x_label", "y_label", "formula_label", "marker_x", "marker_label",
            "bias", "activation", "unit", "group", "cannot_explain_reason"}


def _scene(type_name, props, required, desc):
    p = {"type": {"type": "string", "enum": [type_name]},
         "heading": {"type": "string", "description": "Short scene heading, max 50 chars. Empty string for title and statement scenes if not needed."},
         "narration": {"type": "string", "description": "What the narrator says in this scene. 1-3 short sentences, max 350 chars. Plain text, no markdown, no formulas, numbers written as words where natural."}}
    p.update(props)
    req = [k for k in p if k not in OPTIONAL]
    return {"type": "object", "description": desc, "properties": p, "required": req, "additionalProperties": False}


SCENE_SCHEMAS = [
    _scene("title", {"title": _S, "subtitle": _S}, ["title"],
           "Opening card. Use once, first."),
    _scene("statement", {"text": _S, "attribution": _S}, ["text"],
           "One key sentence in large type, e.g. the core idea, or a short quote of the post (max 220 chars)."),
    _scene("bullets", {"items": {"type": "array", "items": _S, "minItems": 2, "maxItems": 5}}, ["heading", "items"],
           "2-5 short points (each max 80 chars)."),
    _scene("flow", {"steps": {"type": "array", "items": _S, "minItems": 2, "maxItems": 6}}, ["heading", "steps"],
           "A process: 2-6 steps in order, each max 40 chars. The current step is highlighted while narrating."),
    _scene("compare", {"left_title": _S, "left_items": {"type": "array", "items": _S, "maxItems": 4},
                       "right_title": _S, "right_items": {"type": "array", "items": _S, "maxItems": 4}},
           ["heading", "left_title", "left_items", "right_title", "right_items"],
           "Two columns: A versus B, or myth versus fact. Items max 60 chars."),
    _scene("formula", {"formula": {"type": "string", "description": "matplotlib mathtext WITHOUT dollar signs, e.g. y = w_1 x_1 + w_2 x_2 + b"},
                       "where": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
                           "symbol": {"type": "string", "description": "mathtext without $, e.g. \\eta"},
                           "meaning": _S}, "required": ["symbol", "meaning"], "additionalProperties": False}}},
           ["heading", "formula"], "One formula and the meaning of its symbols."),
    _scene("plot", {"expr": {"type": "string", "description": "Python-like expression in x. Allowed: + - * / ^ ( ), numbers, pi, e, sin cos tan exp log sqrt abs tanh arctan sigmoid relu."},
                    "x_min": _N, "x_max": _N, "x_label": _S, "y_label": _S,
                    "formula_label": {"type": "string", "description": "Same function as mathtext without $, e.g. y = x^2"},
                    "marker_x": {"type": "number", "description": "Optional x of a point to highlight"},
                    "marker_label": _S},
           ["heading", "expr", "x_min", "x_max"], "Animated graph of a function."),
    _scene("gradient_descent", {"expr": {"type": "string", "description": "Loss as an expression in x (same syntax as plot). Must have a minimum inside the range."},
                                "x_min": _N, "x_max": _N, "start": _N,
                                "learning_rate": _N, "steps": {"type": "integer", "minimum": 3, "maximum": 25}},
           ["heading", "expr", "x_min", "x_max", "start", "learning_rate", "steps"],
           "A ball rolls down a curve step by step: x <- x - lr * f'(x). The renderer computes all values."),
    _scene("neuron", {"inputs": {"type": "array", "items": _N, "minItems": 1, "maxItems": 4},
                      "weights": {"type": "array", "items": _N, "minItems": 1, "maxItems": 4},
                      "bias": _N, "activation": {"type": "string", "enum": ["none", "relu", "sigmoid", "tanh"]}},
           ["heading", "inputs", "weights"], "One artificial neuron; the renderer computes the output."),
    _scene("vectors", {"x_label": _S, "y_label": _S,
                       "vectors": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "object", "properties": {
                           "label": _S, "x": _N, "y": _N, "group": {"type": "integer", "minimum": 0, "maximum": 5}},
                           "required": ["label", "x", "y"], "additionalProperties": False}}},
           ["heading", "vectors"], "2-D arrows from the origin, e.g. toy word embeddings. Same group = same colour."),
    _scene("bars", {"unit": _S, "items": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"type": "object", "properties": {
                        "label": _S, "value": _N}, "required": ["label", "value"], "additionalProperties": False}}},
           ["heading", "items"], "Bar chart. ONLY for numbers that are in the source text or are well-established facts."),
    _scene("summary", {"rows": {"type": "array", "minItems": 2, "maxItems": 5, "items": {"type": "object", "properties": {
                           "key": _S, "text": _S}, "required": ["key", "text"], "additionalProperties": False}}},
           ["heading", "rows"], "Closing summary: short key word + one line each. Use once, last."),
]
SCENE_TYPES = [s["properties"]["type"]["enum"][0] for s in SCENE_SCHEMAS]

PLAN_SCHEMA_RAW = {
        "type": "object",
        "properties": {
            "cannot_explain_reason": {"type": "string", "description": "Set ONLY if you will not explain this request. Short reason in the user's language. Then scenes may be empty."},
            "title": {"type": "string", "description": "Video title, max 60 chars."},
            "language": {"type": "string", "description": "BCP-47 code of the narration language, e.g. ru, en."},
            "scenes": {"type": "array", "maxItems": MAX_SCENES, "description": "Empty only when cannot_explain_reason is set.",
                       "items": {"anyOf": SCENE_SCHEMAS}},
        },
        "required": ["title", "language", "scenes"],
        "additionalProperties": False,
}


def to_structured_schema(node):
    """Make a JSON schema fit the structured-output subset (same rules as the Anthropic SDK's transform_schema):
    keep type/anyOf/enum/description/properties/required/items and minItems 0 or 1; move other keywords
    (maxItems, minimum, maximum, ...) into the description; set additionalProperties false on every object."""
    node = dict(node)
    out = {}
    if "anyOf" in node:
        out["anyOf"] = [to_structured_schema(v) for v in node.pop("anyOf")]
        node.pop("type", None)
    else:
        out["type"] = node.pop("type")
    for k in ("enum", "description"):
        if k in node:
            out[k] = node.pop(k)
    t = out.get("type")
    if t == "object":
        out["properties"] = {k: to_structured_schema(v) for k, v in node.pop("properties", {}).items()}
        node.pop("additionalProperties", None)
        out["additionalProperties"] = False
        if "required" in node:
            out["required"] = node.pop("required")
    elif t == "array":
        if "items" in node:
            out["items"] = to_structured_schema(node.pop("items"))
        if node.get("minItems") in (0, 1):
            out["minItems"] = node.pop("minItems")
    if node:  # unsupported keywords become a hint in the description
        hint = "{" + ", ".join(f"{k}: {v}" for k, v in node.items()) + "}"
        out["description"] = (out.get("description", "") + "\n\n" + hint).strip()
    return out


PLAN_SCHEMA = to_structured_schema(PLAN_SCHEMA_RAW)

# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
_MATH_CHECK_CACHE = {}


def _math_ok(s: str) -> bool:
    if "$" not in s:
        return True
    if s in _MATH_CHECK_CACHE:
        return _MATH_CHECK_CACHE[s]
    from matplotlib.mathtext import MathTextParser
    try:
        if s.count("$") % 2:
            raise ValueError("odd $")
        MathTextParser("path").parse(s)
        ok = True
    except Exception:
        ok = False
    _MATH_CHECK_CACHE[s] = ok
    return ok


def clean_text(v, limit, default=""):
    if v is None:
        return default
    s = re.sub(r"\s+", " ", str(v)).strip()
    s = s.replace("**", "").replace("`", "")
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    if not _math_ok(s):
        s = s.replace("$", "")
    return s


def clean_math(v, limit=120):
    """Mathtext without $ -> (string_with_$, is_math)."""
    s = clean_text(v, limit).strip("$ ")
    if not s:
        return "", False
    if _math_ok(f"${s}$"):
        return f"${s}$", True
    return s, False


def _num(v, lo=-1e6, hi=1e6, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        if default is None:
            raise PlanError("number expected")
        return default
    if not math.isfinite(f):
        raise PlanError("non-finite number")
    return min(hi, max(lo, f))


def _str_list(v, n_max, limit, n_min=1):
    if not isinstance(v, list):
        raise PlanError("list expected")
    out = [clean_text(x, limit) for x in v if str(x).strip()][:n_max]
    if len(out) < n_min:
        raise PlanError("list too short")
    return out


def _range(p):
    a, b = _num(p.get("x_min")), _num(p.get("x_max"))
    if a > b:
        a, b = b, a
    if b - a < 1e-6:
        raise PlanError("empty x range")
    return a, b


def _validate_scene(s):
    t = str(s.get("type", "")).strip().lower()
    if t not in SCENE_TYPES:
        raise PlanError(f"unknown scene type {t}")
    o = {"type": t,
         "heading": clean_text(s.get("heading"), 60),
         "narration": clean_text(s.get("narration"), MAX_NARRATION).replace("$", "")}
    if t == "title":
        o["title"] = clean_text(s.get("title"), 70) or o["heading"]
        o["subtitle"] = clean_text(s.get("subtitle"), 100)
        if not o["title"]:
            raise PlanError("empty title")
    elif t == "statement":
        o["text"] = clean_text(s.get("text"), 240)
        o["attribution"] = clean_text(s.get("attribution"), 60)
        if not o["text"]:
            raise PlanError("empty statement")
    elif t == "bullets":
        o["items"] = _str_list(s.get("items"), 5, 90, 2)
    elif t == "flow":
        o["steps"] = _str_list(s.get("steps"), 6, 48, 2)
    elif t == "compare":
        o["left_title"] = clean_text(s.get("left_title"), 40)
        o["right_title"] = clean_text(s.get("right_title"), 40)
        o["left_items"] = _str_list(s.get("left_items"), 4, 70)
        o["right_items"] = _str_list(s.get("right_items"), 4, 70)
    elif t == "formula":
        o["formula"], o["formula_is_math"] = clean_math(s.get("formula"))
        if not o["formula"]:
            raise PlanError("empty formula")
        rows = []
        for r in (s.get("where") or [])[:4]:
            sym, _ = clean_math(r.get("symbol"), 40)
            mean = clean_text(r.get("meaning"), 70)
            if sym and mean:
                rows.append({"symbol": sym, "meaning": mean})
        o["where"] = rows
    elif t in ("plot", "gradient_descent"):
        f = compile_expr(s.get("expr"))
        a, b = _range(s)
        y = f(np.linspace(a, b, 400))
        if np.mean(np.isfinite(y)) < 0.5:
            raise PlanError("function is undefined on most of the range")
        o.update(expr=str(s["expr"]), x_min=a, x_max=b)
        if t == "plot":
            o["x_label"] = clean_text(s.get("x_label"), 30)
            o["y_label"] = clean_text(s.get("y_label"), 30)
            o["formula_label"], _ = clean_math(s.get("formula_label"), 80)
            if s.get("marker_x") is not None:
                mx = _num(s.get("marker_x"), default=None)
                if a <= mx <= b and np.isfinite(f(np.array([mx]))[0]):
                    o["marker_x"] = mx
                    o["marker_label"] = clean_text(s.get("marker_label"), 40)
        else:
            o["start"] = _num(s.get("start"), a, b)
            o["learning_rate"] = _num(s.get("learning_rate"), 1e-6, 1e3)
            o["steps"] = int(_num(s.get("steps"), 3, 25, 10))
    elif t == "neuron":
        xs = [_num(v, -1e4, 1e4) for v in (s.get("inputs") or [])][:4]
        ws = [_num(v, -1e4, 1e4) for v in (s.get("weights") or [])][:4]
        n = min(len(xs), len(ws))
        if n < 1:
            raise PlanError("neuron needs inputs and weights")
        o.update(inputs=xs[:n], weights=ws[:n], bias=_num(s.get("bias"), -1e4, 1e4, 0.0),
                 activation=s.get("activation") if s.get("activation") in ("relu", "sigmoid", "tanh") else "none")
    elif t == "vectors":
        vs = []
        for v in (s.get("vectors") or [])[:10]:
            try:
                vs.append({"label": clean_text(v.get("label"), 24), "x": _num(v.get("x")), "y": _num(v.get("y")),
                           "group": int(_num(v.get("group"), 0, 5, 0))})
            except PlanError:
                continue
        if not vs or max(max(abs(v["x"]), abs(v["y"])) for v in vs) == 0:
            raise PlanError("no usable vectors")
        o.update(vectors=vs, x_label=clean_text(s.get("x_label"), 30), y_label=clean_text(s.get("y_label"), 30))
    elif t == "bars":
        items = []
        for it in (s.get("items") or [])[:8]:
            try:
                items.append({"label": clean_text(it.get("label"), 40), "value": _num(it.get("value"), -1e15, 1e15)})
            except PlanError:
                continue
        if len(items) < 2:
            raise PlanError("bars need 2 items")
        o.update(items=items, unit=clean_text(s.get("unit"), 20))
    elif t == "summary":
        rows = []
        for r in (s.get("rows") or [])[:5]:
            k, v = clean_text(r.get("key"), 22), clean_text(r.get("text"), 110)
            if k and v:
                rows.append({"key": k, "text": v})
        if len(rows) < 2:
            raise PlanError("summary needs 2 rows")
        o["rows"] = rows
    return o


def validate_plan(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise PlanError("plan is not an object")
    scenes, dropped = [], []
    for i, s in enumerate((raw.get("scenes") or [])[:MAX_SCENES]):
        try:
            scenes.append(_validate_scene(s if isinstance(s, dict) else {}))
        except (PlanError, SyntaxError, ValueError, TypeError, AttributeError) as e:
            dropped.append(f"scene {i}: {e}")
    # cap total narration
    total = 0
    for s in scenes:
        room = MAX_TOTAL_NARRATION - total
        if len(s["narration"]) > room:
            s["narration"] = s["narration"][: max(0, room)].rsplit(" ", 1)[0]
        total += len(s["narration"])
    scenes = [s for s in scenes if s["narration"] or s["type"] == "title"]
    if sum(1 for s in scenes if s["type"] != "title") < 1:
        raise PlanError("plan has no usable scenes: " + "; ".join(dropped))
    title = clean_text(raw.get("title"), 70) or "Explainer"
    if scenes[0]["type"] != "title":
        scenes.insert(0, {"type": "title", "heading": "", "narration": "", "title": title, "subtitle": ""})
    return {"title": title, "language": clean_text(raw.get("language"), 10) or "en",
            "scenes": scenes, "dropped": dropped}
