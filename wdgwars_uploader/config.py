"""Configuration storage.

Config lives in ``%APPDATA%\\WDGWarsUploader\\config.json`` on Windows and
``$XDG_CONFIG_HOME/wdgwars-uploader/config.json`` (``~/.config/...``) on Linux.
The API key is the only secret; it is stored in plain text at the same trust
level as the rest of the account, and is never returned by the dashboard API.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any


# Interval choices offered in the UI, in hours.
INTERVAL_CHOICES_HOURS = [1, 4, 12, 24]

DEFAULT_CONFIG: dict[str, Any] = {
    "api_key": "",
    "interval_hours": 4,
    # ADS-B --------------------------------------------------------------------
    "adsb_enabled": True,
    # Which radio to read aircraft from:
    #   "auto"    - RTL-SDR if present, else a PortaPack/Mayhem console
    #   "rtlsdr"  - dump1090 + RTL-SDR only
    #   "mayhem"  - PortaPack/HackRF (Mayhem) console only
    "adsb_source": "auto",
    # PortaPack (Mayhem) source. "" / "auto" = the detected PortaPack console.
    "mayhem_port": "auto",
    "mayhem_log_path": "/LOGS/ADSB.TXT",
    # Seconds between log polls. Mayhem writes the same file being read, and
    # tight polling is the suspected trigger for a firmware wedge.
    "mayhem_poll_secs": 3.5,
    # Issue "appstart adsbrx" on connect so the receiver is running.
    "mayhem_autostart_app": True,
    # Receiver location (decimal degrees). dump1090 decodes single-frame
    # positions RELATIVE to this - left unset, the bundled build uses -1,-1 and
    # aircraft appear off the coast of Africa instead of overhead. Also the
    # reference point for dropping implausible fixes before upload.
    "adsb_home_lat": None,
    "adsb_home_lon": None,
    # Where the receiver location comes from (see wdgwars_uploader.gps):
    #   "auto"   - a GPS fix from gpsd when there is one, else the manual lat/lon
    #   "gpsd"   - GPS only; never fall back to the manual values
    #   "manual" - the manual lat/lon only; gpsd is not contacted
    "location_source": "auto",
    "gpsd_host": "127.0.0.1",
    "gpsd_port": 2947,
    # A fix older than this (seconds) is ignored - a persisted fix from an earlier
    # run, or a receiver that lost the sky. Generous on purpose: a stationary
    # receiver's last fix is still right a day later.
    "gps_max_fix_age_secs": 86400,
    # Where to read decoded aircraft from. Priority: url first, then file.
    # A running dump1090/tar1090/readsb serves this by default.
    "adsb_json_url": "http://localhost:8080/data/aircraft.json",
    "adsb_json_file": "",
    # Optional: a dump1090.exe the app can launch itself if nothing is serving
    # aircraft yet. Blank = auto-find one installed in the app's tools dir.
    "dump1090_exe": "",
    "dump1090_args": "--net",
    # If the aircraft URL isn't answering, auto-launch an installed dump1090.
    "dump1090_autostart": True,
    # RTL-SDR tuner gain in dB, written into the bundled dump1090.cfg on every
    # launch (0-49.6; the R820T2 steps to the nearest supported value).
    # None = leave the cfg's own value alone.
    "dump1090_gain": None,
    # MeshCore (Heltec) --------------------------------------------------------
    "mesh_enabled": True,
    # "serial" (USB companion) or "ble" (Bluetooth LE companion).
    "mesh_transport": "serial",
    # "" / "auto" = auto-pick the first MeshCore-looking serial port.
    "mesh_port": "auto",
    "mesh_baud": 115200,
    # BLE transport: "" / "auto" = first MeshCore-looking BLE device, else a
    # name or address to match.
    "mesh_ble_address": "auto",
    # Periodically send a flood advert to announce this node mesh-wide and
    # encourage discovery/propagation. Minutes between adverts; 0 = never.
    # Flooding too often is poor mesh etiquette (and LoRa duty cycle), so keep
    # this >= ~30.
    "mesh_advert_minutes": 60,
    # One or more mesh devices, each with its own protocol, read every cycle and
    # uploaded together (MeshCore + Meshtastic side by side). Seeded from the
    # legacy single-device fields on first load; then this list is authoritative.
    # Each entry: {"port": "auto"|"COMx", "protocol": "meshcore"|"meshtastic",
    #              "baud": 115200, "enabled": true}
    "mesh_devices": [],
    # Meshtastic only: raise the device's LoRa hop limit (1-7) for more reach.
    "mesh_hop_limit": 7,
    # System -------------------------------------------------------------------
    "start_on_boot": False,
    "upload_on_start": True,
    "last_upload_ts": 0,
}

# Where the Linux decoder services write aircraft.json. readsb is the usual one
# on DragonOS Pi64 / Raspberry Pi OS / Debian (install-dragonos-pi64.sh builds
# it with RTL-SDR support; the archive package lacks librtlsdr); dump1090-fa
# comes from FlightAware's own apt repository.
LINUX_AIRCRAFT_JSON_CANDIDATES = (
    ("readsb", "/run/readsb/aircraft.json"),
    ("dump1090-fa", "/run/dump1090-fa/aircraft.json"),
    ("dump1090-mutability", "/run/dump1090-mutability/aircraft.json"),
    ("dump1090", "/run/dump1090/aircraft.json"),
)


def default_aircraft_json_linux() -> str:
    """The aircraft.json of whichever decoder this box has: the first whose file
    already exists (its service is running), else the first whose binary is on
    PATH (installed, not yet running), else dump1090-fa's path."""
    import shutil
    for _, path in LINUX_AIRCRAFT_JSON_CANDIDATES:
        if Path(path).exists():
            return path
    for exe, path in LINUX_AIRCRAFT_JSON_CANDIDATES:
        if shutil.which(exe):
            return path
    return LINUX_AIRCRAFT_JSON_CANDIDATES[1][1]


if sys.platform != "win32":
    # On Linux a packaged decoder (readsb / dump1090-fa) runs as a system
    # service that owns the RTL-SDR and writes aircraft.json under /run; the app
    # reads that file and must never launch a second decoder to fight it over
    # the dongle. The default URL is also dropped: :8080 is a popular port for
    # other SDR web UIs, not a decoder, on a typical Linux radio box.
    DEFAULT_CONFIG.update({
        "adsb_json_url": "",
        "adsb_json_file": default_aircraft_json_linux(),
        "dump1090_autostart": False,
    })


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        d = Path(base) / "WDGWarsUploader"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        d = Path(base) / "wdgwars-uploader"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def log_path() -> Path:
    return config_dir() / "uploader.log"


class Config:
    """Thread-safe view over the JSON config file."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        path = config_path()
        with self._lock:
            self._data = dict(DEFAULT_CONFIG)
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        # Only keep known keys so a stale file cannot break the load.
                        for k in DEFAULT_CONFIG:
                            if k in loaded:
                                self._data[k] = loaded[k]
                except (json.JSONDecodeError, OSError):
                    # Corrupt config: fall back to defaults rather than crash.
                    pass
            # Migrate stale dump1090 args from older versions (pre-gvanem, which
            # used --write-json/--device-type flags the bundled build rejects).
            args = self._data.get("dump1090_args", "")
            if "--write-json" in args or "--device-type" in args:
                self._data["dump1090_args"] = "--net"
            # Seed the mesh-device list from the legacy single-device fields.
            if not self._data.get("mesh_devices"):
                self._data["mesh_devices"] = [{
                    "port": self._data.get("mesh_port", "auto") or "auto",
                    "protocol": "meshcore",
                    "baud": int(self._data.get("mesh_baud", 115200) or 115200),
                    "enabled": True,
                }]

    def save(self) -> None:
        path = config_path()
        with self._lock:
            data = json.dumps(dict(self._data), indent=2)
            # Unique temp file + atomic replace, all under the lock, so
            # concurrent writers can't collide on a shared temp name.
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(values)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)
