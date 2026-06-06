import io
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

from . import db

_COLORS = ["#2196F3", "#FF9800", "#9C27B0", "#009688", "#E91E63", "#795548"]
_STYLES = ["-", "--", "-.", ":"]


def build(sensors: list[tuple[str, Optional[float], str]], hours: int = 8) -> io.BytesIO:
    fig, ax = plt.subplots(figsize=(10, 4))
    title_lines = []
    any_data = False

    for i, (name, threshold, unit) in enumerate(sensors):
        rows = db.get_history(name, seconds=hours * 3600)
        times = [datetime.fromtimestamp(r["ts"]) for r in rows]
        values = [r["value"] for r in rows]
        color = _COLORS[i % len(_COLORS)]
        style = _STYLES[i % len(_STYLES)]

        if values:
            any_data = True
            vmin, vmax = min(values), max(values)
            t_from = datetime.fromtimestamp(rows[0]["ts"]).strftime("%d/%m %H:%M")
            t_to   = datetime.fromtimestamp(rows[-1]["ts"]).strftime("%d/%m %H:%M")
            title_lines.append(f"{name}   {vmin:.1f}/{vmax:.1f}   {t_from} - {t_to}   ({len(rows)})")
            ax.plot(times, values, color=color, linestyle=style, linewidth=1.5)
            idx_min = values.index(min(values))
            idx_max = values.index(max(values))
            ax.plot(times[idx_min], values[idx_min], "o", color="#4CAF50", markersize=6, zorder=5)
            ax.plot(times[idx_max], values[idx_max], "o", color="#F44336", markersize=6, zorder=5)
        else:
            title_lines.append(f"{name}   no data")

    if not any_data:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes,
                ha="center", va="center", fontsize=12, color="gray")

    units = list({u for _, _, u in sensors if u})
    ax.set_ylabel(units[0] if len(units) == 1 else "")
    ax.set_title("\n".join(title_lines), fontsize=9, loc="left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf
