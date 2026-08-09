"""Tests for bot.main wiring that is worth pinning in isolation — currently the
command-trace file setup, whose whole point is to never crash the bot over a bad
path (the container mounts /app read-only)."""
import logging
from types import SimpleNamespace

import pytest

from bot import main


@pytest.fixture(autouse=True)
def _clean_trace_logger():
    """Detach anything _setup_cmd_trace attaches, so handlers (and open files)
    don't leak between tests."""
    lg = logging.getLogger("bot.cmdtrace")
    yield
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()
    lg.propagate = True
    lg.setLevel(logging.NOTSET)


def _cfg(trace_cmd, path="x.log"):
    return SimpleNamespace(trace_cmd=trace_cmd, trace_cmd_file=path)


def test_digest_recipients_survives_a_db_error(monkeypatch, caplog):
    # this read runs inside the digest task's while-loop: letting it raise killed
    # the task, and the daily digest then never fired again for the life of the
    # process, with the failure only surfacing at shutdown
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(main.db, "get_all_dm_registered", boom)
    with caplog.at_level(logging.ERROR):
        assert main._digest_recipients() == []
    assert any("digest recipients" in r.message for r in caplog.records)


def test_digest_recipients_passes_rows_through(monkeypatch):
    monkeypatch.setattr(main.db, "get_all_dm_registered", lambda: [7, 9])
    assert main._digest_recipients() == [7, 9]


def test_trace_off_attaches_nothing(tmp_path):
    assert main._setup_cmd_trace(_cfg(False)) is False
    assert logging.getLogger("bot.cmdtrace").handlers == []


def test_trace_creates_parent_and_writes(tmp_path):
    target = tmp_path / "data" / "cmdtrace.log"   # parent does not exist yet
    assert main._setup_cmd_trace(_cfg(True, str(target))) is True
    assert target.parent.is_dir()                 # created for us

    logging.getLogger("bot.cmdtrace").info("→ 7 | /get T")
    assert "→ 7 | /get T" in target.read_text()   # actually reaches the file


def test_trace_failopen_does_not_raise(tmp_path, caplog):
    # Parent path is a regular file, so makedirs/open must fail. The bot must
    # survive it: return False, warn, attach nothing — never raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    bad = blocker / "cmdtrace.log"

    with caplog.at_level(logging.WARNING):
        assert main._setup_cmd_trace(_cfg(True, str(bad))) is False

    assert logging.getLogger("bot.cmdtrace").handlers == []
    assert any("Command trace disabled" in r.message for r in caplog.records)
