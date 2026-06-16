import io
import math
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.transforms as mtransforms
from datetime import datetime

from . import db

_COLORS = ["#004c6d", "#4c91ad", "#8cc5a6", "#e87c47", "#c83c0c"]
_STYLES = ["-", "--", "-.", ":"]
_INDICATORS = {"-": "─────", "--": "╌╌╌╌╌", "-.": "─·─·─", ":": "·····"}


def build(
    sensors: list[tuple[str, Optional[float], str, Optional[float], Optional[float]]],
    hours: int = 8,
) -> io.BytesIO:
    n = len(sensors)
    line_h = 0.055  # figure fraction per title line
    top_margin = n * line_h + 0.02

    fig, ax = plt.subplots(figsize=(10, 4))
    fig.subplots_adjust(top=1.0 - top_margin, bottom=0.15, left=0.09, right=0.97)

    any_data = False
    max_name_len = max((len(name) for name, *_ in sensors), default=8)

    # blended transform: x in data coords, y in axes fraction (edge markers)
    edge_tf = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)

    for i, (name, threshold, unit, vmin_b, vmax_b) in enumerate(sensors):
        rows = db.get_history(name, seconds=hours * 3600)
        color = _COLORS[i % len(_COLORS)]
        style = _STYLES[i // len(_COLORS)]
        indicator = _INDICATORS.get(style, "─────")
        padded = name.ljust(max_name_len)

        times, line_vals, in_vals = [], [], []
        hi_times, lo_times = [], []  # discarded above / below range
        for r in rows:
            t = datetime.fromtimestamp(r["ts"])
            v = r["value"]
            times.append(t)
            if vmax_b is not None and v > vmax_b:
                hi_times.append(t)
                line_vals.append(math.nan)   # break line at glitch
            elif vmin_b is not None and v < vmin_b:
                lo_times.append(t)
                line_vals.append(math.nan)
            else:
                line_vals.append(v)
                in_vals.append((t, v))

        if in_vals:
            any_data = True
            vals_only = [v for _, v in in_vals]
            vmin, vmax = min(vals_only), max(vals_only)
            t_from = datetime.fromtimestamp(rows[0]["ts"]).strftime("%d/%m %H:%M")
            t_to   = datetime.fromtimestamp(rows[-1]["ts"]).strftime("%d/%m %H:%M")
            dropped = len(hi_times) + len(lo_times)
            extra = f", {dropped} fuori scala" if dropped else ""
            stats = f"{vmin:5.1f}/{vmax:<5.1f}  {t_from} – {t_to}  ({len(rows)}{extra})"
            ax.plot(times, line_vals, color=color, linestyle=style, linewidth=1.5)
            t_min, v_min = min(in_vals, key=lambda p: p[1])
            t_max, v_max = max(in_vals, key=lambda p: p[1])
            ax.plot(t_min, v_min, "o", color="#4CAF50", markersize=6, zorder=5)
            ax.plot(t_max, v_max, "o", color="#F44336", markersize=6, zorder=5)
            # tiny edge markers at the time of each discarded reading
            if hi_times:
                ax.plot(hi_times, [0.985] * len(hi_times), "v", transform=edge_tf,
                        color="#F44336", markersize=3, clip_on=False, zorder=6)
            if lo_times:
                ax.plot(lo_times, [0.015] * len(lo_times), "^", transform=edge_tf,
                        color="#2196F3", markersize=3, clip_on=False, zorder=6)
        else:
            stats = "no data"

        y = 1.0 - 0.01 - i * line_h
        fig.text(0.09, y, f"{padded}  {indicator}  {stats}",
                 fontsize=9, fontfamily="monospace", color=color, va="top")

    if not any_data:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes,
                ha="center", va="center", fontsize=12, color="gray")

    units = list({s[2] for s in sensors if s[2]})
    ax.set_ylabel(units[0] if len(units) == 1 else "")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf
