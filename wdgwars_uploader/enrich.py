"""Aircraft enrichment: registration / type / operator + a photo, by ICAO hex.

Fetched server-side (no browser CORS worries) from two free, no-key public APIs
and cached in memory — aircraft metadata is static, so a long TTL is fine:

* **adsbdb** (``api.adsbdb.com``) — registration, type, manufacturer, operator,
  country, and often a photo URL; plus flight route by callsign.
* **planespotters** (``api.planespotters.net``) — a good thumbnail + credit,
  used as the photo source / fallback.

Everything degrades gracefully: a miss or a network error just yields fewer
fields, never an error to the caller.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from . import __version__

log = logging.getLogger("wdgwars.enrich")

# planespotters requires a User-Agent that includes a contact URL/email.
_UA = f"wdgwars-mesh-adsb-uploader/{__version__} (+https://github.com/RocketGod-git/wdgwars-mesh-adsb-uploader)"
_TTL = 12 * 3600
_cache: dict[str, tuple[float, dict]] = {}
_route_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _get_json(url: str, timeout: float = 6.0) -> dict | None:
    try:
        r = requests.get(url, headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def _planespotters_photo(hex_id: str) -> dict:
    data = _get_json(f"https://api.planespotters.net/pub/photos/hex/{hex_id}")
    photos = (data or {}).get("photos") or []
    if not photos:
        return {}
    p = photos[0]
    large = (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("src")
    small = (p.get("thumbnail") or {}).get("src") or large
    return {"photo": large, "photo_thumb": small,
            "photo_link": p.get("link"), "photographer": p.get("photographer")}


def _adsbdb_aircraft(hex_id: str) -> dict:
    data = _get_json(f"https://api.adsbdb.com/v0/aircraft/{hex_id}")
    ac = ((data or {}).get("response") or {}).get("aircraft") or {}
    if not ac or not isinstance(ac, dict):
        return {}
    return {
        "registration": ac.get("registration"),
        "type": ac.get("type"),
        "icao_type": ac.get("icao_type"),
        "manufacturer": ac.get("manufacturer"),
        "operator": ac.get("registered_owner"),
        "country": ac.get("registered_owner_country_name"),
        "flag_code": ac.get("registered_owner_operator_flag_code"),
        "photo": ac.get("url_photo"),
        "photo_thumb": ac.get("url_photo_thumbnail"),
    }


def aircraft_info(hex_id: str) -> dict[str, Any]:
    hex_id = (hex_id or "").strip().lower()
    if not hex_id:
        return {}
    now = time.time()
    with _lock:
        hit = _cache.get(hex_id)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    info: dict[str, Any] = {"hex": hex_id.upper()}
    adb = _adsbdb_aircraft(hex_id)
    # adsbdb metadata (registration/type/operator/…) — but NOT its photo, which
    # comes from airport-data.com and blocks hotlinking in a browser.
    info.update({k: v for k, v in adb.items() if v and k not in ("photo", "photo_thumb")})
    adb_photo = adb.get("photo")            # kept only as a last-resort fallback

    # planespotters photos are meant to be embedded — use them as the display img.
    ps = _planespotters_photo(hex_id)
    if ps.get("photo"):
        info["photo"] = ps["photo"]
        info["photo_thumb"] = ps.get("photo_thumb") or ps["photo"]
        if ps.get("photo_link"):
            info["photo_link"] = ps["photo_link"]
        if ps.get("photographer"):
            info["photographer"] = ps["photographer"]
        if adb_photo:
            info["photo_alt"] = adb_photo
    elif adb_photo:
        info["photo"] = adb_photo
        info["photo_thumb"] = adb.get("photo_thumb") or adb_photo
    with _lock:
        _cache[hex_id] = (now, info)
    return info


def flight_route(callsign: str) -> dict[str, Any]:
    callsign = (callsign or "").strip().upper()
    if not callsign:
        return {}
    now = time.time()
    with _lock:
        hit = _route_cache.get(callsign)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    data = _get_json(f"https://api.adsbdb.com/v0/callsign/{callsign}")
    fr = ((data or {}).get("response") or {}).get("flightroute") or {}
    out: dict[str, Any] = {}
    if isinstance(fr, dict) and fr:
        o, d = fr.get("origin") or {}, fr.get("destination") or {}
        out = {
            "airline": (fr.get("airline") or {}).get("name"),
            "origin": o.get("iata_code") or o.get("icao_code"),
            "origin_name": o.get("name"),
            "destination": d.get("iata_code") or d.get("icao_code"),
            "destination_name": d.get("name"),
        }
        out = {k: v for k, v in out.items() if v}
    with _lock:
        _route_cache[callsign] = (now, out)
    return out


def full(hex_id: str, callsign: str = "") -> dict[str, Any]:
    info = dict(aircraft_info(hex_id))
    if callsign:
        route = flight_route(callsign)
        if route:
            info["route"] = route
    return info
