"""PortaPack (HackRF, **Mayhem** firmware) as an ADS-B source, over its USB CDC
console.

The PortaPack ``adsbrx`` app decodes 1090 MHz on-device and appends decoded
aircraft to ``/LOGS/ADSB.TXT`` on its microSD. This module drives the Mayhem
shell over the CDC serial port (``1d50:6018``): ``appstart adsbrx`` starts the
receiver, then the log's NEW bytes are pulled through the Mayhem file API
(``filesize`` / ``fopen`` / ``fseek`` / ``fread``, which answers in hex) and
parsed into dump1090-shape records for the rest of the app.

Mayhem already CPR-decodes positions, so ``Lat:``/``Lon:`` arrive directly on
airborne-position frames - no ADS-B/CPR decoder is needed here.

Log line format::

    <YYYYMMDDHHMMSS> <rawhexframe> ICAO:<hex24> [Alt:N] [Lat:x Lon:y]
                                   [Type:N Hdg:N Spd:N Vrate:N] [Squawk:N] [<callsign>]

**The device RTC is not trustworthy** (it reads 1980 out of the box), so the
log's own timestamps are used only for ordering *within* the log; every
freshness and aging decision uses the PC clock instead.

Reads are kept deliberately sparse: ``adsbrx`` is writing the same file being
read, and tight ``fopen``/``fread`` churn against a busy Mayhem SD stack is the
suspected trigger for a firmware wedge. Polling is gentle and backs off on
errors.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

try:
    import serial  # pyserial
except Exception:  # pragma: no cover - pyserial is a hard requirement at runtime
    serial = None  # type: ignore

log = logging.getLogger("wdgwars.mayhem")

# PortaPack Mayhem exposes a CDC console on this VID/PID. (A plain HackRF One in
# SDR mode is 1d50:6089 and has no console - it cannot be used as a source here.)
MAYHEM_IDS = {
    (0x1D50, 0x6018),   # HackRF One + PortaPack, Mayhem firmware (CDC console)
}

DEFAULT_LOG_PATH = "/LOGS/ADSB.TXT"
DEFAULT_POLL_SECS = 3.0      # gentle on the Mayhem SD/FS while adsbrx writes
DEFAULT_EXPIRY_SECS = 300.0  # forget an aircraft not heard for 5 min
READ_CAP = 12000             # max bytes pulled per poll
# A busy sky fills the log at roughly 1 kB/s. READ_CAP over one poll cycle has
# to beat that, or the reader falls behind forever on ever-staler aircraft. A
# backlog past this point skips forward to the live end: the feed is about
# what is overhead NOW, and the log is not a queue owed a full replay.
MAX_BACKLOG = READ_CAP * 4
# The console can keep answering while adsbrx has stopped logging (a rebooted
# PortaPack, a wedged SD stack). The log simply stops growing, which the poll
# loop reads as "nothing new" forever - connected, no error, no data, and a
# green dashboard. A long silence is treated as a fault and forces a
# reconnect, which re-issues appstart adsbrx. Generous, so a genuinely quiet
# sky at 3am is not mistaken for a stall.
STALL_SECS = 900.0
_BAUD = 115200
_PROMPT = b"ch>"


# --------------------------------------------------------------------------
# Log parsing
# --------------------------------------------------------------------------

_TS14 = re.compile(r"^\d{14}$")
_CALLSIGN = re.compile(r"^[A-Z0-9]{3,8}$")

# Mayhem token -> (dump1090 field name, coercion).
_NUM_FIELDS: dict[str, tuple[str, Any]] = {
    "Lat:": ("lat", float),
    "Lon:": ("lon", float),
    "Alt:": ("alt_baro", int),
    "Spd:": ("gs", int),
    "Hdg:": ("track", int),
    "Vrate:": ("baro_rate", int),
}


def parse_adsb_log(text: str) -> dict[str, dict[str, Any]]:
    """Parse a chunk of ``ADSB.TXT`` into ``{icao_lower: dump1090-shape dict}``.

    Newest value wins per field, and a field is never overwritten with nothing, so
    a position frame and a later velocity frame for the same aircraft merge into one
    record. Stateless on text: callers feed only the bytes they have not parsed yet,
    so each batch is a coherent time/place window.
    """
    out: dict[str, dict[str, Any]] = {}
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        toks = line.split()
        if len(toks) < 3 or not _TS14.match(toks[0]):
            continue
        icao_tok = next((t for t in toks if t.startswith("ICAO:")), None)
        if icao_tok is None:
            continue
        icao = icao_tok[len("ICAO:"):].strip().lower()
        if not (5 <= len(icao) <= 6) or any(c not in "0123456789abcdef" for c in icao):
            continue

        rec = out.setdefault(icao, {"hex": icao})
        try:
            ts = int(toks[0])
        except ValueError:
            continue
        if ts >= int(rec.get("_ts", 0)):
            rec["_ts"] = ts

        raw_frame = toks[1]
        for t in toks:
            for prefix, (field, cast) in _NUM_FIELDS.items():
                if t.startswith(prefix):
                    try:
                        rec[field] = cast(t[len(prefix):])
                    except (TypeError, ValueError):
                        pass
                    break
            else:
                if t.startswith("Squawk:"):
                    sq = t[len("Squawk:"):].strip()
                    if sq:
                        rec["squawk"] = sq
                # A bare alnum token that is neither the timestamp nor the raw
                # frame is the callsign (identification frames). The raw frame is
                # 14-28 hex chars, so it cannot collide with the 3-8 char pattern.
                elif ":" not in t and t != toks[0] and t != raw_frame and _CALLSIGN.match(t):
                    rec["flight"] = t
    return out


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Merge a freshly parsed record into the running one (newer values win)."""
    for k, v in src.items():
        if v is not None:
            dst[k] = v


# --------------------------------------------------------------------------
# Mayhem serial console
# --------------------------------------------------------------------------

class MayhemConsole:
    """Minimal driver for the Mayhem shell (the ``ch>`` prompt)."""

    def __init__(self, port: str, baud: int = _BAUD) -> None:
        self.port = port
        self.baud = baud
        self._ser: Any = None

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial not installed")
        # Do NOT let pyserial assert DTR/RTS. Toggling the modem-control lines
        # wedges the Mayhem USB stack: the port returns as Windows error 31,
        # "a device attached to the system is not functioning", and only a
        # physical replug clears it. This happens after a force-kill and after a
        # clean shutdown alike, so it is the line toggling and not the manner of
        # exit.
        # pyserial applies these during open(), so set them first.
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = self.baud
        ser.timeout = 1
        ser.dsrdtr = False
        ser.rtscts = False
        ser.dtr = False
        ser.rts = False
        ser.open()
        self._ser = ser
        time.sleep(0.3)
        self._ser.write(b"\r\n")
        time.sleep(0.2)
        self._ser.reset_input_buffer()

    def close(self) -> None:
        s, self._ser = self._ser, None
        if s is not None:
            # Keep the lines low across the close too (same reason as open).
            for _attr in ("dtr", "rts"):
                try:
                    setattr(s, _attr, False)
                except Exception:
                    pass
            try:
                s.close()
            except Exception:
                pass

    def ask(self, cmd: str, ms: int = 900) -> str:
        """Send one shell command and read until the shell re-prints its ``ch>``
        prompt, or ``ms`` milliseconds elapse (a cap, not a fixed wait).

        The prompt is what says the command is done. Waiting out a flat per-command
        timeout instead makes a poll cycle ~12 s, which is slower than the log grows,
        so the reader falls permanently behind; reading to the prompt finishes a cycle
        in about 1 s.
        """
        s = self._ser
        if s is None:
            raise RuntimeError("console not open")
        s.reset_input_buffer()
        s.write((cmd + "\r\n").encode())
        end = time.time() + ms / 1000.0
        buf = bytearray()
        while time.time() < end:
            n = s.in_waiting
            if n:
                buf += s.read(n)
                # Mid-transfer the buffer ends in payload, so this can only match
                # once the command has actually finished.
                if buf.rstrip().endswith(_PROMPT):
                    break
            else:
                time.sleep(0.01)
        return buf.decode("utf-8", "replace")

    def devtype(self) -> str:
        m = re.search(r"HPRO|PORTAPACK|PORTARF", self.ask("getdevtype", 800).upper())
        return m.group(0) if m else ""

    def appstart(self, app: str = "adsbrx") -> bool:
        return "ok" in self.ask(f"appstart {app}", 1500).lower()

    def filesize(self, path: str) -> int:
        """Size of ``path`` in bytes, or -1 if the shell did not answer."""
        m = re.search(r"^\s*(\d+)\s*$", self.ask(f"filesize {path}", 800), re.M)
        return int(m.group(1)) if m else -1

    def read_range(self, path: str, offset: int, length: int) -> str:
        """Read ``length`` bytes of ``path`` from ``offset``. ``fread`` answers in
        hex; anything that is not a hex digit (echo, prompt, newlines) is stripped."""
        self.ask(f"fopen {path}", 1500)
        try:
            self.ask(f"fseek {offset}", 1000)
            # fread answers in hex (2 chars/byte) at 115200 baud ~= 11.5 kB/s, so allow
            # the transfer the time the payload actually needs, plus headroom.
            raw = self.ask(f"fread {length}", int(length * 2 / 11.5) + 2000)
        finally:
            self.ask("fclose", 1000)
        hexs = re.sub(r"(?s).*?fread \d+\s*", "", raw)
        hexs = re.sub(r"[^0-9A-Fa-f]", "", hexs)
        if len(hexs) % 2:
            hexs = hexs[:-1]
        try:
            return bytes.fromhex(hexs).decode("utf-8", "replace")
        except ValueError:
            return ""


# --------------------------------------------------------------------------
# Background reader
# --------------------------------------------------------------------------

class MayhemAdsbReader(threading.Thread):
    """Polls the PortaPack ADS-B log and keeps a live aircraft view.

    Runs in the background because a serial round-trip takes seconds, while
    :func:`collect` must stay fast and side-effect-free for the poller.
    """

    def __init__(self, port: str, log_path: str = DEFAULT_LOG_PATH,
                 poll_secs: float = DEFAULT_POLL_SECS, autostart_app: bool = True,
                 expiry_secs: float = DEFAULT_EXPIRY_SECS) -> None:
        super().__init__(name="mayhem-adsb", daemon=True)
        self.port = port
        self.log_path = log_path
        self.poll_secs = max(1.5, float(poll_secs))
        self.autostart_app = autostart_app
        self.expiry_secs = float(expiry_secs)

        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._aircraft: dict[str, dict[str, Any]] = {}   # icao -> record
        self._seen_wall: dict[str, float] = {}           # icao -> wall clock last heard
        self.connected = False
        self.device = ""
        self.error = ""
        self.bytes_read = 0
        self.last_data_ts = 0.0    # wall clock of the last NEW log bytes

    # -- public ------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        """Current view as a dump1090-shape object. ``seen`` is seconds since the
        aircraft was last heard, measured on the PC clock (the device RTC is
        unreliable).
        """
        now = time.time()
        out = []
        with self._lock:
            for icao, rec in self._aircraft.items():
                last = self._seen_wall.get(icao, 0.0)
                if now - last > self.expiry_secs:
                    continue
                a = {k: v for k, v in rec.items() if not k.startswith("_")}
                a["seen"] = round(now - last, 1)
                out.append(a)
        return {"now": now, "aircraft": out}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "port": self.port, "connected": self.connected, "device": self.device,
                "error": self.error, "aircraft": len(self._aircraft),
                "bytes_read": self.bytes_read,
                "stalled_for": (time.time() - self.last_data_ts) if self.last_data_ts else None,
            }

    # -- thread body -------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            con = MayhemConsole(self.port)
            try:
                con.open()
                self.device = con.devtype() or "PortaPack"
                if self.autostart_app:
                    con.appstart("adsbrx")
                    time.sleep(1.5)
                # Start from the CURRENT end of the log: earlier entries are from
                # a previous session (and the device clock cannot date them), so
                # uploading them as "now" would be wrong.
                offset = max(0, con.filesize(self.log_path))
                with self._lock:
                    self.connected = True
                    self.error = ""
                    self.last_data_ts = time.time()   # arm the stall watchdog
                log.info("mayhem: %s on %s, adsbrx running, log at %d bytes",
                         self.device, self.port, offset)
                self._poll_loop(con, offset)
            except Exception as e:
                with self._lock:
                    self.connected = False
                    self.error = str(e)
                log.warning("mayhem reader on %s failed: %s", self.port, e)
            finally:
                con.close()
                with self._lock:
                    self.connected = False
            if self._stop.wait(5.0):   # reconnect backoff
                break

    def _poll_loop(self, con: MayhemConsole, offset: int) -> None:
        carry = ""     # partial trailing line carried between polls
        errs = 0       # consecutive failures -> progressive backoff
        while not self._stop.is_set():
            # Back off when the shell stops answering rather than hammering a
            # wedged Mayhem (see module docstring).
            if self._stop.wait(self.poll_secs + min(errs, 4) * 1.5):
                break
            try:
                size = con.filesize(self.log_path)
                if size < 0:
                    errs += 1
                    if errs > 6:
                        raise RuntimeError("console stopped answering filesize")
                    continue
                if size < offset:
                    # Log rotated/truncated (adsbrx restarted) - resync.
                    log.info("mayhem: log shrank (%d < %d), resyncing", size, offset)
                    offset, carry = 0, ""
                if size <= offset:
                    errs = 0
                    # Console answering but the log is not growing: adsbrx is not
                    # running. Bail out so run() reconnects and restarts it.
                    if self.last_data_ts and (time.time() - self.last_data_ts) > STALL_SECS:
                        raise RuntimeError(
                            f"no new ADS-B data for {STALL_SECS:.0f}s "
                            "(adsbrx not logging) - reconnecting")
                    continue
                if size - offset > MAX_BACKLOG:
                    # Fell behind (busy device, or a paused reader). Skip to the live end
                    # rather than grinding through stale history.
                    log.info("mayhem: %d bytes behind, skipping to live end",
                             size - offset)
                    offset, carry = size - READ_CAP, ""
                chunk = con.read_range(self.log_path, offset, min(size - offset, READ_CAP))
                if not chunk:
                    errs += 1
                    continue
                offset += len(chunk)   # advance by bytes actually decoded
                errs = 0
                with self._lock:
                    self.bytes_read += len(chunk)
                    self.last_data_ts = time.time()

                text = carry + chunk
                nl = text.rfind("\n")
                complete, carry = (text[:nl], text[nl + 1:]) if nl >= 0 else ("", text)
                if not complete.strip():
                    continue
                batch = parse_adsb_log(complete)
                if batch:
                    now = time.time()
                    with self._lock:
                        for icao, rec in batch.items():
                            cur = self._aircraft.setdefault(icao, {"hex": icao})
                            _merge(cur, rec)
                            self._seen_wall[icao] = now
                        self._expire(now)
            except Exception as e:
                errs += 1
                log.debug("mayhem poll hiccup (#%d): %s", errs, e)
                if errs > 8:
                    raise

    def _expire(self, now: float) -> None:
        stale = [i for i, t in self._seen_wall.items() if now - t > self.expiry_secs]
        for i in stale:
            self._aircraft.pop(i, None)
            self._seen_wall.pop(i, None)


# --------------------------------------------------------------------------
# Module-level singleton (mirrors adsb.ensure_decoder / adsb.collect)
# --------------------------------------------------------------------------

_reader: MayhemAdsbReader | None = None
_reader_lock = threading.Lock()


def ensure_reader(port: str, cfg: dict[str, Any] | None = None) -> bool:
    """Start (or restart on a port change) the background reader. Non-blocking."""
    global _reader
    cfg = cfg or {}
    if not port:
        return False
    with _reader_lock:
        if _reader is not None and _reader.is_alive():
            if _reader.port == port:
                return True
            _reader.stop()          # port changed - replace the reader
            _reader = None
        r = MayhemAdsbReader(
            port=port,
            log_path=cfg.get("mayhem_log_path") or DEFAULT_LOG_PATH,
            poll_secs=float(cfg.get("mayhem_poll_secs") or DEFAULT_POLL_SECS),
            autostart_app=bool(cfg.get("mayhem_autostart_app", True)),
        )
        r.start()
        _reader = r
        return True


def collect() -> dict[str, Any] | None:
    """Latest dump1090-shape view, or ``None`` if no reader is attached."""
    with _reader_lock:
        r = _reader
    if r is None or not r.is_alive():
        return None
    return r.snapshot()


def status() -> dict[str, Any] | None:
    with _reader_lock:
        r = _reader
    return r.status() if r is not None else None


def stop(timeout: float = 15.0) -> None:
    """Stop the reader and WAIT for it to release the port.

    The reader is a daemon thread, so without this join the interpreter can kill
    it mid-``fread`` on exit. The port then closes while the PortaPack is still
    streaming, which wedges its USB stack (Windows error 31, "a device attached
    to the system is not functioning") until the cable is physically replugged.
    Joining lets the poll loop finish its current read and close the console
    itself.
    """
    global _reader
    with _reader_lock:
        r, _reader = _reader, None
    if r is None:
        return
    r.stop()
    try:
        r.join(timeout)
        if r.is_alive():
            log.warning("mayhem reader did not stop within %.0fs; the port may "
                        "need a replug", timeout)
    except Exception:  # noqa: BLE001
        pass
