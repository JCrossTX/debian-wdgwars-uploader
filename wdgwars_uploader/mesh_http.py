"""Meshtastic over meshtasticd's HTTP API.

meshtasticd serves its client API two ways: the TCP stream on port 4403, which
accepts exactly ONE client (a new connection force-closes the previous one), and
the web API on its HTTPS port (443 by default) used by the bundled web client.
The web API has its own packet queue, independent of the TCP slot, so a client
here coexists with the Meshtastic MUI, the web client and a phone on TCP.

:class:`HTTPInterface` is a :class:`meshtastic.mesh_interface.MeshInterface`
with the transport swapped: ``PUT /api/v1/toradio`` carries each ToRadio
protobuf, and a reader thread polls ``GET /api/v1/fromradio?all=false`` (one
FromRadio protobuf per response, empty when the queue is drained) and feeds the
bytes to the library's normal packet handling. Everything above the transport -
node DB, pubsub events, config download, heartbeat - is the stock library.

The daemon's certificate is self-signed, so TLS is not verified by default; the
API is normally only reachable on localhost anyway.
"""

from __future__ import annotations

import http.client
import logging
import ssl
import threading
import time
from typing import Any

from meshtastic.mesh_interface import MeshInterface

log = logging.getLogger("wdgwars.mesh_http")

DEFAULT_HTTPS_PORT = 443
DEFAULT_HTTP_PORT = 80
_API = "/api/v1/"
_POLL_BUSY = 0.05      # seconds between polls while packets are flowing
_POLL_IDLE = 1.0       # ... once the queue has drained
_REQ_TIMEOUT = 10.0
_MAX_CONSECUTIVE_ERRORS = 5   # transport failures in a row before the reader gives up


class HTTPInterface(MeshInterface):
    """Meshtastic interface talking to meshtasticd's web API."""

    def __init__(self, host: str = "localhost", port: int | None = None, tls: bool = True,
                 verify: bool = False, timeout: int = 300, noNodes: bool = False,
                 connectNow: bool = True) -> None:
        self.host = host
        self.port = int(port or (DEFAULT_HTTPS_PORT if tls else DEFAULT_HTTP_PORT))
        self.tls = tls
        self._ctx: ssl.SSLContext | None = None
        if tls:
            self._ctx = ssl.create_default_context()
            if not verify:
                self._ctx.check_hostname = False
                self._ctx.verify_mode = ssl.CERT_NONE
        self._conn: http.client.HTTPConnection | None = None
        self._conn_lock = threading.Lock()
        self._wantExit = False
        self._rxThread = threading.Thread(target=self._reader, daemon=True, name="http reader")
        MeshInterface.__init__(self, noProto=False, noNodes=noNodes, timeout=timeout)
        if connectNow:
            try:
                self.connect()
                self.waitForConfig()
            except Exception:
                try:
                    self.close()
                except Exception:  # noqa: BLE001
                    pass
                raise

    def __repr__(self) -> str:
        return f"HTTPInterface({'https' if self.tls else 'http'}://{self.host}:{self.port})"

    # -- transport ---------------------------------------------------------
    def _open_conn(self) -> http.client.HTTPConnection:
        if self.tls:
            return http.client.HTTPSConnection(self.host, self.port, timeout=_REQ_TIMEOUT,
                                               context=self._ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=_REQ_TIMEOUT)

    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        """One API call on a persistent connection (re-opened when the server
        drops it). Raises OSError on any transport or HTTP failure."""
        headers = {"Content-Type": "application/x-protobuf"} if body is not None else {}
        with self._conn_lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None:
                        self._conn = self._open_conn()
                    self._conn.request(method, _API + path, body=body, headers=headers)
                    resp = self._conn.getresponse()
                    data = resp.read()
                    if resp.status >= 400:
                        raise OSError(f"meshtasticd HTTP {resp.status} on {method} {path}")
                    if resp.getheader("Connection", "").lower() == "close":
                        self._conn.close()
                        self._conn = None
                    return data
                except (http.client.HTTPException, OSError) as e:
                    if self._conn is not None:
                        try:
                            self._conn.close()
                        except Exception:  # noqa: BLE001
                            pass
                        self._conn = None
                    if attempt == 2 or isinstance(e, OSError) and "HTTP" in str(e):
                        raise OSError(str(e) or e.__class__.__name__) from e
        raise OSError("unreachable")  # pragma: no cover

    # -- MeshInterface hooks -----------------------------------------------
    def connect(self) -> None:
        """Start the poller and request the config + node DB download."""
        self._rxThread.start()
        self._startConfig()
        self._waitConnected()

    def _sendToRadioImpl(self, toRadio: Any) -> None:
        self._request("PUT", "toradio", toRadio.SerializeToString())

    def _reader(self) -> None:
        errors = 0
        delay = _POLL_BUSY
        try:
            while not self._wantExit:
                try:
                    data = self._request("GET", "fromradio?all=false")
                    errors = 0
                except OSError as e:
                    errors += 1
                    if errors >= _MAX_CONSECUTIVE_ERRORS:
                        if not self._wantExit:
                            log.error("meshtasticd web API unreachable, terminating reader: %s", e)
                        break
                    time.sleep(min(2.0 * errors, 10.0))
                    continue
                if data:
                    self._handleFromRadio(data)
                    delay = _POLL_BUSY
                else:
                    time.sleep(delay)
                    delay = min(_POLL_IDLE, delay * 2)
        except Exception as e:  # noqa: BLE001
            log.error("unexpected error in meshtasticd web API reader: %s", e)
        finally:
            self._disconnected()

    def close(self) -> None:
        """Shut down: tell the radio we are leaving, stop the poller."""
        self._wantExit = True
        try:
            MeshInterface.close(self)   # heartbeat timer + disconnect message
        finally:
            with self._conn_lock:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None
            if self._rxThread.is_alive() and self._rxThread is not threading.current_thread():
                self._rxThread.join(timeout=_REQ_TIMEOUT + 2)
