import sqlite3
import time
from contextlib import contextmanager
from typing import Optional


DB_PATH = "data/sensors.db"


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    import os
    os.makedirs("data", exist_ok=True)
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS readings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                sensor    TEXT    NOT NULL,
                value     REAL    NOT NULL,
                ts        INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_readings_sensor_ts
                ON readings(sensor, ts);

            CREATE TABLE IF NOT EXISTS thresholds (
                sensor    TEXT PRIMARY KEY,
                value     REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS silenced (
                sensor    TEXT PRIMARY KEY,
                silenced_at INTEGER NOT NULL
            );
        """)


def insert_reading(sensor: str, value: float, ts: Optional[int] = None):
    if ts is None:
        ts = int(time.time())
    with _conn() as con:
        con.execute(
            "INSERT INTO readings (sensor, value, ts) VALUES (?, ?, ?)",
            (sensor, value, ts),
        )


def get_latest(sensor: str) -> Optional[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT value, ts FROM readings WHERE sensor=? ORDER BY ts DESC LIMIT 1",
            (sensor,),
        ).fetchone()


def get_history(sensor: str, seconds: int = 8 * 3600) -> list[sqlite3.Row]:
    since = int(time.time()) - seconds
    with _conn() as con:
        return con.execute(
            "SELECT value, ts FROM readings WHERE sensor=? AND ts>=? ORDER BY ts ASC",
            (sensor, since),
        ).fetchall()


def get_all_latest() -> list[sqlite3.Row]:
    with _conn() as con:
        return con.execute("""
            SELECT r.sensor, r.value, r.ts
            FROM readings r
            INNER JOIN (
                SELECT sensor, MAX(ts) AS max_ts FROM readings GROUP BY sensor
            ) latest ON r.sensor = latest.sensor AND r.ts = latest.max_ts
        """).fetchall()


def set_threshold(sensor: str, value: float):
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO thresholds (sensor, value) VALUES (?, ?)",
            (sensor, value),
        )


def get_threshold(sensor: str) -> Optional[float]:
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM thresholds WHERE sensor=?", (sensor,)
        ).fetchone()
        return row["value"] if row else None


def get_all_thresholds() -> dict[str, float]:
    with _conn() as con:
        rows = con.execute("SELECT sensor, value FROM thresholds").fetchall()
        return {r["sensor"]: r["value"] for r in rows}


def silence_sensor(sensor: str):
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO silenced (sensor, silenced_at) VALUES (?, ?)",
            (sensor, int(time.time())),
        )


def unsilence_sensor(sensor: str):
    with _conn() as con:
        con.execute("DELETE FROM silenced WHERE sensor=?", (sensor,))


def is_silenced(sensor: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT sensor FROM silenced WHERE sensor=?", (sensor,)
        ).fetchone()
        return row is not None


def purge_old_readings(retention_days: int):
    cutoff = int(time.time()) - retention_days * 86400
    with _conn() as con:
        con.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
