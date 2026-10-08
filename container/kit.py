"""Helper API for generated animation code.  Generated code starts with `from kit import *`.

Canvas: c = canvas(fig) gives an axes with x 0..16, y 0..9 (1280x720 px, 80 px per unit).
The band y < 1.6 is reserved for subtitles; the harness draws them.
"""
import math  # noqa: F401  (re-exported)

import numpy as np  # noqa: F401
import matplotlib  # noqa: F401
import matplotlib.pyplot as plt  # noqa: F401
from matplotlib.patches import (Arc, Circle, Ellipse, FancyArrowPatch, FancyBboxPatch, Polygon,  # noqa: F401
                                Rectangle, Wedge)

from scenes import (BG, BLUE, FG, GREY, GRN, ORANGE, PAL, PANEL, PURPLE, RED, YEL,  # noqa: F401
                    appear, canvas, fit, fmt, line_h, mfmt, sm, stagger, style_axes, wrap)

SUB_TOP = 1.6        # keep content above this y (subtitles below)
CONTENT_TOP = 8.85   # keep content below this y


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
