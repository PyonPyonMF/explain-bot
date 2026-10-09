"""Scene renderers (3Blue1Brown-like dark style) and per-scene video encoding.

Canvas coordinates: x 0..16, y 0..9.  Heading at the top, content in y 1.75..7.6,
subtitles at the bottom.  Each renderer draws one frame for time t of a scene
that lasts T seconds.
"""
import math
import re
import subprocess

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch  # noqa: E402

from plan import compile_expr  # noqa: E402

W_PX, H_PX, DPI = 1280, 720, 100
FPS = 24
AUDIO_DELAY = 0.25      # seconds of silence before the narration in each scene
TAIL = 0.55             # seconds after the narration ends

BG = "#0b0b0f"; FG = "#ece6e2"; GREY = "#7a7a85"; PANEL = "#16161d"
BLUE = "#58C4DD"; YEL = "#FFE45C"; GRN = "#83C167"; RED = "#FC6255"; PURPLE = "#C59BF0"; ORANGE = "#FF9F43"
PAL = [BLUE, YEL, GRN, RED, PURPLE, ORANGE]

plt.rcParams.update({
    "font.family": "DejaVu Sans", "mathtext.fontset": "cm",
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
    "axes.edgecolor": GREY, "xtick.color": GREY, "ytick.color": GREY,
    "axes.labelcolor": FG, "text.color": FG,
})

# ----------------------------------------------------------------- helpers
CW = 0.0112  # approx. canvas units per (font point * character), measured for DejaVu Sans Cyrillic


def sm(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def appear(t, t0, d=0.5):
    return sm((t - t0) / d)


def stagger(n, T, start=0.35, frac=0.6):
    span = max(0.0, frac * T - start - 0.5)
    step = min(1.6, span / max(1, n - 1)) if n > 1 else 0
    return [start + i * step for i in range(n)]


def vis_len(tok):
    s = re.sub(r"\\[A-Za-z]+", "x", tok)
    return max(1, len(re.sub(r"[{}$^_\\]", "", s)))


def wrap(text, width_units, size):
    max_chars = max(6, int(width_units / (CW * size)))
    lines, cur, cur_len = [], [], 0
    for tok in re.findall(r"\$[^$]*\$\S*|\S+", text or ""):
        L = vis_len(tok)
        if cur and cur_len + 1 + L > max_chars:
            lines.append(" ".join(cur)); cur, cur_len = [], 0
        cur.append(tok); cur_len += L + (1 if cur_len else 0)
    if cur:
        lines.append(" ".join(cur))
    return lines or [""]


def fit(text, width, size, min_size=14, max_lines=2):
    while True:
        lines = wrap(text, width, size)
        if len(lines) <= max_lines or size <= min_size:
            break
        size -= 2
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    return "\n".join(lines), size


def line_h(size):
    """Height of one text line in canvas units (linespacing 1.45)."""
    return size * 1.39 * 1.45 / 80.0


def fmt(v):
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e5 or a < 1e-3:
        return f"{v:.2e}".replace("e+0", "e").replace("e-0", "e-")
    s = f"{v:.3f}".rstrip("0").rstrip(".") if a < 100 else f"{v:,.0f}".replace(",", " ")
    return s


def mfmt(v):  # number inside mathtext, negative in parentheses
    s = fmt(v)
    return f"({s})" if v < 0 else s


def canvas(fig):
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 16); ax.set_ylim(0, 9); ax.axis("off")
    return ax


def style_axes(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=12)


# ----------------------------------------------------------- subtitles
def build_subtitles(narration, audio_len):
    """Split the narration into chunks of max 2 lines, timed by character count."""
    if not narration or audio_len <= 0:
        return []
    chunks = []
    for sent in re.split(r"(?<=[.!?…])\s+", narration.strip()):
        lines = wrap(sent, 14.6, 21)
        for i in range(0, len(lines), 2):
            chunks.append("\n".join(lines[i:i + 2]))
    total = sum(len(c) for c in chunks) or 1
    t, out = AUDIO_DELAY, []
    for c in chunks:
        d = audio_len * len(c) / total
        out.append((t, t + d, c)); t += d
    return out


def draw_subtitles(c, subs, t):
    for t0, t1, txt in subs:
        if t0 - 0.1 <= t <= t1 + 0.1:
            a = min(sm((t - t0 + 0.1) / 0.2), sm((t1 + 0.1 - t) / 0.2))
            c.text(8, 0.85, txt, ha="center", va="center", fontsize=21, color=FG, alpha=0.95 * a, linespacing=1.35)


def draw_heading(c, s, t):
    if s.get("heading"):
        txt, size = fit(s["heading"], 14.8, 30, 22, 1)
        c.text(0.6, 8.3, txt, ha="left", va="center", fontsize=size, weight="bold", color=FG, alpha=appear(t, 0, 0.6))


# ------------------------------------------------------------- scenes
def prep_title(s):
    return {"title": fit(s["title"], 14.0, 56, 32, 2), "sub": fit(s.get("subtitle", ""), 14.0, 26, 18, 2)}


def r_title(fig, c, s, x, t, T):
    txt, size = x["title"]
    c.text(8, 5.25, txt, ha="center", va="center", fontsize=size, weight="bold", alpha=appear(t, 0, 0.9), linespacing=1.2)
    u = appear(t, 0.4, 1.0)
    c.plot([8 - 3.5 * u, 8 + 3.5 * u], [4.05, 4.05], color=BLUE, lw=2.5, alpha=u)
    if x["sub"][0]:
        c.text(8, 3.3, x["sub"][0], ha="center", va="center", fontsize=x["sub"][1], color=GREY, alpha=appear(t, 0.8, 0.9))


def prep_statement(s):
    return {"text": fit(s["text"], 12.6, 38, 22, 5)}


def r_statement(fig, c, s, x, t, T):
    txt, size = x["text"]
    a = appear(t, 0.3, 0.8)
    c.plot([1.3, 1.3], [5.0 - 1.9 * appear(t, 0, 0.8), 5.0 + 1.9 * appear(t, 0, 0.8)], color=YEL, lw=5, solid_capstyle="round")
    c.text(1.9, 5.0 + 0.15 * (1 - a), txt, ha="left", va="center", fontsize=size, alpha=a, linespacing=1.35)
    if s.get("attribution"):
        c.text(14.8, 2.35, "— " + s["attribution"], ha="right", va="center", fontsize=20, color=GREY, alpha=appear(t, 1.0, 0.8))


def prep_bullets(s):
    size = 25
    for _ in range(4):
        items = [fit(it, 13.2, size, size, 2)[0] for it in s["items"]]
        n_lines = sum(i.count("\n") + 1 for i in items)
        if n_lines * line_h(size) + 0.35 * len(items) <= 5.6:
            break
        size -= 2
    ys, y = [], 7.25
    for it in items:
        ys.append(y)
        y -= (it.count("\n") + 1) * line_h(size) + 0.35
    return {"items": items, "ys": ys, "size": size}


def r_bullets(fig, c, s, x, t, T):
    starts = stagger(len(x["items"]), T)
    for i, (txt, y) in enumerate(zip(x["items"], x["ys"])):
        a = appear(t, starts[i], 0.5)
        if a <= 0:
            continue
        top = y - line_h(x["size"]) / 2
        c.add_patch(Circle((1.0, top), 0.13, color=PAL[i % len(PAL)], alpha=a))
        c.text(1.45 + 0.25 * (1 - a), y, txt, ha="left", va="top", fontsize=x["size"], alpha=a, linespacing=1.45)


def prep_flow(s):
    n = len(s["steps"])
    rows = [list(range(n))] if n <= 4 else [list(range((n + 1) // 2)), list(range((n + 1) // 2, n))]
    k = max(len(r) for r in rows)
    w = (14.6 - (k - 1) * 0.8) / k
    h = 2.2 if len(rows) == 1 else 1.8
    ycs = [4.9] if len(rows) == 1 else [6.0, 3.1]
    boxes = {}
    x0 = 8 - (k * w + (k - 1) * 0.8) / 2
    for ri, row in enumerate(rows):
        for j, i in enumerate(row):
            col = j if ri == 0 else k - 1 - j   # second row runs right to left (snake)
            boxes[i] = (x0 + col * (w + 0.8), ycs[ri], ri)
    texts = [fit(st, w - 0.4, 22, 13, 3) for st in s["steps"]]
    return {"boxes": boxes, "w": w, "h": h, "texts": texts, "n": n}


def r_flow(fig, c, s, x, t, T):
    n, w, h = x["n"], x["w"], x["h"]
    starts = stagger(n, T, frac=0.45)
    active = min(n - 1, max(0, int(n * (t - AUDIO_DELAY) / max(0.5, T - AUDIO_DELAY - TAIL))))
    for i in range(n):
        bx, yc, ri = x["boxes"][i]
        a = appear(t, starts[i], 0.5)
        if a <= 0:
            continue
        on = i == active and t > starts[-1]
        col = PAL[i % len(PAL)]
        c.add_patch(FancyBboxPatch((bx, yc - h / 2), w, h, boxstyle="round,pad=0.02,rounding_size=0.18",
                                   fc=PANEL, ec=col if on else GREY, lw=3.2 if on else 1.4, alpha=a))
        c.text(bx + 0.2, yc + h / 2 - 0.3, str(i + 1), fontsize=15, color=col, alpha=a, weight="bold", va="center")
        txt, size = x["texts"][i]
        c.text(bx + w / 2, yc - 0.1, txt, ha="center", va="center", fontsize=size, alpha=a, linespacing=1.3)
        if i > 0:
            px, pyc, pri = x["boxes"][i - 1]
            if pri == ri and bx > px:
                start, end = (px + w + 0.05, pyc), (bx - 0.05, yc)
            elif pri == ri:
                start, end = (px - 0.05, pyc), (bx + w + 0.05, yc)
            else:
                start, end = (px + w / 2, pyc - h / 2 - 0.05), (bx + w / 2, yc + h / 2 + 0.05)
            c.annotate("", xy=end, xytext=start, arrowprops=dict(arrowstyle="-|>", color=GREY, lw=2, alpha=a))


def prep_compare(s):
    col = lambda items: [fit(it, 6.4, 21, 21, 2)[0] for it in items]
    return {"l": col(s["left_items"]), "r": col(s["right_items"]),
            "lt": fit(s["left_title"], 6.6, 28, 18, 1), "rt": fit(s["right_title"], 6.6, 28, 18, 1)}


def r_compare(fig, c, s, x, t, T):
    items = [(0, it) for it in x["l"]] + [(1, it) for it in x["r"]]
    starts = stagger(len(items) + 1, T, start=0.9)
    c.text(4.2, 7.1, x["lt"][0], ha="center", fontsize=x["lt"][1], color=BLUE, weight="bold", alpha=appear(t, 0.2))
    c.text(11.8, 7.1, x["rt"][0], ha="center", fontsize=x["rt"][1], color=YEL, weight="bold", alpha=appear(t, 0.5))
    c.plot([8, 8], [1.9, 7.5], color=GREY, lw=1.3, alpha=appear(t, 0.3))
    ys = [6.4, 6.4]
    for k, (side, txt) in enumerate(items):
        a = appear(t, starts[k + 1], 0.5)
        x0 = 1.1 if side == 0 else 8.7
        c.add_patch(Circle((x0, ys[side] - 0.2), 0.1, color=BLUE if side == 0 else YEL, alpha=a))
        c.text(x0 + 0.35, ys[side], txt, ha="left", va="top", fontsize=21, alpha=a, linespacing=1.4)
        ys[side] -= (txt.count("\n") + 1) * line_h(21) + 0.3


def prep_formula(s):
    L = vis_len(s["formula"]) if s["formula_is_math"] else len(s["formula"])
    size = max(22, min(46, int(46 * 24 / max(L, 24))))
    return {"size": size, "where": [(r["symbol"], fit(r["meaning"], 8.2, 22, 22, 1)[0]) for r in s["where"]]}


def r_formula(fig, c, s, x, t, T):
    y0 = 5.9 if x["where"] else 4.9
    kw = {} if s["formula_is_math"] else {"family": "DejaVu Sans Mono"}
    c.text(8, y0, s["formula"], ha="center", va="center", fontsize=x["size"], alpha=appear(t, 0.2, 0.8), **kw)
    starts = stagger(len(x["where"]), T, start=1.4)
    for i, (sym, mean) in enumerate(x["where"]):
        a = appear(t, starts[i], 0.5); y = 4.3 - i * 0.78
        c.text(6.3, y, sym, ha="right", va="center", fontsize=26, color=PAL[i % len(PAL)], alpha=a)
        c.text(6.75, y, "— " + mean, ha="left", va="center", fontsize=22, alpha=a)


def _ylim(ys, extra=()):
    v = np.concatenate([ys[np.isfinite(ys)], np.asarray(extra, float)])
    lo, hi = np.percentile(v, 1), np.percentile(v, 99)
    lo, hi = min(lo, *extra) if extra else lo, max(hi, *extra) if extra else hi
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = 0.1 * (hi - lo)
    return lo - pad, hi + pad


def prep_plot(s):
    f = compile_expr(s["expr"])
    xs = np.linspace(s["x_min"], s["x_max"], 600); ys = f(xs)
    extra = []
    if "marker_x" in s:
        extra = [float(f(np.array([s["marker_x"]]))[0])]
    return {"f": f, "xs": xs, "ys": ys, "ylim": _ylim(ys, extra), "label": fit(s["formula_label"], 4.6, 26, 15, 3) if s.get("formula_label") else None}


def _zero_lines(ax, xlim, ylim):
    if ylim[0] < 0 < ylim[1]:
        ax.axhline(0, color=GREY, lw=0.8, alpha=0.5)
    if xlim[0] < 0 < xlim[1]:
        ax.axvline(0, color=GREY, lw=0.8, alpha=0.5)


def r_plot(fig, c, s, x, t, T):
    ax = fig.add_axes([0.07, 0.23, 0.6, 0.6]); style_axes(ax)
    ax.set_xlim(s["x_min"], s["x_max"]); ax.set_ylim(*x["ylim"])
    _zero_lines(ax, (s["x_min"], s["x_max"]), x["ylim"])
    if s.get("x_label"): ax.set_xlabel(s["x_label"], fontsize=16)
    if s.get("y_label"): ax.set_ylabel(s["y_label"], fontsize=16)
    t_draw = 0.5 * T
    u = sm((t - 0.4) / max(0.5, t_draw))
    k = max(2, int(u * len(x["xs"])))
    ax.plot(x["xs"][:k], x["ys"][:k], color=BLUE, lw=3.5)
    if u < 1 and np.isfinite(x["ys"][k - 1]):
        ax.scatter([x["xs"][k - 1]], [x["ys"][k - 1]], s=70, color=YEL, zorder=5)
    if x["label"]:
        c.text(13.4, 6.6, x["label"][0], ha="center", va="center", fontsize=x["label"][1], color=BLUE, alpha=appear(t, 0.3))
    if "marker_x" in s and t > 0.4 + t_draw:
        a = appear(t, 0.4 + t_draw, 0.5)
        mx = s["marker_x"]; my = float(x["f"](np.array([mx]))[0])
        ax.scatter([mx], [my], s=200, color=YEL, edgecolor=BG, linewidth=1.5, zorder=6, alpha=a)
        ax.plot([mx, mx], [x["ylim"][0], my], color=GREY, lw=1.2, ls="--", alpha=a)
        c.text(13.4, 4.9, f"$x = {fmt(mx)}$\n$y = {fmt(my)}$", ha="center", va="center", fontsize=24, color=YEL, alpha=a, linespacing=1.6)
        if s.get("marker_label"):
            c.text(13.4, 3.6, fit(s["marker_label"], 4.6, 20, 20, 2)[0], ha="center", va="center", fontsize=20, alpha=a)


def prep_gd(s):
    f = compile_expr(s["expr"])
    a, b = s["x_min"], s["x_max"]
    h = 1e-4 * (b - a)
    df = lambda v: (f(np.array([v + h]))[0] - f(np.array([v - h]))[0]) / (2 * h)
    traj = [s["start"]]
    for _ in range(s["steps"]):
        g = df(traj[-1])
        if not np.isfinite(g):
            break
        nx = traj[-1] - s["learning_rate"] * g
        if not np.isfinite(nx) or abs(nx - (a + b) / 2) > 4 * (b - a):
            break
        traj.append(float(nx))
    lo, hi = min(a, *traj), max(b, *traj)
    xs = np.linspace(lo, hi, 600); ys = f(xs)
    ty = [float(f(np.array([v]))[0]) for v in traj]
    return {"f": f, "df": df, "traj": traj, "ty": ty, "xs": xs, "ys": ys,
            "xlim": (lo, hi), "ylim": _ylim(ys, [v for v in ty if np.isfinite(v)])}


def r_gd(fig, c, s, x, t, T):
    ax = fig.add_axes([0.07, 0.23, 0.56, 0.6]); style_axes(ax)
    ax.set_xlim(*x["xlim"]); ax.set_ylim(*x["ylim"]); _zero_lines(ax, x["xlim"], x["ylim"])
    ax.set_xlabel("$x$", fontsize=18); ax.set_ylabel("$f(x)$", fontsize=18)
    ax.plot(x["xs"], x["ys"], color=RED, lw=3)
    traj, ty, n = x["traj"], x["ty"], len(x["traj"]) - 1
    t0, t1 = 0.35 * T, 0.88 * T
    if n == 0 or t <= t0:
        k, fr = 0, 0.0
    else:
        uu = min(1.0, (t - t0) / (t1 - t0)) * n
        k = min(n, int(uu)); fr = sm((uu - k) / 0.6) if k < n else 0.0
    cx = traj[k] + (traj[min(k + 1, n)] - traj[k]) * fr
    cy = float(x["f"](np.array([cx]))[0])
    ax.scatter(traj[:k + 1], ty[:k + 1], s=40, color=YEL, alpha=0.45, zorder=4)
    g = x["df"](cx)
    if np.isfinite(g) and np.isfinite(cy):
        span = 0.08 * (x["xlim"][1] - x["xlim"][0])
        d = span / math.sqrt(1 + (g * span / max(1e-9, x["ylim"][1] - x["ylim"][0])) ** 2)
        ax.plot([cx - d, cx + d], [cy - g * d, cy + g * d], color=GRN, lw=3, alpha=appear(t, 0.8), zorder=5)
        ax.scatter([cx], [cy], s=230, color=YEL, edgecolor=BG, linewidth=1.5, zorder=6)
    a = appear(t, 0.5)
    c.text(13.0, 7.0, r"$x_{k+1} = x_k - \eta\, f\,'(x_k)$", ha="center", fontsize=24, alpha=a)
    c.text(13.0, 6.2, f"$\\eta = {fmt(s['learning_rate'])}$", ha="center", fontsize=22, color=GREY, alpha=a)
    rows = [(f"$k = {k}$", GREY), (f"$x = {fmt(cx)}$", BLUE), (f"$f(x) = {fmt(cy) if np.isfinite(cy) else '?'}$", RED),
            (f"$f\\,'(x) = {fmt(g) if np.isfinite(g) else '?'}$", GRN)]
    for i, (txt, col) in enumerate(rows):
        c.text(13.0, 5.1 - i * 0.75, txt, ha="center", fontsize=23, color=col, alpha=a)


def prep_neuron(s):
    z = sum(w * v for w, v in zip(s["weights"], s["inputs"])) + s["bias"]
    act = {"none": lambda v: v, "relu": lambda v: max(0.0, v), "sigmoid": lambda v: 1 / (1 + math.exp(-max(-60, min(60, v)))),
           "tanh": math.tanh}[s["activation"]]
    return {"z": z, "a": act(z), "wmax": max(1e-9, max(abs(w) for w in s["weights"]))}


def r_neuron(fig, c, s, x, t, T):
    n = len(s["inputs"]); nx, ny = 8.2, 4.95
    span = min(1.75, 0.9 * (n - 1))
    ys = [ny] if n == 1 else list(np.linspace(ny + span, ny - span, n))
    a_in, a_w, a_out, a_eq = (appear(t, v) for v in (0.3, 0.3 + 0.18 * T, 0.3 + 0.4 * T, 0.3 + 0.5 * T))
    for i, (v, w, py) in enumerate(zip(s["inputs"], s["weights"], ys)):
        col = BLUE if w >= 0 else RED
        c.plot([3.2, nx - 1.0], [py, ny], color=col, lw=1.2 + 5 * abs(w) / x["wmax"], alpha=0.85 * a_w, solid_capstyle="round", zorder=1)
        c.add_patch(Circle((2.6, py), 0.6, fc=BG, ec=FG, lw=2.2, alpha=a_in, zorder=2))
        c.text(2.6, py, fmt(v), ha="center", va="center", fontsize=19 if len(fmt(v)) < 5 else 14, alpha=a_in, zorder=3)
        c.text(1.4, py, f"$x_{i + 1}$", ha="center", va="center", fontsize=24, alpha=a_in)
        mx, my = 3.2 + 0.45 * (nx - 1.0 - 3.2), py + 0.45 * (ny - py) + 0.32
        c.text(mx, my, f"$w_{i + 1} = {mfmt(w)}$", ha="center", va="bottom", fontsize=19, color=col, alpha=a_w)
    c.add_patch(Circle((nx, ny), 1.0, fc=BG, ec=FG, lw=3, alpha=a_in, zorder=2))
    c.text(nx, ny, r"$\Sigma$", ha="center", va="center", fontsize=38, alpha=a_in, zorder=3)
    c.text(nx, ny - 1.4, f"$b = {mfmt(s['bias'])}$" + ("" if s["activation"] == "none" else f",  {s['activation']}"),
           ha="center", va="center", fontsize=18, color=GREY, alpha=a_w)
    c.annotate("", xy=(11.6, ny), xytext=(nx + 1.0, ny), arrowprops=dict(arrowstyle="-|>", color=FG, lw=2.5, alpha=a_out))
    c.text(12.9, ny, fmt(x["a"]), ha="center", va="center", fontsize=40, color=GRN, alpha=a_out)
    terms = " + ".join(f"{mfmt(w)}\\cdot {mfmt(v)}" for w, v in zip(s["weights"], s["inputs"]))
    eq = f"$z = {terms} + {mfmt(s['bias'])} = {fmt(x['z'])}$"
    size = 26 if len(eq) < 70 else 20
    c.text(8, 2.2, eq, ha="center", va="center", fontsize=size, alpha=a_eq)
    if s["activation"] != "none":
        c.text(8, 1.62, f"$a = \\mathrm{{{s['activation']}}}(z) = {fmt(x['a'])}$", ha="center", va="center", fontsize=22, color=GRN, alpha=a_eq)


def prep_vectors(s):
    m = max(max(abs(v["x"]), abs(v["y"])) for v in s["vectors"]) * 1.3
    return {"lim": m}


def r_vectors(fig, c, s, x, t, T):
    ax = fig.add_axes([0.06, 0.2, 0.5, 0.64]); style_axes(ax)
    m = x["lim"]; ax.set_xlim(-m, m); ax.set_ylim(-m, m); ax.set_aspect("equal")
    ax.axhline(0, color=GREY, lw=0.8, alpha=0.5); ax.axvline(0, color=GREY, lw=0.8, alpha=0.5)
    if s.get("x_label"): ax.set_xlabel(s["x_label"], fontsize=15)
    if s.get("y_label"): ax.set_ylabel(s["y_label"], fontsize=15)
    vs = s["vectors"]; starts = stagger(len(vs), T)
    for i, v in enumerate(vs):
        a = appear(t, starts[i], 0.6)
        if a <= 0:
            continue
        col = PAL[v["group"] % len(PAL)]
        ax.annotate("", xy=(v["x"] * a, v["y"] * a), xytext=(0, 0), arrowprops=dict(arrowstyle="-|>", color=col, lw=2.2))
        if v["label"]:
            off = 0.05 * m
            ax.text(v["x"] * a + off * np.sign(v["x"] or 1), v["y"] * a + off * np.sign(v["y"] or 1), v["label"], color=col,
                    fontsize=15, weight="bold", ha="center", va="center", alpha=a)
        c.text(10.0, 7.2 - i * 0.55, f"{v['label'][:14]:<14} [{fmt(v['x']):>6}, {fmt(v['y']):>6}]", fontsize=15,
               family="DejaVu Sans Mono", color=col, alpha=a, va="center")


def prep_bars(s):
    labels = [fit(it["label"], 3.6, 14, 14, 2)[0] for it in s["items"]]
    vals = [it["value"] for it in s["items"]]
    lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
    pad = 0.18 * (hi - lo or 1)
    return {"labels": labels, "vals": vals, "xlim": (lo - (pad if lo < 0 else 0), hi + pad)}


def r_bars(fig, c, s, x, t, T):
    ax = fig.add_axes([0.27, 0.22, 0.65, 0.6]); style_axes(ax)
    n = len(x["vals"]); ys = np.arange(n)[::-1]
    ax.set_yticks(ys); ax.set_yticklabels(x["labels"], fontsize=14, color=FG)
    ax.set_xlim(*x["xlim"]); ax.set_ylim(-0.7, n - 0.3)
    ax.axvline(0, color=GREY, lw=1)
    starts = stagger(n, T, frac=0.5)
    span = x["xlim"][1] - x["xlim"][0]
    for i, (v, y) in enumerate(zip(x["vals"], ys)):
        u = appear(t, starts[i], 0.8)
        ax.barh([y], [v * u], color=PAL[i % len(PAL)], height=0.62, alpha=0.9)
        if u > 0:
            lab = fmt(v) + (f" {s['unit']}" if s.get("unit") else "")
            ax.text(v * u + (0.01 * span if v >= 0 else -0.01 * span), y, lab, va="center",
                    ha="left" if v >= 0 else "right", fontsize=14, color=FG, alpha=u)


def prep_summary(s):
    kw = min(5.2, max(vis_len(r["key"]) for r in s["rows"]) * CW * 26 * 1.22 + 0.3)
    tx = 1.0 + kw + 0.4
    size = 21
    while size > 16 and any(len(wrap(r["text"], 15.2 - tx, size)) > 2 for r in s["rows"]):
        size -= 1
    rows = [(r["key"], fit(r["text"], 15.2 - tx, size, size, 2)[0]) for r in s["rows"]]
    return {"rows": rows, "tx": tx, "size": size}


def r_summary(fig, c, s, x, t, T):
    starts = stagger(len(x["rows"]), T)
    y = 7.2
    for i, (k, v) in enumerate(x["rows"]):
        a = appear(t, starts[i], 0.6)
        c.text(1.0, y, k, fontsize=26, color=PAL[i % len(PAL)], weight="bold", va="top", alpha=a)
        c.text(x["tx"], y - 0.05, v, fontsize=x["size"], va="top", alpha=a, linespacing=1.45)
        y -= max(1, v.count("\n") + 1) * line_h(x["size"]) + 0.45


RENDERERS = {
    "title": (prep_title, r_title), "statement": (prep_statement, r_statement), "bullets": (prep_bullets, r_bullets),
    "flow": (prep_flow, r_flow), "compare": (prep_compare, r_compare), "formula": (prep_formula, r_formula),
    "plot": (prep_plot, r_plot), "gradient_descent": (prep_gd, r_gd), "neuron": (prep_neuron, r_neuron),
    "vectors": (prep_vectors, r_vectors), "bars": (prep_bars, r_bars), "summary": (prep_summary, r_summary),
}


def scene_duration(audio_len):
    return max(2.5, AUDIO_DELAY + audio_len + TAIL) if audio_len > 0 else 2.5


def draw_frame(fig, s, x, subs, t, T):
    fig.clf()
    c = canvas(fig)
    if s["type"] != "title":
        draw_heading(c, s, t)
    if s.get("_asset"):
        from kit import image
        image(c, s["_asset"]["id"], 0.7, 2.0, 14.6, 5.65, alpha=appear(t, 0.1))
    else:
        RENDERERS[s["type"]][1](fig, c, s, x, t, T)
    draw_subtitles(c, subs, t)


def encode_scene(draw, T, audio_path, out_path, first, last, crf):
    """Render frames with draw(fig, t) and encode them with the scene audio into one MP4 segment.
    Used by the template renderer and by the full-gen harness."""
    n = int(round(T * FPS))
    vf = []
    if first:
        vf.append("fade=in:0:10")
    if last:
        vf.append(f"fade=out:{max(0, n - 14)}:14")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W_PX}x{H_PX}",
           "-r", str(FPS), "-i", "-"]
    if audio_path:
        cmd += ["-i", audio_path]
        af = f"[1:a]aresample=44100,aformat=channel_layouts=stereo,adelay={int(AUDIO_DELAY * 1000)}:all=1,apad[a]"
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        af = "[1:a]anull[a]"
    cmd += ["-filter_complex", af, "-map", "0:v", "-map", "[a]", "-t", f"{T:.3f}"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", out_path]
    fig = plt.figure(figsize=(W_PX / DPI, H_PX / DPI), dpi=DPI)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for i in range(n):
            draw(fig, i / FPS)
            fig.canvas.draw()
            p.stdin.write(fig.canvas.buffer_rgba().tobytes())
    finally:
        p.stdin.close()
        rc = p.wait()
        plt.close(fig)
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed (code {rc})")
    return out_path, T


def render_scene(args):
    """Render one template scene with its audio to an MP4 segment. Runs in a worker process."""
    s, audio_path, audio_len, out_path, first, last, crf = args
    T = scene_duration(audio_len)
    if s.get("_asset"):
        from kit import configure_assets
        configure_assets([s["_asset"]])
    x = None if s.get("_asset") else RENDERERS[s["type"]][0](s)
    subs = build_subtitles(s["narration"], audio_len)
    return encode_scene(lambda fig, t: draw_frame(fig, s, x, subs, t, T), T, audio_path, out_path, first, last, crf)
