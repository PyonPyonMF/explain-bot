"""Runs generated animation code. This process runs as the unprivileged `sandbox` user with no API keys.

usage: python gen_harness.py spec.json
spec: {"code": path, "mode": "test"|"full", "out_dir": dir, "only": [scene indexes] or null,
       "scenes": [{"T": seconds, "audio": path|null, "audio_len": s, "narration": str, "title": str}],
       "fractions": [0.35, 0.85], "crf": 23}
Writes out_dir/result-<mode>-<first index>.json.
"""
import importlib.util
import json
import os
import sys
import time
import traceback

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import scenes as S  # noqa: E402

W, H = S.W_PX, S.H_PX
SUB_BAND_PX = 1.6 * 80  # subtitle band height in pixels


def overlay(fig):
    """Transparent full-frame axes on top, for subtitles (generated code may add its own axes first)."""
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 16); ax.set_ylim(0, 9); ax.axis("off"); ax.patch.set_visible(False)
    return ax


def short_tb(limit=12):
    lines = traceback.format_exc().strip().splitlines()
    keep = [ln for ln in lines if "gen_harness.py" not in ln]
    return "\n".join(keep[-limit:])


def load(path):
    spec = importlib.util.spec_from_file_location("video_code", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fallback_draw(fig, t, T, sc):
    """Plain text card for a scene whose code failed."""
    c = S.canvas(fig)
    first = (sc.get("narration") or sc.get("title") or "").split(". ")[0]
    txt, size = S.fit(first, 13.5, 34, 20, 4)
    c.text(8, 5.2, txt, ha="center", va="center", fontsize=size, color=S.FG, alpha=S.appear(t, 0.1))


def lint(fig):
    """Find text outside the frame, in the subtitle band, or on top of other text."""
    issues, boxes = [], []
    r = fig.canvas.get_renderer()
    for ax in fig.axes:
        for t in ax.texts + [ax.title, ax.xaxis.label, ax.yaxis.label]:
            if not t.get_visible() or not t.get_text().strip() or (t.get_alpha() is not None and t.get_alpha() < 0.3):
                continue
            try:
                bb = t.get_window_extent(r)
            except Exception:
                continue
            label = t.get_text().strip().replace("\n", " ")[:40]
            if bb.x0 < -2 or bb.x1 > W + 2 or bb.y0 < -2 or bb.y1 > H + 2:
                issues.append(f"text outside the frame: '{label}'")
            elif bb.y0 < SUB_BAND_PX - 4:
                issues.append(f"text in the subtitle band (y < 1.6): '{label}'")
            boxes.append((bb, label))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i][0], boxes[j][0]
            w = min(a.x1, b.x1) - max(a.x0, b.x0)
            h = min(a.y1, b.y1) - max(a.y0, b.y0)
            if w > 0 and h > 0:
                small = min(a.width * a.height, b.width * b.height) or 1
                if w * h / small > 0.25:
                    issues.append(f"texts overlap: '{boxes[i][1]}' and '{boxes[j][1]}'")
    return issues[:8]


def run_test(fns, spec, out):
    res = {"scenes": []}
    fig = plt.figure(figsize=(W / S.DPI, H / S.DPI), dpi=S.DPI)
    # Warm-up frame: the first draw of a process loads fonts and caches and would look "slow".
    try:
        fig.clf(); fns[spec_indexes(spec)[0]](fig, 0.0, spec["scenes"][spec_indexes(spec)[0]]["T"]); fig.canvas.draw()
    except Exception:
        pass
    for k in spec_indexes(spec):
        sc, T = spec["scenes"][k], spec["scenes"][k]["T"]
        subs = S.build_subtitles(sc.get("narration", ""), sc.get("audio_len", 0))
        r = {"index": k, "frames": [], "error": None, "lint": [], "slow_ms": 0}
        for frac in spec.get("fractions", [0.35, 0.85]):
            t = T * frac
            fig.clf()
            t0 = time.time()
            try:
                fns[k](fig, t, T)
                fig.canvas.draw()
            except Exception:
                r["error"] = r["error"] or short_tb()
                fig.clf()
                fallback_draw(fig, t, T, sc)
                fig.canvas.draw()
            r["slow_ms"] = max(r["slow_ms"], int((time.time() - t0) * 1000))
            if not r["error"]:
                r["lint"] += [i for i in lint(fig) if i not in r["lint"]]
            S.draw_subtitles(overlay(fig), subs, t)
            path = os.path.join(out, f"test_{k}_{int(frac * 100)}.png")
            fig.savefig(path)
            r["frames"].append({"t": round(t, 2), "path": path})
        res["scenes"].append(r)
    plt.close(fig)
    return res


def spec_indexes(spec):
    return spec.get("only") or list(range(len(spec["scenes"])))


def run_full(fns, spec, out):
    res = {"scenes": []}
    n = len(spec["scenes"])
    for k in spec_indexes(spec):
        sc, T = spec["scenes"][k], spec["scenes"][k]["T"]
        subs = S.build_subtitles(sc.get("narration", ""), sc.get("audio_len", 0))
        state = {"failed": None}

        def draw(fig, t, k=k, sc=sc, T=T, subs=subs, state=state):
            fig.clf()
            if not state["failed"]:
                try:
                    fns[k](fig, t, T)
                except Exception:
                    state["failed"] = f"t={t:.2f}: " + short_tb(6)
                    fig.clf()
            if state["failed"]:
                fallback_draw(fig, t, T, sc)
            S.draw_subtitles(overlay(fig), subs, t)

        path = os.path.join(out, f"scene_{k}.mp4")
        S.encode_scene(draw, T, sc.get("audio"), path, k == 0, k == n - 1, spec.get("crf", 23))
        res["scenes"].append({"index": k, "path": path, "T": T, "failed": state["failed"]})
    return res


def main():
    spec = json.load(open(sys.argv[1]))
    out = spec["out_dir"]
    tag = f"{spec['mode']}-{(spec.get('only') or [0])[0]}"
    result_path = os.path.join(out, f"result-{tag}.json")
    try:
        mod = load(spec["code"])
        fns = list(getattr(mod, "SCENES"))
        if len(fns) != len(spec["scenes"]):
            raise ValueError(f"SCENES has {len(fns)} functions, but the plan has {len(spec['scenes'])} scenes")
    except Exception:
        json.dump({"import_error": short_tb(15)}, open(result_path, "w"))
        return
    res = run_test(fns, spec, out) if spec["mode"] == "test" else run_full(fns, spec, out)
    json.dump(res, open(result_path, "w"))


if __name__ == "__main__":
    main()
