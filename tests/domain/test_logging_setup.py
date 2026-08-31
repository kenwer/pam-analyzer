"""Behaviour of the root logging configuration and the runtime level switch."""

import logging
import sys
from pathlib import Path

import pytest

from pam_analyzer.domain import logging_setup


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Put the root logger back after each test.

    configure() attaches handlers to the process-wide root logger, so without
    this a test would leak its file handler into every later test.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    # The module still lists the handlers this test installed, and the next
    # configure() would close them a second time.
    logging_setup._installed.clear()


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "pam-analyzer.log"


def _read(log_file: Path) -> str:
    """Flush every root handler, then read the log.

    Flushes all of them rather than indexing one, because pytest's logging
    plugin attaches its own capture handler to the root logger and it is not
    guaranteed to sort after ours.
    """
    for handler in logging.getLogger().handlers:
        handler.flush()
    return log_file.read_text(encoding="utf-8")


def test_records_below_the_handler_level_are_not_written(log_file: Path):
    logging_setup.configure(log_file, logging.WARNING)
    logging.getLogger("t").debug("quiet")
    assert "quiet" not in _read(log_file)


def test_lowering_the_level_replays_records_dropped_before_the_change(log_file: Path):
    """The point of the ring buffer: a record from before the change still lands."""
    logging_setup.configure(log_file, logging.WARNING)
    logging.getLogger("t").debug("startup detail")
    assert "startup detail" not in _read(log_file)

    logging_setup.set_level(logging.DEBUG)

    assert "startup detail" in _read(log_file)


def test_lowering_the_level_writes_later_records_too(log_file: Path):
    logging_setup.configure(log_file, logging.WARNING)
    logging_setup.set_level(logging.DEBUG)
    logging.getLogger("t").debug("after the change")
    assert "after the change" in _read(log_file)


def test_raising_the_level_stops_writing_the_quieter_records(log_file: Path):
    logging_setup.configure(log_file, logging.WARNING)
    logging_setup.set_level(logging.DEBUG)
    logging_setup.set_level(logging.WARNING)
    logging.getLogger("t").debug("quiet again")
    assert "quiet again" not in _read(log_file)


def test_a_record_the_log_already_holds_is_not_replayed(log_file: Path):
    """The handler wrote this one live, so lowering the level must not write it twice.

    An earlier design replayed every buffered record at or above the new level,
    which duplicated exactly the records a reader most wants to find.
    """
    logging_setup.configure(log_file, logging.WARNING)
    logging.getLogger("t").warning("already written")

    logging_setup.set_level(logging.DEBUG)

    assert _read(log_file).count("already written") == 1


def test_a_full_cycle_does_not_duplicate_a_replayed_record(log_file: Path):
    logging_setup.configure(log_file, logging.WARNING)
    logging.getLogger("t").debug("only once")
    logging_setup.set_level(logging.DEBUG)
    logging_setup.set_level(logging.WARNING)
    logging_setup.set_level(logging.DEBUG)
    assert _read(log_file).count("only once") == 1


def test_records_from_a_quiet_period_are_replayed_when_the_level_drops_again(log_file: Path):
    logging_setup.configure(log_file, logging.WARNING)
    logging_setup.set_level(logging.DEBUG)
    logging_setup.set_level(logging.WARNING)
    logging.getLogger("t").debug("while quiet")
    logging_setup.set_level(logging.DEBUG)
    assert "while quiet" in _read(log_file)


def test_stepping_down_one_rung_replays_only_what_that_rung_admits(log_file: Path):
    """The ladder, not a switch: each rung releases its own slice of the buffer."""
    logging_setup.configure(log_file, logging.ERROR)
    log = logging.getLogger("t")
    log.warning("a warning")
    log.debug("a debug line")

    logging_setup.set_level(logging.WARNING)
    written = _read(log_file)
    assert "a warning" in written
    assert "a debug line" not in written

    logging_setup.set_level(logging.DEBUG)
    assert "a debug line" in _read(log_file)


def test_the_console_level_follows_the_switch(log_file: Path, capsys):
    logging_setup.configure(log_file, logging.WARNING)
    logging_setup.set_level(logging.DEBUG)
    logging.getLogger("t").debug("live line")
    assert "live line" in capsys.readouterr().err


def test_the_replay_does_not_reach_the_console(log_file: Path, capsys):
    """Picking a rung must not dump the whole buffer into the user's terminal."""
    logging_setup.configure(log_file, logging.WARNING)
    logging.getLogger("t").debug("buffered only")
    capsys.readouterr()

    logging_setup.set_level(logging.DEBUG)

    assert "buffered only" in _read(log_file)
    assert "buffered only" not in capsys.readouterr().err


def test_the_buffer_drops_the_oldest_record_beyond_its_capacity():
    """Tested against a recording handler, so it needs no configure() or file."""

    class Recorder(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.seen: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.seen.append(record.getMessage())

    buffer = logging_setup.RingBufferHandler(capacity=2)
    buffer.set_floor(logging.WARNING)
    for i in range(3):
        buffer.emit(logging.LogRecord("t", logging.DEBUG, __file__, i, "r%d", (i,), None))

    recorder = Recorder()
    buffer.replay_to(recorder)

    assert recorder.seen == ["r1", "r2"]


def test_the_buffer_renders_a_traceback_and_drops_the_frames():
    """A held exc_info pins every frame's locals for the process lifetime."""
    buffer = logging_setup.RingBufferHandler(capacity=2)
    buffer.set_floor(logging.WARNING)
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("t", logging.DEBUG, __file__, 1, "failed", None, sys.exc_info())

    buffer.emit(record)

    assert record.exc_info is None
    assert "ValueError: boom" in record.exc_text


def test_a_locked_configuration_ignores_the_switch(log_file: Path):
    """PAM_LOG_LEVEL wins, so the menu must not override it."""
    logging_setup.configure(log_file, logging.WARNING, locked=True)
    logging_setup.set_level(logging.DEBUG)
    assert logging_setup.is_locked() is True
    assert logging_setup.current_level() == logging.WARNING
    logging.getLogger("t").debug("still quiet")
    assert "still quiet" not in _read(log_file)


def test_configure_twice_does_not_stack_handlers(log_file: Path, tmp_path: Path):
    logging_setup.configure(log_file, logging.WARNING)
    count = len(logging.getLogger().handlers)
    logging_setup.configure(tmp_path / "logs" / "second.log", logging.WARNING)
    assert len(logging.getLogger().handlers) == count
