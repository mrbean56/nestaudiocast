"""Minimal, dependency-light CASTv2 client — synchronous by design.

Why hand-rolled: the PoC needs (a) exact control over which socket talks to which
device, (b) sub-100 ms polling for A/V offset measurement, and (c) to *serve* the same
protocol from a virtual receiver. All of that wants the raw wire format: TLS plus
4-byte big-endian length-prefixed protobuf `CastMessage`.

Why single-threaded: an OpenSSL SSL* must not be used by two threads at once. An
earlier version read in a background thread while the caller wrote, which corrupted
the record layer (SSL: RECORD_LAYER_FAILURE) on the very first message. One thread
doing select() -> recv() -> send() is race-free and also timestamps arrivals exactly
when they are read, which is what the measurements need.

Namespaces:
  urn:x-cast:com.google.cast.tp.connection   CONNECT / CLOSE
  urn:x-cast:com.google.cast.tp.heartbeat    PING / PONG
  urn:x-cast:com.google.cast.receiver        LAUNCH / GET_STATUS / SET_VOLUME
  urn:x-cast:com.google.cast.media           LOAD / PLAY / PAUSE / SEEK / GET_STATUS
  urn:x-cast:com.google.cast.multizone       GET_STATUS  (group membership)
"""
from __future__ import annotations

import json
import select
import socket
import ssl
import time
import uuid

from pychromecast.generated import cast_channel_pb2

NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
NS_DEVICEAUTH = "urn:x-cast:com.google.cast.tp.deviceauth"
NS_DEVICESTATUS = "urn:x-cast:com.google.cast.deviceinfo"
NS_MULTIZONE = "urn:x-cast:com.google.cast.multizone"

CAST_PORT = 8009


class CastError(RuntimeError):
    pass


class CastLink:
    """One TLS connection to one Cast endpoint (device, group host or our own)."""

    def __init__(self, host: str, port: int = CAST_PORT, timeout: float = 10.0,
                 source_id: str | None = None):
        self.host = host
        self.port = port
        self.source_id = source_id or f"sender-{uuid.uuid4().hex[:8]}"
        self._req = 0
        self._buf = b""
        self.inbox: list[dict] = []
        self.closed = False
        self.sock = None
        self.tls = None

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        # Cast endpoints rate-limit connections and answer plaintext when hammered
        # (SSLError WRONG_VERSION_NUMBER); retry with backoff instead of failing.
        last = None
        for attempt in range(4):
            try:
                self.sock = socket.create_connection((host, port), timeout=timeout)
                self.sock.settimeout(timeout)
                self.tls = ctx.wrap_socket(self.sock)
                self.tls.setblocking(False)
                last = None
                break
            except (ssl.SSLError, OSError) as exc:
                last = exc
                try:
                    self.sock.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.4 * (attempt + 1))
        if last is not None:
            raise CastError(f"connect to {host}:{port} failed: {type(last).__name__}: {last}")

    # ---------------- wire ----------------
    def send(self, destination: str, namespace: str, payload: dict,
             request_id: int | None = None) -> int | None:
        if request_id is not None:
            payload = {**payload, "requestId": request_id}
        msg = cast_channel_pb2.CastMessage(
            protocol_version=cast_channel_pb2.CastMessage.CASTV2_1_0,
            source_id=self.source_id,
            destination_id=destination,
            namespace=namespace,
            payload_type=cast_channel_pb2.CastMessage.STRING,
            payload_utf8=json.dumps(payload),
        )
        blob = msg.SerializeToString()
        self.tls.sendall(len(blob).to_bytes(4, "big") + blob)
        return request_id

    def pump(self, timeout: float = 0.0) -> int:
        """Read whatever is available (waiting up to `timeout`), dispatch it."""
        got = 0
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            try:
                r, _, _ = select.select([self.tls], [], [], max(0.0, left))
            except (OSError, ValueError):
                self.closed = True
                return got
            if not r:
                return got
            try:
                data = self.tls.recv(65536)
            except ssl.SSLWantReadError:
                continue
            except (OSError, ssl.SSLError):
                self.closed = True
                return got
            if not data:
                self.closed = True
                return got
            self._buf += data
            while len(self._buf) >= 4:
                (n,) = int.from_bytes(self._buf[:4], "big"),
                if len(self._buf) < 4 + n:
                    break
                body, self._buf = self._buf[4:4 + n], self._buf[4 + n:]
                msg = cast_channel_pb2.CastMessage()
                msg.ParseFromString(body)
                try:
                    payload = json.loads(msg.payload_utf8) if msg.payload_utf8 else {}
                except json.JSONDecodeError:
                    payload = {"_raw": msg.payload_utf8[:200]}
                self.inbox.append({
                    "t": time.time(),
                    "namespace": msg.namespace,
                    "source_id": msg.source_id,
                    "destination_id": msg.destination_id,
                    "payload": payload,
                })
                got += 1
                if msg.namespace == NS_HEARTBEAT and payload.get("type") == "PING":
                    self.send(msg.source_id, NS_HEARTBEAT, {"type": "PONG"})
                if len(self.inbox) > 500:      # unsolicited MEDIA_STATUS pile up over hours
                    del self.inbox[:-200]
            if time.time() >= deadline:
                return got

    def keepalive(self, every: float = 5.0):
        """Answer pending PINGs and send our own now and then. A link nobody reads stops
        answering the receiver's heartbeat and gets closed by it (evidence/22: ~24 s)."""
        self.pump(0.0)
        now = time.time()
        if now - getattr(self, "_last_ping", 0.0) >= every:
            self._last_ping = now
            self.send("receiver-0", NS_HEARTBEAT, {"type": "PING"})

    def _next_req(self) -> int:
        self._req += 1
        return self._req

    # ---------------- high level ----------------
    def connect_platform(self):
        self.send("receiver-0", NS_CONNECTION, {"type": "CONNECT"})

    def connect_transport(self, transport_id: str):
        self.send(transport_id, NS_CONNECTION, {"type": "CONNECT"})

    def close(self):
        self.closed = True
        for s in (self.tls, self.sock):
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.03)  # let the endpoint free the slot before the next connect

    def request(self, destination: str, namespace: str, payload: dict,
                timeout: float = 8.0, match: str | None = None) -> dict:
        rid = self._next_req()
        self.send(destination, namespace, payload, request_id=rid)
        deadline = time.time() + timeout
        while True:
            for rec in self.inbox:
                p = rec["payload"] or {}
                if p.get("requestId") == rid and (
                        match is None or match in str(p.get("type", ""))):
                    self.inbox.remove(rec)
                    return rec
            if time.time() >= deadline:
                raise CastError(
                    f"timeout waiting for {namespace} reply to {payload.get('type')} "
                    f"on {self.host}:{self.port}")
            self.pump(min(0.02, max(0.0, deadline - time.time())))

    # ---- receiver / platform ----
    def receiver_status(self, timeout: float = 6.0) -> dict:
        return self.request("receiver-0", NS_RECEIVER, {"type": "GET_STATUS"},
                            timeout)["payload"].get("status", {})

    def device_info(self, timeout: float = 6.0) -> dict:
        return self.request("*", NS_DEVICESTATUS, {"type": "GET_STATUS"}, timeout)["payload"]

    def launch(self, app_id: str, timeout: float = 20.0) -> dict:
        return self.request("receiver-0", NS_RECEIVER,
                            {"type": "LAUNCH", "appId": app_id}, timeout)["payload"]

    def stop_app(self, session_id: str, timeout: float = 10.0) -> dict:
        return self.request("receiver-0", NS_RECEIVER,
                            {"type": "STOP", "sessionId": session_id}, timeout)["payload"]

    def set_receiver_volume(self, level: float | None = None, muted: bool | None = None,
                            timeout: float = 6.0) -> dict:
        vol = {}
        if level is not None:
            vol["level"] = float(level)
        if muted is not None:
            vol["muted"] = bool(muted)
        return self.request("receiver-0", NS_RECEIVER,
                            {"type": "SET_VOLUME", "volume": vol}, timeout)["payload"]

    # ---- media ----
    def media_status(self, transport_id: str, timeout: float = 6.0) -> dict:
        return self.request(transport_id, NS_MEDIA, {"type": "GET_STATUS"}, timeout)["payload"]

    def load(self, transport_id: str, data: dict, timeout: float = 20.0) -> dict:
        return self.request(transport_id, NS_MEDIA, {"type": "LOAD", **data}, timeout)["payload"]

    def media_command(self, transport_id: str, cmd: str, media_session_id: int,
                      fields: dict | None = None, timeout: float = 8.0) -> dict:
        payload = {"type": cmd, "mediaSessionId": media_session_id}
        if fields:
            payload.update(fields)
        return self.request(transport_id, NS_MEDIA, payload, timeout)["payload"]

    # ---- observation ----
    def drain_events(self) -> list[dict]:
        self.pump(0.0)
        out = self.inbox[:]
        self.inbox.clear()
        return out

    def last_media_status(self) -> dict | None:
        self.pump(0.0)
        for rec in reversed(self.inbox):
            if rec["namespace"] == NS_MEDIA and rec["payload"].get("type") == "MEDIA_STATUS":
                return rec["payload"]
        return None


def receiver_app(status: dict) -> tuple[str | None, str | None, str | None]:
    """(app_id, display_name, transport_id) from a receiver_status dict."""
    apps = status.get("applications") or []
    if not apps:
        return None, None, None
    app = apps[0]
    return app.get("appId"), app.get("displayName"), app.get("transportId")


def probe_endpoint(host: str, port: int = CAST_PORT, timeout: float = 8.0) -> dict:
    """Connect + GET_STATUS + media status. Read-only, changes nothing."""
    res = {"host": host, "port": port}
    t0 = time.time()
    link = CastLink(host, port, timeout=timeout)
    res["connect_ms"] = round((time.time() - t0) * 1000, 1)
    try:
        link.connect_platform()
        t1 = time.time()
        st = link.receiver_status()
        res["receiver_status_ms"] = round((time.time() - t1) * 1000, 1)
        app_id, app_name, transport = receiver_app(st)
        res["app_id"], res["app_name"], res["transport_id"] = app_id, app_name, transport
        res["volume"] = st.get("volume")
        res["applications"] = st.get("applications")
        if transport:
            try:
                link.connect_transport(transport)
                st2 = link.media_status(transport)
                ms = (st2.get("status") or [{}])[0]
                res["media"] = {
                    "playerState": ms.get("playerState"),
                    "currentTime": ms.get("currentTime"),
                    "mediaSessionId": ms.get("mediaSessionId"),
                    "contentId": (ms.get("media") or {}).get("contentId"),
                    "contentType": (ms.get("media") or {}).get("contentType"),
                }
            except CastError as exc:
                res["media_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        link.close()
    return res


if __name__ == "__main__":
    targets = [
        ("tv_chromecast_salon", "192.168.2.80", 8009),
        ("nest_fenetre_vert", "192.168.2.61", 8009),
        ("group_tous", "192.168.2.61", 32012),
    ]
    out = []
    for label, h, p in targets:
        r = probe_endpoint(h, p)
        r["label"] = label
        out.append(r)
        print(f"\n=== {label} {h}:{p}")
        for k, v in r.items():
            if k not in ("label", "host", "port", "applications"):
                print(f"    {k}: {v}")
    with open("evidence/04-raw-castv2-probe.json", "w") as fh:
        json.dump(out, fh, indent=2)
    print("\nsaved evidence/04-raw-castv2-probe.json")
