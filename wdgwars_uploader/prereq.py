"""Prerequisite checks surfaced on the dashboard so a new user knows exactly what
is missing and how to fix it: Python deps, a dump1090 decoder, and — the common
gotcha — whether the RTL-SDR is bound to the WinUSB driver (needed by rtl-sdr /
dump1090) rather than the Realtek DVB-T driver.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

ZADIG_URL = "https://zadig.akeo.ie/"


def _check(name: str, ok: bool, status: str, detail: str = "", fix: str = "", url: str = "") -> dict:
    return {"name": name, "ok": ok, "status": status, "detail": detail, "fix": fix, "url": url}


def check_python_deps() -> dict:
    required = ["requests", "serial", "meshcore", "PIL"]
    if sys.platform == "win32":
        # On Linux the tray is optional (headless + web dashboard works without
        # a StatusNotifier host), so a missing pystray is not a failure there.
        required.append("pystray")
    missing = [m for m in required if importlib.util.find_spec(m) is None]
    if missing:
        return _check("Python packages", False, "missing: " + ", ".join(missing),
                      fix="pip install -r requirements.txt")
    return _check("Python packages", True, "all installed")


def check_dump1090(configured_exe: str = "") -> dict:
    from . import adsb
    exe = adsb.find_dump1090(configured_exe)
    if exe:
        return _check("ADS-B decoder (dump1090)", True, "installed", detail=exe)
    return _check("ADS-B decoder (dump1090)", False, "not installed",
                  fix="Click 'Install dump1090' to fetch the Windows build automatically, "
                      "or point Settings at a running dump1090 URL. If it was installed "
                      "and has disappeared, check antivirus quarantine: Microsoft Defender "
                      "flags this unsigned decoder as Trojan:Win32/Bearfoos.A!ml, a "
                      "machine-learning false positive. The download is checked against a "
                      "pinned SHA-256 of the gvanem/Dump1090 v1.0.0 release.")


def check_adsb_source_linux(cfg: dict) -> dict:
    """On Linux the decoder is a system service (dump1090-fa / readsb) the app
    reads from, never launches. OK = the configured aircraft JSON is reachable."""
    from pathlib import Path

    from . import adsb
    url = (cfg.get("adsb_json_url") or "").strip()
    file = (cfg.get("adsb_json_file") or "").strip()
    if url and adsb.from_url(url, timeout=3) is not None:
        return _check("ADS-B decoder", True, "serving", detail=url)
    if file:
        p = Path(file)
        if p.exists() and adsb.from_file(file) is not None:
            return _check("ADS-B decoder", True, "serving", detail=file)
        if p.exists():
            return _check("ADS-B decoder", False, f"{file} exists but is not aircraft JSON",
                          fix="Point Settings at the decoder's aircraft.json "
                              "(readsb writes /run/readsb/aircraft.json, "
                              "dump1090-fa writes /run/dump1090-fa/aircraft.json).")
    # A decoder IS running, just not the one Settings points at.
    from .config import LINUX_AIRCRAFT_JSON_CANDIDATES
    for _, cand in LINUX_AIRCRAFT_JSON_CANDIDATES:
        if cand != file and Path(cand).exists() and adsb.from_file(cand) is not None:
            return _check("ADS-B decoder", False, f"running, but writing {cand}",
                          detail=f"Settings: {file or url or '(blank)'}",
                          fix=f"Set the ADS-B aircraft.json file in Settings to {cand}.")
    return _check("ADS-B decoder", False, "no aircraft source reachable",
                  fix="Install one: ./install-dragonos-pi64.sh builds readsb with RTL-SDR support "
                      "(the archive readsb lacks librtlsdr; writes /run/readsb/aircraft.json), "
                      "or dump1090-fa from FlightAware's repo (/run/dump1090-fa/aircraft.json). "
                      "Check it is running (systemctl status readsb) and that Settings points "
                      "at its aircraft.json file or URL.")


def rtlsdr_driver_state(rtlsdr_present: bool) -> dict:
    """Determine whether a present RTL-SDR is on WinUSB (usable by rtl-sdr) or
    still on the Realtek DVB-T driver (not usable until re-bound via Zadig)."""
    if not rtlsdr_present:
        return _check("RTL-SDR driver", True, "n/a (no RTL-SDR)")
    if sys.platform != "win32":
        return _check("RTL-SDR driver", True, "n/a (non-Windows)")
    ps = (
        "$d = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_0BDA&PID_283[289]' } | Select-Object -First 1;"
        "if ($d) { $svc = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue).Data;"
        "Write-Output ($d.Class + '|' + $svc) }"
    )
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        out = (res.stdout or "").strip()
    except Exception:
        out = ""
    cls, _, svc = out.partition("|")
    svc_l = svc.strip().lower()
    if svc_l in ("winusb", "libusbk", "libusb0", "winusbcompat"):
        return _check("RTL-SDR driver", True, f"WinUSB ready ({svc.strip()})")
    # Bound to DVB/Realtek (MEDIA class, RTL2832UUSB service) or unknown.
    return _check(
        "RTL-SDR driver", False,
        f"needs WinUSB (currently: {cls or 'DVB'} / {svc.strip() or 'Realtek'})",
        fix="Run Zadig → Options → List All Devices → select 'Bulk-In, Interface (Interface 0)' "
            "(RTL2832U) → replace driver with WinUSB. Requires admin. This disables DVB-T TV use.",
        url=ZADIG_URL,
    )


def portapack_state(port: str) -> dict:
    """Health of the PortaPack/Mayhem ADS-B source."""
    from . import mayhem
    if not port:
        return _check("PortaPack (Mayhem)", False, "not detected",
                      fix="Connect the PortaPack over USB. It must be running Mayhem "
                          "firmware, which exposes a console on USB ID 1d50:6018.")
    st = mayhem.status() or {}
    if st.get("connected"):
        return _check("PortaPack (Mayhem)", True,
                      f"reading on {port} ({int(st.get('aircraft', 0) or 0)} aircraft)",
                      detail=str(st.get("device", "")))
    return _check("PortaPack (Mayhem)", False, f"detected on {port}, not reading yet",
                  detail=str(st.get("error", "")),
                  fix="Insert the microSD (adsbrx logs to /LOGS/ADSB.TXT), attach the "
                      "1090 MHz antenna, and close anything else holding the COM port.")


def check_receiver_location(cfg: dict) -> dict:
    """Is the receiver's own position known? It anchors the outlier filters for
    aircraft and mesh nodes, and the decoder needs it for single-frame positions."""
    from . import adsb, gps
    loc = adsb.receiver_location(cfg)
    if loc:
        how = "GPS fix via gpsd" if loc.get("source") == "gps" else "manual setting"
        return _check("Receiver location", True, f"{loc['lat']:.4f}, {loc['lon']:.4f} ({how})")
    st = gps.status()
    source = str(cfg.get("location_source") or "auto").lower()
    if source != "manual" and st.get("enabled"):
        if not st.get("connected"):
            why = f"gpsd not reachable at {st.get('host')}"
        elif not st.get("device"):
            why = "gpsd is running but has no GPS receiver"
        else:
            why = f"GPS acquiring ({st.get('sats_used', 0)}/{st.get('sats_seen', 0)} satellites)"
    else:
        why = "not set"
    return _check("Receiver location", False, why,
                  fix="Plug in a USB GPS receiver (gpsd picks it up automatically) or enter "
                      "the receiver latitude/longitude in Settings. Without it, glitched "
                      "aircraft and mesh positions cannot be filtered against the receiver, "
                      "and the decoder cannot place single-frame ADS-B positions.")


def check_all(devices, cfg: dict) -> list[dict]:
    from . import adsb
    source, port = adsb.resolve_source(cfg, devices)
    checks = [check_python_deps(), check_receiver_location(cfg)]
    if source == "mayhem":
        # dump1090 and the WinUSB driver are irrelevant when the PortaPack does the
        # decoding on-device, so they are not reported as missing.
        checks.append(portapack_state(port))
    elif sys.platform == "win32":
        checks.append(check_dump1090(cfg.get("dump1090_exe", "")))
        checks.append(rtlsdr_driver_state(bool(devices and devices.rtlsdr_present)))
    else:
        checks.append(check_adsb_source_linux(cfg))
    return checks
