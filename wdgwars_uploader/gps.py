"""Receiver location from a GPS receiver, via gpsd.

On Linux a USB GPS dongle belongs to gpsd (udev hands hotplugged receivers to
it), and gpsd serves fixes to any number of clients as JSON over TCP, port 2947.
One background client here keeps the latest fix for the rest of the app:

* :func:`current_fix` is what :func:`adsb.receiver_home` uses as the receiver
  reference (outlier filtering of aircraft and mesh positions) when the location
  source allows it;
* :func:`status` feeds the dashboard's GPS card and the map's receiver marker;
* the last good fix is persisted to ``gps_fix.json`` so the position is known
  right after a restart, while the receiver is still re-acquiring.

The decoder gets the same position on its own: readsb's
``--net-connector 127.0.0.1,2947,gpsd_in`` makes it a gpsd client too.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import threading
import time
from typing import Any

from .config import config_dir

log = logging.getLogger("wdgwars.gps")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2947
_WATCH = b'?WATCH={"enable":true,"json":true};\n'
_RECONNECT_SECS = 30.0
_PERSIST_EVERY = 60.0


def _fix_path():
    return config_dir() / "gps_fix.json"


def _valid(lat: Any, lon: Any) -> tuple[float, float] | None:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:     # null island
        return None
    return lat, lon


class GpsdClient(threading.Thread):
    """Persistent gpsd JSON client: reconnects with backoff, keeps the latest
    fix and receiver status. All getters are thread-safe."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        super().__init__(name="wdgw-gpsd", daemon=True)
        self.host, self.port = host, int(port)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.connected = False
        self.error = ""
        self.device = ""
        self.driver = ""
        self.mode = 0            # gpsd TPV mode: 0 unknown, 1 no fix, 2 = 2D, 3 = 3D
        self.sats_used = 0
        self.sats_seen = 0
        self._fix: dict[str, Any] | None = None
        self._persist_ts = 0.0
        self._restore()

    # -- public ------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def fix(self, max_age: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            f = dict(self._fix) if self._fix else None
        if not f or f.get("mode", 0) < 2:
            return None
        if max_age is not None and time.time() - f.get("ts", 0) > max_age:
            return None
        return f

    def status(self) -> dict[str, Any]:
        with self._lock:
            f = dict(self._fix) if self._fix else None
            return {
                "enabled": True,
                "connected": self.connected,
                "host": f"{self.host}:{self.port}",
                "device": self.device,
                "driver": self.driver,
                "mode": self.mode,
                "sats_used": self.sats_used,
                "sats_seen": self.sats_seen,
                "fix": f,
                "fix_age": (time.time() - f["ts"]) if f else None,
                "error": self.error,
            }

    # -- persistence -------------------------------------------------------
    def _restore(self) -> None:
        try:
            p = _fix_path()
            if not p.exists():
                return
            f = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(f, dict) and int(f.get("mode", 0)) >= 2 and _valid(f.get("lat"), f.get("lon")):
                f["restored"] = True
                self._fix = f
                log.info("restored last GPS fix %.5f,%.5f from %s", f["lat"], f["lon"], f.get("time", "?"))
        except (OSError, ValueError, TypeError):
            pass

    def _persist(self, f: dict[str, Any]) -> None:
        now = time.time()
        if now - self._persist_ts < _PERSIST_EVERY:
            return
        self._persist_ts = now
        try:
            p = _fix_path()
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(f, fh)
            os.replace(tmp, p)
        except OSError:
            pass

    # -- protocol ----------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except (OSError, ValueError) as e:
                self.error = str(e) or e.__class__.__name__
                log.debug("gpsd %s:%s: %s", self.host, self.port, self.error)
            self.connected = False
            self._stop.wait(_RECONNECT_SECS)

    def _session(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=10.0) as s:
            s.sendall(_WATCH)
            self.connected = True
            self.error = ""
            s.settimeout(30.0)
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    # A quiet gpsd (no receiver attached, or one that stopped
                    # reporting): keep the link alive and refresh the device list.
                    s.sendall(b"?DEVICES;\n")
                    continue
                if not chunk:
                    raise OSError("gpsd closed the connection")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._on_message(line)

    def _on_message(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        cls = msg.get("class")
        if cls == "TPV":
            self._on_tpv(msg)
        elif cls == "SKY":
            sats = msg.get("satellites") if isinstance(msg.get("satellites"), list) else []
            with self._lock:
                self.sats_seen = int(msg.get("nSat") or len(sats) or 0)
                self.sats_used = int(msg.get("uSat") or sum(1 for x in sats if isinstance(x, dict) and x.get("used")) or 0)
        elif cls == "DEVICES":
            devs = [d for d in (msg.get("devices") or []) if isinstance(d, dict)]
            with self._lock:
                self.device = str(devs[0].get("path", "")) if devs else ""
                self.driver = str(devs[0].get("driver", "")) if devs else ""
                if not devs:
                    self.mode = 0
        elif cls == "DEVICE":
            # Hotplug: an "activated" timestamp on add; none when the receiver is gone.
            with self._lock:
                if msg.get("activated"):
                    self.device = str(msg.get("path") or self.device)
                    self.driver = str(msg.get("driver") or self.driver)
                elif msg.get("path") and msg.get("path") == self.device:
                    self.device, self.driver, self.mode = "", "", 0
        elif cls == "ERROR":
            self.error = str(msg.get("message") or "gpsd error")

    def _on_tpv(self, msg: dict[str, Any]) -> None:
        try:
            mode = int(msg.get("mode") or 0)
        except (TypeError, ValueError):
            mode = 0
        with self._lock:
            self.mode = mode
            if msg.get("device"):
                self.device = str(msg["device"])
            if mode < 2:
                return
            ll = _valid(msg.get("lat"), msg.get("lon"))
            if ll is None:
                return
            lat, lon = ll
            f = {"lat": lat, "lon": lon,
                 "alt": msg.get("altMSL", msg.get("alt", msg.get("altHAE"))),
                 "mode": mode, "time": msg.get("time"), "ts": time.time(),
                 "epx": msg.get("epx"), "epy": msg.get("epy"), "speed": msg.get("speed")}
            fresh = self._fix is None or self._fix.get("restored")
            self._fix = f
            dev = self.device
        if fresh:
            log.info("GPS fix %.5f,%.5f (%dD, %s)", lat, lon, mode, dev or "gpsd")
        self._persist(f)


# ---------------------------------------------------------------------------
# Module-level singleton driven by the config (see core.poll_devices).
# ---------------------------------------------------------------------------
_client: GpsdClient | None = None
_client_lock = threading.Lock()


def _endpoint(cfg: dict[str, Any]) -> tuple[str, int]:
    host = str(cfg.get("gpsd_host") or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(cfg.get("gpsd_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return host, port


def ensure_client(cfg: dict[str, Any]) -> None:
    """Start, re-point or stop the background gpsd client to match the config.
    Idempotent; cheap enough to call from every device poll."""
    global _client
    source = str(cfg.get("location_source") or "auto").strip().lower()
    host, port = _endpoint(cfg)
    with _client_lock:
        if source == "manual":
            if _client is not None:
                _client.stop()
                _client = None
            return
        if _client is not None and (_client.host, _client.port) != (host, port):
            _client.stop()
            _client = None
        if _client is None or not _client.is_alive():
            _client = GpsdClient(host, port)
            _client.start()


def current_fix(max_age: Any = None) -> dict[str, Any] | None:
    """The latest usable fix (2D or better, not older than ``max_age`` seconds
    when given), or None."""
    c = _client
    if c is None:
        return None
    try:
        age = float(max_age) if max_age is not None else None
    except (TypeError, ValueError):
        age = None
    return c.fix(age)


def status() -> dict[str, Any]:
    c = _client
    if c is None:
        return {"enabled": False, "connected": False, "host": "", "device": "", "driver": "",
                "mode": 0, "sats_used": 0, "sats_seen": 0, "fix": None, "fix_age": None, "error": ""}
    return c.status()


def stop() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.stop()
            _client = None


def query_once(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 5.0) -> dict[str, Any]:
    """One-shot probe for ``--detect``: is gpsd there, which receiver, any fix."""
    out: dict[str, Any] = {"reachable": False, "device": "", "driver": "", "mode": 0,
                           "fix": None, "sats_used": 0, "error": ""}
    deadline = time.time() + timeout
    try:
        with socket.create_connection((host, int(port)), timeout=min(timeout, 3.0)) as s:
            out["reachable"] = True
            s.sendall(_WATCH)
            buf = b""
            while time.time() < deadline:
                s.settimeout(max(0.1, deadline - time.time()))
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    cls = msg.get("class")
                    if cls == "DEVICES":
                        devs = [d for d in (msg.get("devices") or []) if isinstance(d, dict)]
                        if devs:
                            out["device"] = str(devs[0].get("path", ""))
                            out["driver"] = str(devs[0].get("driver", ""))
                    elif cls == "SKY":
                        out["sats_used"] = int(msg.get("uSat") or 0)
                    elif cls == "TPV":
                        try:
                            mode = int(msg.get("mode") or 0)
                        except (TypeError, ValueError):
                            mode = 0
                        out["mode"] = max(out["mode"], mode)
                        if not out["device"] and msg.get("device"):
                            out["device"] = str(msg["device"])
                        ll = _valid(msg.get("lat"), msg.get("lon")) if mode >= 2 else None
                        if ll:
                            out["fix"] = {"lat": ll[0], "lon": ll[1], "mode": mode}
                            return out
    except OSError as e:
        out["error"] = str(e)
    return out
