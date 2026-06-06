import io
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

from . import db


def build(sensor: str, threshold: Optional[float] = None, unit: str = "", hours: int = 8) -> io.BytesIO:
    rows = db.get_history(sensor, seconds=hours * 3600)

    times = [datetime.fromtimestamp(r["ts"]) for r in rows]
    values = [r["value"] for r in rows]

    fig, ax = plt.subplots(figsize=(10, 4))

    if rows:
        t_from = datetime.fromtimestamp(rows[0]["ts"]).strftime("%d/%m %H:%M")
        t_to   = datetime.fromtimestamp(rows[-1]["ts"]).strftime("%d/%m %H:%M")
        range_str = f"{sensor}   {t_from} - {t_to}"
    else:
        range_str = f"{sensor}   no data"

    if values:
        ax.plot(times, values, color="#2196F3", linewidth=1.5)
    else:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes,
                ha="center", va="center", fontsize=12, color="gray")

    ax.set_title(range_str, fontsize=11, loc="left")
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
