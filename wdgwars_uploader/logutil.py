"""Logging setup: rotating file in %APPDATA% plus an in-memory ring buffer the
tray/UI can show without opening the file."""

from __future__ import annotations

import collections
import faulthandler
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from .config import log_path

_RING: collections.deque[str] = collections.deque(maxlen=500)
_crash_fp = None  # kept open so faulthandler can write on a hard crash


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _RING.append(self.format(record))
        except Exception:
            pass


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("wdgwars")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    rh = _RingHandler()
    rh.setFormatter(fmt)
    logger.addHandler(rh)

    logger.propagate = False
    _install_crash_handlers(logger)
    return logger


def _install_crash_handlers(logger: logging.Logger) -> None:
    """Capture the reasons an otherwise-silent background app can vanish:
    * faulthandler -> writes a native traceback to the log on a hard crash
      (segfault/fatal error) — the default is stderr, which a windowed app
      has none of.
    * sys.excepthook / threading.excepthook -> log unhandled exceptions from
      the main thread and any worker thread instead of dying quietly.
    """
    global _crash_fp
    try:
        # A SEPARATE file. This handle stays open for the life of the process, and
        # on Windows an open handle stops RotatingFileHandler renaming uploader.log
        # at its size cap - after which every log line is silently dropped.
        _crash_fp = open(log_path().with_name("crash.log"), "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_crash_fp, all_threads=True)
    except Exception:
        pass

    def _hook(exc_type, exc, tb):
        logger.critical("UNHANDLED EXCEPTION", exc_info=(exc_type, exc, tb))
    sys.excepthook = _hook

    def _thook(args):
        name = args.thread.name if args.thread else "?"
        logger.critical("UNHANDLED THREAD EXCEPTION in %s", name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    try:
        threading.excepthook = _thook
    except Exception:
        pass


def recent_lines(n: int = 60) -> list[str]:
    return list(_RING)[-n:]
