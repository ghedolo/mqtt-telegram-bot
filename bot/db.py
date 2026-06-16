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
                value     REAL,
                low       REAL
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

            CREATE TABLE IF NOT EXISTS user_activity (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                full_name  TEXT,
                last_seen  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS digest_subscriptions (
                user_id  INTEGER NOT NULL,
                sensor   TEXT    NOT NULL,
                PRIMARY KEY (user_id, sensor)
            );

            CREATE TABLE IF NOT EXISTS mutes (
                chat_id  INTEGER NOT NULL,
                sensor   TEXT    NOT NULL,
                until_ts INTEGER NOT NULL,
                PRIMARY KEY (chat_id, sensor)
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
        try:
            con.execute("ALTER TABLE thresholds ADD COLUMN low REAL")
        except Exception:
            pass
        # migrate: drop NOT NULL on thresholds.value if still present
        col_info = con.execute("PRAGMA table_info(thresholds)").fetchall()
        value_col = next((c for c in col_info if c["name"] == "value"), None)
        if value_col and value_col["notnull"]:
            con.executescript("""
                CREATE TABLE thresholds_new (
                    sensor TEXT PRIMARY KEY,
                    value  REAL,
                    low    REAL
                );
                INSERT INTO thresholds_new (sensor, value)
                    SELECT sensor, value FROM thresholds;
                DROP TABLE thresholds;
                ALTER TABLE thresholds_new RENAME TO thresholds;
            """)

    # write probe: fail fast at startup if the DB file is not writable
    # (e.g. wrong volume ownership) instead of failing silently later
    with _conn() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS _write_probe (ts INTEGER)"
        )
        con.execute("INSERT INTO _write_probe (ts) VALUES (?)", (int(time.time()),))
        con.execute("DELETE FROM _write_probe")


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
            "INSERT INTO thresholds (sensor, value) VALUES (?, ?) "
            "ON CONFLICT(sensor) DO UPDATE SET value=excluded.value",
            (sensor, value),
        )


def clear_threshold(sensor: str):
    with _conn() as con:
        con.execute("UPDATE thresholds SET value=NULL WHERE sensor=?", (sensor,))
        con.execute(
            "DELETE FROM thresholds WHERE sensor=? AND value IS NULL AND low IS NULL",
            (sensor,),
        )


def clear_threshold_low(sensor: str):
    with _conn() as con:
        con.execute("UPDATE thresholds SET low=NULL WHERE sensor=?", (sensor,))
        con.execute(
            "DELETE FROM thresholds WHERE sensor=? AND value IS NULL AND low IS NULL",
            (sensor,),
        )


def set_threshold_low(sensor: str, value: float):
    with _conn() as con:
        con.execute(
            "INSERT INTO thresholds (sensor, low) VALUES (?, ?) "
            "ON CONFLICT(sensor) DO UPDATE SET low=excluded.low",
            (sensor, value),
        )


def get_threshold(sensor: str) -> Optional[float]:
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM thresholds WHERE sensor=?", (sensor,)
        ).fetchone()
        return row["value"] if row else None


def get_threshold_low(sensor: str) -> Optional[float]:
    with _conn() as con:
        row = con.execute(
            "SELECT low FROM thresholds WHERE sensor=?", (sensor,)
        ).fetchone()
        return row["low"] if row else None


def get_all_thresholds() -> dict[str, float]:
    with _conn() as con:
        rows = con.execute("SELECT sensor, value FROM thresholds WHERE value IS NOT NULL").fetchall()
        return {r["sensor"]: r["value"] for r in rows}


def get_all_thresholds_low() -> dict[str, float]:
    with _conn() as con:
        rows = con.execute("SELECT sensor, low FROM thresholds WHERE low IS NOT NULL").fetchall()
        return {r["sensor"]: r["low"] for r in rows}


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


def mute_sensor(chat_id: int, sensor: str, until_ts: int):
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO mutes (chat_id, sensor, until_ts) VALUES (?, ?, ?)",
            (chat_id, sensor, until_ts),
        )


def unmute_sensor(chat_id: int, sensor: str):
    with _conn() as con:
        con.execute(
            "DELETE FROM mutes WHERE chat_id=? AND sensor=?", (chat_id, sensor)
        )


def is_muted(chat_id: int, sensor: str) -> bool:
    now = int(time.time())
    with _conn() as con:
        con.execute("DELETE FROM mutes WHERE until_ts<=?", (now,))
        row = con.execute(
            "SELECT 1 FROM mutes WHERE chat_id=? AND sensor=? AND until_ts>?",
            (chat_id, sensor, now),
        ).fetchone()
        return row is not None


def get_active_mutes(chat_id: int) -> list[sqlite3.Row]:
    now = int(time.time())
    with _conn() as con:
        con.execute("DELETE FROM mutes WHERE until_ts<=?", (now,))
        return con.execute(
            "SELECT sensor, until_ts FROM mutes WHERE chat_id=? AND until_ts>? "
            "ORDER BY sensor",
            (chat_id, now),
        ).fetchall()


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


def get_alarms_since(sensors: list[str], since_ts: int) -> list[sqlite3.Row]:
    """All alarm events for the given sensors with ts >= since_ts, newest first."""
    if not sensors:
        return []
    with _conn() as con:
        ph = ",".join("?" * len(sensors))
        return con.execute(
            f"SELECT sensor, kind, message, ts FROM alarms "
            f"WHERE sensor IN ({ph}) AND ts>=? ORDER BY ts DESC",
            (*sensors, since_ts),
        ).fetchall()


def has_threshold_alarm_since(sensor: str, since_ts: int) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM alarms WHERE sensor=? AND kind IN ('ALARM','ALARM_LOW') AND ts>=? LIMIT 1",
            (sensor, since_ts),
        ).fetchone()
        return row is not None


def forget_sensor(sensor: str):
    with _conn() as con:
        con.execute(
            "INSERT INTO readings_archive (sensor, value, ts) "
            "SELECT sensor, value, ts FROM readings WHERE sensor=?",
            (sensor,),
        )
        con.execute("DELETE FROM readings WHERE sensor=?", (sensor,))
        con.execute("DELETE FROM alarms WHERE sensor=?", (sensor,))
        con.execute("DELETE FROM silenced WHERE sensor=?", (sensor,))


def forget_device(sensor_names: list, device_key: str):
    with _conn() as con:
        for sensor in sensor_names:
            con.execute(
                "INSERT INTO readings_archive (sensor, value, ts) "
                "SELECT sensor, value, ts FROM readings WHERE sensor=?",
                (sensor,),
            )
            con.execute("DELETE FROM readings WHERE sensor=?", (sensor,))
            con.execute("DELETE FROM alarms WHERE sensor=?", (sensor,))
            con.execute("DELETE FROM thresholds WHERE sensor=?", (sensor,))
        con.execute("DELETE FROM silenced WHERE sensor=?", (device_key,))
        con.execute("DELETE FROM alarms WHERE sensor=?", (device_key,))


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


def record_activity(user_id: int, username: Optional[str], full_name: Optional[str]):
    with _conn() as con:
        con.execute(
            "INSERT INTO user_activity (user_id, username, full_name, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, full_name=excluded.full_name, "
            "last_seen=excluded.last_seen",
            (user_id, username, full_name, int(time.time())),
        )


def get_all_activity() -> list[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT user_id, username, full_name, last_seen "
            "FROM user_activity ORDER BY last_seen DESC"
        ).fetchall()


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
