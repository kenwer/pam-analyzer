"""Root logging configuration and the runtime log level switch.

The root logger always runs at DEBUG so every record is created. The file and
console handlers carry the level that decides what is written. That split is
what lets set_level() replay records the handlers dropped before the user
lowered the level.

Buffered records keep their args, so a call site that logs a large object would
pin it for the process lifetime. Every current call site logs names, counts and
timings.
"""

import logging
import logging.handlers
from collections import deque
from pathlib import Path

BUFFER_CAPACITY = 2000

_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_CONSOLE_FORMAT = "%(levelname)s %(name)s: %(message)s"

_installed: list[logging.Handler] = []
_file_handler: logging.Handler | None = None
_console_handler: logging.Handler | None = None
_buffer: RingBufferHandler | None = None
_level = logging.WARNING
_locked = False


class RingBufferHandler(logging.Handler):
    """Holds recent records so a lower level can replay the ones not yet written.

    Each record is marked on arrival with whether the handlers wrote it, so a
    later replay writes only what the log lacks.
    """

    def __init__(self, capacity: int = BUFFER_CAPACITY) -> None:
        super().__init__(level=logging.DEBUG)
        self._records: deque[logging.LogRecord] = deque(maxlen=capacity)
        self._floor = logging.CRITICAL

    def set_floor(self, level: int) -> None:
        """Tell the buffer the level new arrivals are being filtered against."""
        self._floor = level

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info:
            # A buffered traceback pins every frame's locals for the process
            # lifetime. Render it now and drop the reference.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        record.pam_written = record.levelno >= self._floor
        self._records.append(record)

    def replay_to(self, handler: logging.Handler) -> None:
        """Write every buffered record the handler's level accepts and the log lacks.

        Handler.handle does not check the level itself, so the comparison is
        made here.
        """
        for record in list(self._records):
            if not record.pam_written and record.levelno >= handler.level:
                handler.handle(record)
                record.pam_written = True


def configure(log_file: Path, level: int, *, locked: bool = False) -> None:
    """Install the file, console and buffer handlers on the root logger.

    Calling this again replaces the handlers from the previous call rather
    than stacking a second set on top.
    """
    global _file_handler, _console_handler, _buffer, _level, _locked

    root = logging.getLogger()
    for handler in _installed:
        root.removeHandler(handler)
        handler.close()
    _installed.clear()

    log_file.parent.mkdir(parents=True, exist_ok=True)

    _file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=1, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))

    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))

    _buffer = RingBufferHandler()

    _level = level
    _locked = locked
    _file_handler.setLevel(level)
    _console_handler.setLevel(level)
    _buffer.set_floor(level)

    # DEBUG on the logger itself, so every record reaches the buffer even
    # while the handlers are filtering them out. The buffer is added last, so
    # it sees the level the other two applied.
    root.setLevel(logging.DEBUG)
    for handler in (_file_handler, _console_handler, _buffer):
        root.addHandler(handler)
        _installed.append(handler)

    logging.getLogger(__name__).debug(
        "Logging to %s at %s", log_file, logging.getLevelName(level)
    )


def set_level(level: int) -> None:
    """Change what the handlers write, without restarting the app.

    Lowering the level replays the records the handlers dropped while it was
    higher, into the log file only. Raising it replays nothing, because a
    record can only be unwritten if its level is below the one in force when it
    arrived. No-op while locked by PAM_LOG_LEVEL.
    """
    global _level

    if _locked or _buffer is None or _file_handler is None or _console_handler is None:
        return

    _file_handler.setLevel(level)
    _console_handler.setLevel(level)
    _buffer.set_floor(level)
    _buffer.replay_to(_file_handler)
    _level = level


def current_level() -> int:
    return _level


def is_locked() -> bool:
    """True when PAM_LOG_LEVEL set the level, which the UI must not override."""
    return _locked
