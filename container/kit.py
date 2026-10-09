"""Helper API for generated animation code.  Generated code starts with `from kit import *`.

Canvas: c = canvas(fig) gives an axes with x 0..16, y 0..9 (1280x720 px, 80 px per unit).
The band y < 1.6 is reserved for subtitles; the harness draws them.
"""
import math  # noqa: F401  (re-exported)
from functools import lru_cache
from pathlib import Path

import numpy as np  # noqa: F401
import matplotlib  # noqa: F401
import matplotlib.pyplot as plt  # noqa: F401
from matplotlib.patches import (Arc, Circle, Ellipse, FancyArrowPatch, FancyBboxPatch, Polygon,  # noqa: F401
                                Rectangle, Wedge)

from scenes import (BG, BLUE, FG, GREY, GRN, ORANGE, PAL, PANEL, PURPLE, RED, YEL,  # noqa: F401
                    appear, canvas, fit, fmt, line_h, mfmt, sm, stagger, style_axes, wrap)

SUB_TOP = 1.6        # keep content above this y (subtitles below)
CONTENT_TOP = 8.85   # keep content below this y
_ASSETS = {}
_USED_ASSETS = set()


def configure_assets(assets):
    """Called by the trusted harness before loading generated scene code."""
    _ASSETS.clear()
    _ASSETS.update({a["id"]: a for a in assets})
    _USED_ASSETS.clear()
    _pixels.cache_clear()


@lru_cache(maxsize=8)
def _pixels(asset_id):
    from PIL import Image
    path = Path(_ASSETS[asset_id]["path"])
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def used_assets():
    return sorted(_USED_ASSETS)


def clear_asset_usage():
    _USED_ASSETS.clear()


def image(c, asset_id, x, y, w, h, alpha=1.0, credit=True):
    """Fit an approved raster asset inside a canvas rectangle; preserve its proportions."""
    if asset_id not in _ASSETS:
        raise ValueError(f"unknown visual asset: {asset_id}")
    pixels = _pixels(asset_id)
    ratio = pixels.shape[1] / pixels.shape[0]
    draw_w = min(w, h * ratio)
    draw_h = draw_w / ratio
    left, bottom = x + (w - draw_w) / 2, y + (h - draw_h) / 2
    xlim, ylim = c.get_xlim(), c.get_ylim()
    artist = c.imshow(pixels, extent=(left, left + draw_w, bottom, bottom + draw_h),
                      aspect="auto", interpolation="bilinear", alpha=max(0, min(1, alpha)), zorder=1)
    c.set_xlim(xlim); c.set_ylim(ylim)
    if alpha > 0.1:
        _USED_ASSETS.add(asset_id)
    if credit and _ASSETS[asset_id].get("credit"):
        label = "AI-иллюстрация" if _ASSETS[asset_id].get("kind") == "generated" else (
            "Wikimedia Commons" if _ASSETS[asset_id].get("kind") == "web" else "Референс из сообщения")
        c.text(left + 0.12, bottom + 0.12, label, fontsize=12, color=FG, va="bottom", alpha=alpha,
               bbox=dict(facecolor=BG, edgecolor="none", alpha=0.8, pad=3), zorder=3)
    return artist


def lerp(a, b, u):
    return a + (b - a) * u


def heading(c, text, t, size=30):
    """Scene heading at the top left, fades in."""
    txt, sz = fit(text, 14.8, size, 20, 1)
    c.text(0.6, 8.3, txt, ha="left", va="center", fontsize=sz, weight="bold", color=FG, alpha=appear(t, 0, 0.6))


def arrow(c, x0, y0, x1, y1, color=FG, lw=2.0, alpha=1.0, rad=0.0):
    c.annotate("", xy=(x1, y1), xytext=(x0, y0),
               arrowprops=dict(arrowstyle="-|>", color=color, lw=lw, alpha=alpha,
                               connectionstyle=f"arc3,rad={rad}"))


def text(c, x, y, s, size=22, color=FG, alpha=1.0, ha="center", va="center", width=None, max_lines=3, **kw):
    """Text that wraps to `width` canvas units (and shrinks if needed) so it stays inside its box."""
    if width:
        s, size = fit(s, width, size, max(12, size - 10), max_lines)
    return c.text(x, y, s, fontsize=size, color=color, alpha=alpha, ha=ha, va=va, linespacing=1.35, **kw)


def box(c, x, y, w, h, color=GREY, fill=PANEL, lw=1.6, alpha=1.0, r=0.18):
    """Rounded box with its lower-left corner at (x, y)."""
    c.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0.02,rounding_size={r}",
                               fc=fill, ec=color, lw=lw, alpha=alpha))


def draw_partial(ax, xs, ys, u, **kw):
    """Plot the first fraction u (0..1) of a curve: the 'drawing' animation."""
    k = max(2, int(len(xs) * min(1.0, max(0.0, u))))
    return ax.plot(xs[:k], ys[:k], **kw)
