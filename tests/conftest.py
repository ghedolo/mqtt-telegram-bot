"""Shared pytest fixtures.

`temp_db` points bot.db at a throwaway SQLite file under tmp_path and runs
the real schema init, so db tests never touch the production data/sensors.db.
"""
import pytest

from bot import db as db_module


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "sensors.db"
    # bot.db reads DB_PATH at call time inside _conn()/get_db_stats(), so
    # patching the module global is enough to redirect every query.
    monkeypatch.setattr(db_module, "DB_PATH", str(dbfile))
    db_module.init()
    return db_module
