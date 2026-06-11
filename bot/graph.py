import io
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

from . import db

_COLORS = ["#004c6d", "#4c91ad", "#8cc5a6", "#e87c47", "#c83c0c"]
_STYLES = ["-", "--", "-.", ":"]
_INDICATORS = {"-": "─────", "--": "╌╌╌╌╌", "-.": "─·─·─", ":": "·····"}


def build(sensors: list[tuple[str, Optional[float], str]], hours: int = 8) -> io.BytesIO:
    n = len(sensors)
    line_h = 0.055  # figure fraction per title line
    top_margin = n * line_h + 0.02

    fig, ax = plt.subplots(figsize=(10, 4))
    fig.subplots_adjust(top=1.0 - top_margin, bottom=0.15, left=0.09, right=0.97)

    any_data = False
    max_name_len = max((len(name) for name, _, _ in sensors), default=8)

    for i, (name, threshold, unit) in enumerate(sensors):
        rows = db.get_history(name, seconds=hours * 3600)
        times = [datetime.fromtimestamp(r["ts"]) for r in rows]
        values = [r["value"] for r in rows]
        color = _COLORS[i % len(_COLORS)]
        style = _STYLES[i // len(_COLORS)]
        indicator = _INDICATORS.get(style, "─────")
        padded = name.ljust(max_name_len)

        if values:
            any_data = True
            vmin, vmax = min(values), max(values)
            t_from = datetime.fromtimestamp(rows[0]["ts"]).strftime("%d/%m %H:%M")
            t_to   = datetime.fromtimestamp(rows[-1]["ts"]).strftime("%d/%m %H:%M")
            stats = f"{vmin:5.1f}/{vmax:<5.1f}  {t_from} – {t_to}  ({len(rows)})"
            ax.plot(times, values, color=color, linestyle=style, linewidth=1.5)
            idx_min = values.index(min(values))
            idx_max = values.index(max(values))
            ax.plot(times[idx_min], values[idx_min], "o", color="#4CAF50", markersize=6, zorder=5)
            ax.plot(times[idx_max], values[idx_max], "o", color="#F44336", markersize=6, zorder=5)
        else:
            stats = "no data"

        y = 1.0 - 0.01 - i * line_h
        fig.text(0.09, y, f"{padded}  {indicator}  {stats}",
                 fontsize=9, fontfamily="monospace", color=color, va="top")

    if not any_data:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes,
                ha="center", va="center", fontsize=12, color="gray")

    units = list({u for _, _, u in sensors if u})
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
