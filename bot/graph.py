import io
import time
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

from . import db


def build(sensor: str, threshold: Optional[float] = None, unit: str = "") -> io.BytesIO:
    rows = db.get_history(sensor, seconds=8 * 3600)

    times = [datetime.fromtimestamp(r["ts"]) for r in rows]
    values = [r["value"] for r in rows]

    fig, ax = plt.subplots(figsize=(10, 4))

    if values:
        ax.plot(times, values, color="#2196F3", linewidth=1.5, label=unit or "value")
        if threshold is not None:
            ax.axhline(y=threshold, color="#F44336", linewidth=1.2,
                       linestyle="--", label=f"Alarm: {threshold}{' ' + unit if unit else ''}")
        ax.legend(fontsize=9)
    else:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes,
                ha="center", va="center", fontsize=12, color="gray")

    last_ts = rows[-1]["ts"] if rows else None
    last_val = rows[-1]["value"] if rows else None
    subtitle = ""
    if last_ts:
        u = f" {unit}" if unit else ""
        subtitle = f"Last: {last_val:.1f}{u}  @  {datetime.fromtimestamp(last_ts).strftime('%H:%M:%S')}"

    ax.set_title(f"Sensor: {sensor}  —  last 8h\n{subtitle}", fontsize=11)
    ax.set_ylabel(unit if unit else "")
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
