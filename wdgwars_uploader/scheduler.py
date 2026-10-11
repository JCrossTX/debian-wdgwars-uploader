"""Background upload scheduler.

Runs a daemon thread that fires an upload cycle every ``interval_hours`` (1/4/12/
24). Supports an on-demand "upload now" trigger and reacts to interval changes.
The actual per-cycle work is injected as a callable so this module stays free of
device/network concerns.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger("wdgwars.sched")


class Scheduler:
    def __init__(self, cfg, run_cycle: Callable[[], None]) -> None:
        self._cfg = cfg
        self._run_cycle = run_cycle
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._force = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_cycle = threading.Lock()
        self.last_run_ts: float = float(cfg.get("last_upload_ts", 0) or 0)
        self.next_run_ts: float = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wdgw-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    # -- triggers ----------------------------------------------------------
    def trigger_now(self) -> None:
        """Request an immediate upload cycle."""
        self._force.set()
        self._wake.set()

    def reschedule(self) -> None:
        """Interval changed: recompute the next wake."""
        self._wake.set()

    # -- internals ---------------------------------------------------------
    def _interval_seconds(self) -> float:
        hours = float(self._cfg.get("interval_hours", 4) or 4)
        return max(0.25, hours) * 3600.0

    def _compute_next(self) -> float:
        return (self.last_run_ts or time.time()) + self._interval_seconds()

    def _loop(self) -> None:
        # If configured, do an upload shortly after launch.
        if self._cfg.get("upload_on_start", True):
            self._force.set()
        while not self._stop.is_set():
            self.next_run_ts = self._compute_next()
            now = time.time()
            if self._force.is_set() or now >= self.next_run_ts:
                self._force.clear()
                self._do_cycle()
                self.last_run_ts = time.time()
                self._cfg.set("last_upload_ts", self.last_run_ts)
                try:
                    self._cfg.save()
                except Exception:
                    pass
                continue
            # Sleep until next run or an external wake, whichever first.
            timeout = max(1.0, min(self.next_run_ts - now, 3600.0))
            self._wake.wait(timeout=timeout)
            self._wake.clear()

    def _do_cycle(self) -> None:
        if not self._running_cycle.acquire(blocking=False):
            return  # a cycle is already running
        try:
            self._run_cycle()
        except Exception:
            log.exception("upload cycle crashed")
        finally:
            self._running_cycle.release()
