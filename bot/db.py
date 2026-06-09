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

            CREATE TABLE IF NOT EXISTS alarms (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                sensor    TEXT    NOT NULL,
                kind      TEXT    NOT NULL,
                message   TEXT    NOT NULL,
                ts        INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_alarms_sensor_ts
                ON alarms(sensor, ts);

            CREATE TABLE IF NOT EXISTS dm_registered (
                chat_id      INTEGER PRIMARY KEY,
                registered_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS digest_subscriptions (
                user_id  INTEGER NOT NULL,
                sensor   TEXT    NOT NULL,
                PRIMARY KEY (user_id, sensor)
            );

            CREATE TABLE IF NOT EXISTS readings_archive (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                sensor    TEXT    NOT NULL,
                value     REAL    NOT NULL,
                ts        INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_archive_sensor_ts
                ON readings_archive(sensor, ts);
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


def insert_alarm(sensor: str, kind: str, message: str):
    with _conn() as con:
        con.execute(
            "INSERT INTO alarms (sensor, kind, message, ts) VALUES (?, ?, ?, ?)",
            (sensor, kind, message, int(time.time())),
        )


def get_last_alarms(sensor: Optional[str] = None, n: int = 1) -> list[sqlite3.Row]:
    with _conn() as con:
        if sensor:
            return con.execute(
                "SELECT sensor, kind, message, ts FROM alarms WHERE sensor=? ORDER BY ts DESC LIMIT ?",
                (sensor, n),
            ).fetchall()
        return con.execute(
            "SELECT sensor, kind, message, ts FROM alarms ORDER BY ts DESC LIMIT ?",
            (n,),
        ).fetchall()


def has_threshold_alarm_since(sensor: str, since_ts: int) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM alarms WHERE sensor=? AND kind='threshold' AND ts>=? LIMIT 1",
            (sensor, since_ts),
        ).fetchone()
        return row is not None


def forget_sensor(sensor: str):
    with _conn() as con:
        con.execute("DELETE FROM readings WHERE sensor=?", (sensor,))
        con.execute("DELETE FROM readings_archive WHERE sensor=?", (sensor,))
        con.execute("DELETE FROM alarms WHERE sensor=?", (sensor,))
        con.execute("DELETE FROM thresholds WHERE sensor=?", (sensor,))
        con.execute("DELETE FROM silenced WHERE sensor=?", (sensor,))


def register_dm(chat_id: int):
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO dm_registered (chat_id, registered_at) VALUES (?, ?)",
            (chat_id, int(time.time())),
        )


def is_dm_registered(chat_id: int) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT chat_id FROM dm_registered WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return row is not None


def get_all_dm_registered() -> list[int]:
    with _conn() as con:
        rows = con.execute("SELECT chat_id FROM dm_registered").fetchall()
        return [r["chat_id"] for r in rows]


def subscribe_digest(user_id: int, sensor: str):
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO digest_subscriptions (user_id, sensor) VALUES (?, ?)",
            (user_id, sensor),
        )


def unsubscribe_digest(user_id: int, sensor: str):
    with _conn() as con:
        con.execute(
            "DELETE FROM digest_subscriptions WHERE user_id=? AND sensor=?",
            (user_id, sensor),
        )


def get_digest_subscriptions(user_id: int) -> list[str]:
    with _conn() as con:
        rows = con.execute(
            "SELECT sensor FROM digest_subscriptions WHERE user_id=? ORDER BY sensor",
            (user_id,),
        ).fetchall()
        return [r["sensor"] for r in rows]


def archive_old_readings(retention_days: int):
    cutoff = int(time.time()) - retention_days * 86400
    with _conn() as con:
        con.execute(
            "INSERT INTO readings_archive (sensor, value, ts) "
            "SELECT sensor, value, ts FROM readings WHERE ts < ?",
            (cutoff,),
        )
        con.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
