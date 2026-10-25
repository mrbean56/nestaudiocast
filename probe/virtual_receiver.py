#!/usr/bin/env python3
"""Track 1 — a virtual Cast *receiver* that advertises itself on the LAN.

Goal of this track: can our own container present itself as an extra cast target
in the phone's YouTube app?

What this implements, on the real wire protocol:
  * mDNS `_googlecast._tcp` advertisement with the same TXT keys real devices use
    (id/fn/md/ve/ca/rs/st/ic/rm), so the device shows up in a Cast device list.
  * TLS server on 8009 speaking CASTv2 (`CastMessage` protobuf, length-prefixed).
  * namespaces: tp.connection, tp.heartbeat, receiver, media, tp.deviceauth.
  * a minimal "Default Media Receiver" whose playhead advances in real time.

What it deliberately does NOT have: a Google-signed device certificate. Every real
Cast sender (Cast SDK, YouTube app, Chrome) validates the receiver's certificate
chain against Google's root during `tp.deviceauth`. Our chain is self-signed, so
this is the exact point where a legitimate sender is expected to refuse. The
receiver logs the auth exchange either way, which is the evidence we want.

Usage:  python probe/virtual_receiver.py [--port 8009] [--name "Relais Cast PoC"]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from pychromecast.generated import cast_channel_pb2  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERT = os.path.join(ROOT, "certs", "virtual-cast.crt")
KEY = os.path.join(ROOT, "certs", "virtual-cast.key")
LOG = os.path.join(ROOT, "evidence", "11-virtual-receiver.log")

NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_HB = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECV = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
NS_AUTH = "urn:x-cast:com.google.cast.tp.deviceauth"
NS_DEVINFO = "urn:x-cast:com.google.cast.deviceinfo"

DMR = "CC1AD845"


def log(ev, **kw):
    rec = {"t": round(time.time(), 3), "ev": ev, **kw}
    line = json.dumps(rec, ensure_ascii=False)
    print(line)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


class Session:
    """One launched receiver app instance."""

    def __init__(self, app_id, transport_id):
        self.app_id = app_id
        self.transport_id = transport_id
        self.media_session_id = 1
        self.state = "IDLE"
        self.content_id = None
        self.content_type = None
        self.playback_rate = 1.0
        self._anchor_wall = None   # wall clock when currentTime was last set
        self._anchor_time = 0.0
        self.volume = {"level": 1.0, "muted": False}

    def current_time(self) -> float:
        if self.state == "PLAYING" and self._anchor_wall is not None:
            return self._anchor_time + (time.time() - self._anchor_wall) * self.playback_rate
        return self._anchor_time

    def seek(self, t):
        self._anchor_time = max(0.0, t)
        self._anchor_wall = time.time()

    def set_rate(self, r):
        self._anchor_time = self.current_time()
        self._anchor_wall = time.time()
        self.playback_rate = r

    def media_status(self) -> dict:
        return {
            "mediaSessionId": self.media_session_id,
            "playbackRate": self.playback_rate,
            "playerState": self.state,
            "currentTime": round(self.current_time(), 3),
            "supportedMediaCommands": 274447,
            "volume": self.volume,
            "media": {"contentId": self.content_id or "",
                      "contentType": self.content_type or "",
                      "streamType": "BUFFERED",
                      "mediaCategory": "AUDIO"},
        }


class VirtualReceiver:
    def __init__(self, name, port=8009, model="Chromecast"):
        self.name = name
        self.port = port
        self.model = model
        self.uuid = str(uuid.uuid4())
        self.device_id = self.uuid.replace("-", "")
        self.session: Session | None = None
        self.volume = {"level": 1.0, "muted": False}
        self.senders: list[socket.socket] = []
        self.lock = threading.Lock()
        self.mzc = None

    # ---------- mDNS ----------
    def advertise(self):
        from zeroconf import ServiceInfo, Zeroconf

        ip = socket.inet_aton(self._local_ip())
        props = {
            "id": self.device_id,
            "cd": self.device_id[:8].upper(),
            "rm": "",
            "ve": "05",
            "md": self.model,
            "ic": "/setup/icon.png",
            "fn": self.name,
            "ca": "201221",
            "st": "0",
            "bs": "FA8FCA70A1B2",
            "nf": "1",
            "rs": "",
        }
        info = ServiceInfo(
            "_googlecast._tcp.local.",
            f"{self.model}-{self.device_id[:32]}._googlecast._tcp.local.",
            addresses=[ip], port=self.port, properties=props,
            server=f"{self.device_id[:16]}.local.",
        )
        self.mzc = Zeroconf()
        self.mzc.register_service(info)
        log("mdns_advertised", name=self.name, id=self.device_id, port=self.port,
            txt=props)

    def _local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.2.1", 9))
            return s.getsockname()[0]
        finally:
            s.close()

    # ---------- server ----------
    def serve_forever(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(8)
        log("listening", port=self.port, cert=CERT)
        while True:
            raw, addr = srv.accept()
            threading.Thread(target=self._client, args=(raw, addr), daemon=True).start()

    def _client(self, raw, addr):
        try:
            tls = ctx = None
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(CERT, KEY)
            tls = ctx.wrap_socket(raw, server_side=True)
        except Exception as exc:  # noqa: BLE001
            log("tls_handshake_failed", peer=addr[0], error=f"{type(exc).__name__}: {exc}")
            raw.close()
            return
        log("tls_established", peer=addr[0], port=addr[1], cipher=tls.cipher()[0])
        with self.lock:
            self.senders.append(tls)
        buf = b""
        try:
            while True:
                data = tls.recv(65536)
                if not data:
                    # A *clean* close used to fall through silently, which made
                    # "sender_disconnected = 0" look like "the sender abandons" when it
                    # only meant "we never logged clean closes" (cross-review §1 A7).
                    log("sender_closed_cleanly", peer=addr[0])
                    break
                buf += data
                while len(buf) >= 4:
                    (n,) = int.from_bytes(buf[:4], "big"),
                    if len(buf) < 4 + n:
                        break
                    body, buf = buf[4:4 + n], buf[4 + n:]
                    msg = cast_channel_pb2.CastMessage()
                    msg.ParseFromString(body)
                    try:
                        payload = json.loads(msg.payload_utf8) if msg.payload_utf8 else {}
                    except json.JSONDecodeError:
                        payload = {"_raw": msg.payload_utf8[:200]}
                    self._on_message(tls, msg, payload, addr, msg.payload_binary)
        except Exception as exc:  # noqa: BLE001
            log("sender_disconnected", peer=addr[0], error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.lock:
                if tls in self.senders:
                    self.senders.remove(tls)
            try:
                tls.close()
            except Exception:  # noqa: BLE001
                pass

    def _send(self, sock, namespace, payload, dest="*", source="receiver-0"):
        msg = cast_channel_pb2.CastMessage(
            protocol_version=cast_channel_pb2.CastMessage.CASTV2_1_0,
            source_id=source, destination_id=dest, namespace=namespace,
            payload_type=cast_channel_pb2.CastMessage.STRING,
            payload_utf8=json.dumps(payload))
        blob = msg.SerializeToString()
        try:
            sock.sendall(len(blob).to_bytes(4, "big") + blob)
        except Exception:  # noqa: BLE001
            pass

    def _broadcast(self, namespace, payload):
        src = self.session.transport_id if (self.session and namespace == NS_MEDIA) else "receiver-0"
        with self.lock:
            for s in list(self.senders):
                self._send(s, namespace, payload, source=src)

    def _receiver_status(self):
        apps = []
        if self.session:
            apps.append({
                "appId": self.session.app_id, "appType": "WEB",
                "displayName": "Default Media Receiver", "iconUrl": "",
                "isIdleScreen": False, "launchedFromCloud": False,
                "namespaces": [{"name": NS_MEDIA}, {"name": NS_CONN}],
                "sessionId": self.session.transport_id,
                "statusText": "Virtual receiver", "transportId": self.session.transport_id,
            })
        return {"type": "RECEIVER_STATUS",
                "status": {"applications": apps, "volume": self.volume}}

    def _media_status(self):
        if not self.session:
            return {"type": "MEDIA_STATUS", "status": []}
        return {"type": "MEDIA_STATUS", "status": [self.session.media_status()]}

    def _send_binary(self, sock, namespace, payload_bytes, dest="*", source="receiver-0"):
        msg = cast_channel_pb2.CastMessage(
            protocol_version=cast_channel_pb2.CastMessage.CASTV2_1_0,
            source_id=source, destination_id=dest, namespace=namespace,
            payload_type=cast_channel_pb2.CastMessage.BINARY,
            payload_binary=payload_bytes)
        blob = msg.SerializeToString()
        try:
            sock.sendall(len(blob).to_bytes(4, "big") + blob)
        except Exception:  # noqa: BLE001
            pass

    def _on_message(self, sock, msg, payload, addr, raw_binary=b""):
        ns, src, dst = msg.namespace, msg.source_id, msg.destination_id
        ptype = payload.get("type") if isinstance(payload, dict) else None
        rid = payload.get("requestId") if isinstance(payload, dict) else None
        if ns != NS_AUTH:
            log("recv", ns=ns, src=src, dst=dst, type=ptype, peer=addr[0], payload=payload)

        def reply(ns_, body):
            if rid is not None:
                body = {**body, "requestId": rid}
            self._send(sock, ns_, body, dest=src)

        if ns == NS_CONN:
            if ptype == "CONNECT":
                log("sender_connected", src=src, dst=dst)
                if dst == "receiver-0" or (self.session and dst == self.session.transport_id):
                    self._send(sock, NS_RECV, self._receiver_status(), dest=src)
                    if self.session:
                        self._send(sock, NS_MEDIA, self._media_status(), dest=src)
            return

        if ns == NS_HB:
            if ptype == "PING":
                self._send(sock, NS_HB, {"type": "PONG"}, dest=src)
            return

        if ns == NS_AUTH:
            # Real senders open with a DeviceAuthMessage carrying an AuthChallenge; the
            # receiver must answer with its device certificate chain, which the sender
            # validates against Google's Cast root. We answer correctly but with a
            # self-signed certificate, so this is the exact point of failure for a DIY
            # receiver. Log every step: this is the track-1 evidence.
            dm = cast_channel_pb2.DeviceAuthMessage()
            try:
                dm.ParseFromString(raw_binary)
            except Exception as exc:  # noqa: BLE001
                log("deviceauth_unparsable", peer=addr[0], n=len(raw_binary),
                    error=f"{type(exc).__name__}")
                return
            which = ("challenge" if dm.HasField("challenge")
                     else "response" if dm.HasField("response")
                     else "error" if dm.HasField("error") else "unknown")
            log("deviceauth_message", peer=addr[0], kind=which,
                n_bytes=len(raw_binary),
                sender_nonce=dm.challenge.sender_nonce.hex()[:32] if which == "challenge" else None,
                signature_algorithm=int(dm.challenge.signature_algorithm) if which == "challenge" else None)
            if which != "challenge":
                return
            # sign the sender nonce with our own key, as a real device would
            import base64
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            with open(KEY, "rb") as fh:
                priv = serialization.load_pem_private_key(fh.read(), password=None)
            sig = priv.sign(dm.challenge.sender_nonce, padding.PKCS1v15(), hashes.SHA256())
            with open(CERT, "rb") as fh:
                pem = fh.read()
            der = ssl.PEM_cert_to_DER_cert(pem.decode())
            reply = cast_channel_pb2.DeviceAuthMessage()
            reply.response.client_auth_certificate = der
            reply.response.signature = sig
            reply.response.sender_nonce = dm.challenge.sender_nonce
            reply.response.signature_algorithm = cast_channel_pb2.RSASSA_PKCS1v15
            reply.response.hash_algorithm = cast_channel_pb2.SHA256
            self._send_binary(sock, NS_AUTH, reply.SerializeToString(), dest=src)
            log("deviceauth_reply_sent", peer=addr[0], cert="self-signed (not Google-signed)",
                chain_len=1, sig_len=len(sig))
            return

        if ns == NS_RECV:
            if ptype == "GET_STATUS":
                reply(NS_RECV, self._receiver_status())
            elif ptype == "LAUNCH":
                app_id = payload.get("appId")
                self.session = Session(app_id, str(uuid.uuid4()))
                self.session.content_type = "audio/mp4"
                log("launched", app_id=app_id, transport=self.session.transport_id)
                reply(NS_RECV, self._receiver_status())
                self._broadcast(NS_RECV, self._receiver_status())
            elif ptype == "STOP":
                log("stopped", session=payload.get("sessionId"))
                self.session = None
                reply(NS_RECV, self._receiver_status())
                self._broadcast(NS_RECV, self._receiver_status())
            elif ptype == "SET_VOLUME":
                vol = payload.get("volume") or {}
                self.volume.update({k: v for k, v in vol.items() if k in ("level", "muted")})
                log("set_volume", volume=self.volume)
                reply(NS_RECV, self._receiver_status())
                self._broadcast(NS_RECV, self._receiver_status())
            return

        if ns == NS_MEDIA:
            if not self.session:
                if ptype == "GET_STATUS":
                    reply(NS_MEDIA, {"type": "MEDIA_STATUS", "status": []})
                else:
                    reply(NS_MEDIA, {"type": "INVALID_REQUEST", "reason": "INVALID_COMMAND"})
                return
            s = self.session
            if ptype == "GET_STATUS":
                reply(NS_MEDIA, self._media_status())
            elif ptype == "LOAD":
                media = payload.get("media") or {}
                s.content_id = media.get("contentId")
                s.content_type = media.get("contentType")
                s.media_session_id = (s.media_session_id or 0) + 1
                s.seek(float(payload.get("currentTime") or 0))
                s.state = "PLAYING" if payload.get("autoplay", True) else "PAUSED"
                log("media_loaded", content=media.get("contentId"),
                    contentType=media.get("contentType"), from_time=payload.get("currentTime"))
                reply(NS_MEDIA, self._media_status())
                self._broadcast(NS_MEDIA, self._media_status())
            elif ptype in ("PLAY", "PAUSE", "STOP"):
                s._anchor_time = s.current_time()
                s._anchor_wall = time.time()
                s.state = {"PLAY": "PLAYING", "PAUSE": "PAUSED", "STOP": "IDLE"}[ptype]
                log("media_state", state=s.state)
                reply(NS_MEDIA, self._media_status())
                self._broadcast(NS_MEDIA, self._media_status())
            elif ptype == "SEEK":
                s.seek(float(payload.get("currentTime") or 0))
                log("media_seek", to=payload.get("currentTime"))
                reply(NS_MEDIA, self._media_status())
                self._broadcast(NS_MEDIA, self._media_status())
            elif ptype == "SET_PLAYBACK_RATE":
                s.set_rate(float(payload.get("playbackRate") or 1.0))
                log("media_rate", rate=s.playback_rate)
                reply(NS_MEDIA, self._media_status())
            else:
                reply(NS_MEDIA, {"type": "INVALID_REQUEST", "reason": "INVALID_COMMAND"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8009)
    ap.add_argument("--name", default="Relais Cast PoC")
    ap.add_argument("--model", default="Chromecast")
    ap.add_argument("--no-mdns", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    vr = VirtualReceiver(a.name, a.port, a.model)
    if not a.no_mdns:
        vr.advertise()
    log("start", name=a.name, port=a.port, id=vr.device_id)
    vr.serve_forever()
