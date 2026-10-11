"""Non-admin self-heal: a tiny sidecar that restarts the app if it dies
unexpectedly (crash or external kill), so an unattended background uploader
stays up without a Scheduled Task (which needs admin).

Model: each app instance spawns exactly one watchdog watching its PID. When the
app exits, the watchdog checks a "clean stop" marker — written only by a proper
tray-Quit — and, if absent, relaunches the app. The relaunched app spawns its
own new watchdog, and this one exits, so the chain stays one level deep. A
rapid-restart cap breaks crash loops.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import config_dir

log = logging.getLogger("wdgwars.watchdog")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DETACH = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}


# If the app's heartbeat goes this stale while its process is still alive, it's
# hung (not merely busy) — the watchdog kills and restarts it. Comfortably above
# the mesh connect deadline (60s) so a single slow poll never trips it.
_HEARTBEAT_DEADLINE = 150.0


def _marker() -> Path:
    return config_dir() / "clean_stop"


def _heartbeat() -> Path:
    return config_dir() / "heartbeat"


def beat() -> None:
    """Called periodically by the running app to prove it's still alive."""
    try:
        _heartbeat().write_text(str(time.time()))
    except OSError:
        pass


def _heartbeat_age() -> float | None:
    try:
        return time.time() - float(_heartbeat().read_text().strip())
    except (OSError, ValueError):
        return None


def _kill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10, creationflags=_NO_WINDOW)
    except Exception as e:
        log.warning("could not kill pid %s: %s", pid, e)


def _restart_log() -> Path:
    return config_dir() / "restarts.log"


def mark_clean_stop() -> None:
    """Called by app.quit(): tells the watchdog NOT to restart."""
    try:
        _marker().write_text(str(time.time()))
    except OSError:
        pass


def clear_clean_stop() -> None:
    """Called on app startup so a later crash is seen as unexpected."""
    try:
        _marker().unlink()
    except OSError:
        pass


def _hang_marker() -> Path:
    return config_dir() / "port_recovery.ts"


def recover_via_restart(reason: str, cooldown: float = 900.0) -> bool:
    """Force a clean process exit so the watchdog restarts a single fresh
    instance — used to recover a serial port wedged by an un-interruptible
    library deadlock (a fresh process reopens it cleanly).

    Rate-limited: a second force-restart within ``cooldown`` seconds would be a
    restart loop, so instead this returns False and lets the caller
    degrade (e.g. skip that device) while keeping the rest of the app running.
    On the happy path this never returns — the process exits."""
    now = time.time()
    p = _hang_marker()
    try:
        last = float(p.read_text().strip()) if p.exists() else 0.0
    except (OSError, ValueError):
        last = 0.0
    if now - last < cooldown:
        log.error("port-recovery restart suppressed (last was %.0fs ago) — degrading instead: %s",
                  now - last, reason)
        return False
    try:
        p.write_text(str(now))
    except OSError:
        pass
    log.error("forcing clean restart to recover: %s", reason)
    for h in list(logging.getLogger().handlers):
        try:
            h.flush()
        except Exception:
            pass
    os._exit(17)   # the watchdog is watching this pid and relaunches the app
    return False   # unreachable; keeps type-checkers happy


def _app_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--boot"]
    # Use the absolute run_app.py path (not `-m`), so it works regardless of the
    # watchdog's working directory.
    py = sys.executable
    pyw = str(Path(py).with_name("pythonw.exe"))
    exe = pyw if Path(pyw).exists() else py
    return [exe, str(Path(__file__).resolve().parent.parent / "run_app.py"), "--boot"]


def _watchdog_cmd(pid: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--watchdog", str(pid)]
    py = sys.executable
    pyw = str(Path(py).with_name("pythonw.exe"))
    exe = pyw if Path(pyw).exists() else py
    return [exe, str(Path(__file__).resolve().parent.parent / "run_app.py"), "--watchdog", str(pid)]


def spawn(app_pid: int) -> None:
    """Launch a detached watchdog watching ``app_pid``."""
    if sys.platform != "win32":
        return
    try:
        subprocess.Popen(_watchdog_cmd(app_pid), creationflags=_NO_WINDOW, **_DETACH)
    except Exception as e:
        log.warning("could not spawn watchdog: %s", e)


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        return str(pid) in (out.stdout or "")
    except Exception:
        return False


def _crash_looping() -> bool:
    """True if there have already been 3+ restarts in the last 3 minutes."""
    now = time.time()
    p = _restart_log()
    times: list[float] = []
    try:
        if p.exists():
            times = [float(x) for x in p.read_text().split() if x]
    except (OSError, ValueError):
        times = []
    times = [t for t in times if now - t < 180]
    if len(times) >= 3:
        return True
    times.append(now)
    try:
        p.write_text(" ".join(str(t) for t in times[-10:]))
    except OSError:
        pass
    return False


def run(app_pid: int) -> None:
    """Watch ``app_pid``; when it exits, restart the app unless it stopped
    cleanly or is crash-looping."""
    if sys.platform != "win32":
        return
    time.sleep(10)   # grace period so a just-started app isn't judged as hung
    while _pid_alive(app_pid):
        age = _heartbeat_age()
        if age is not None and age > _HEARTBEAT_DEADLINE and not _marker().exists():
            log.warning("app pid %s hung (heartbeat %.0fs stale); killing to restart", app_pid, age)
            _kill(app_pid)
            break
        time.sleep(5)
    if _marker().exists():
        log.info("app stopped cleanly; watchdog exiting")
        return
    if _crash_looping():
        log.warning("app is crash-looping; watchdog backing off")
        return
    log.warning("app died unexpectedly (pid %s); restarting", app_pid)
    try:
        subprocess.Popen(_app_cmd(), creationflags=_NO_WINDOW, **_DETACH)
    except Exception as e:
        log.warning("restart failed: %s", e)
