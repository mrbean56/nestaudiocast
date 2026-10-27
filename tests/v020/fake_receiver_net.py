"""A localhost CASTv2 receiver, on the real wire protocol (contracts §3).

The point: exercise ``lib/v020/chromecast.SenderLink`` over TLS, protobuf ``CastMessage``
framing, the ``receiver``/``tp.connection``/``tp.heartbeat`` namespaces and our own namespace —
not against an in-memory stand-in. Nothing here touches a device, a speaker, the group or the TV:

* it binds ``127.0.0.1`` only, on an ephemeral port;
* TLS with a throwaway self-signed certificate generated for the run (never committed);
* no audio, no media file, no mDNS: it is a protocol peer, not a Chromecast.

It is also the instrument behind ``evidence/42-*``: ``python tests/v020/fake_receiver_net.py
--bench --out evidence/42-sender-link-fake.json`` runs the measurements of notes/labo-link.md
(clock residual under a 0–40 ms RTT distribution, takeover latency, reconnect after a forced
socket close, bounded inbox) and writes them down.

The synthetic network model, deliberately explicit:

* every ``clock`` message waits ``d_in`` before the receiver reads its clock, and the reply waits
  ``d_out``, with ``d_in + d_out`` = a random RTT in ``rtt_ms`` (default 0–40 ms);
* ``d_in − d_out`` = the path asymmetry: ``path_asym_ms`` plus jitter of ±``asym_jitter_ms``.
  A symmetric path makes the midpoint estimate exact; a fixed asymmetry biases it and no
  one-sided estimator can see it (notes/labo-link.md L1.4). The fake is the only place where
  the truth is known, so both are measured and reported.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import ssl
import sys
import tempfile
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LIB = os.path.join(ROOT, "lib")
for _p in (LIB, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from v020 import contracts as C  # noqa: E402

NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_HB = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"  # the google receiver namespace
NS_OURS = C.NS_RECEIVER  # urn:x-cast:com.beansden.nestaudiocast.v1

PB = None


def _pb():
    """The protobuf module, imported on first use (CI installs no dependencies)."""
    global PB
    if PB is None:
        from pychromecast.generated import cast_channel_pb2  # noqa: PLC0415

        PB = cast_channel_pb2
    return PB


_CERT_CACHE: tuple[str, str] | None = None


def _tmpdir() -> str:
    preferred = "/tmp/opencode"
    if os.path.isdir(preferred) and os.access(preferred, os.W_OK):
        return preferred
    return tempfile.gettempdir()


def _cert_pair() -> tuple[str, str]:
    """A throwaway self-signed certificate for 127.0.0.1, generated once per process."""
    global _CERT_CACHE
    if _CERT_CACHE is not None:
        return _CERT_CACHE
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nac-fake-cast")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    folder = tempfile.mkdtemp(prefix="nac-fake-cast-", dir=_tmpdir())
    crt = os.path.join(folder, "fake.crt")
    pem = os.path.join(folder, "fake.key")
    with open(crt, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(pem, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    os.chmod(pem, 0o600)
    _CERT_CACHE = (crt, pem)
    return _CERT_CACHE


class _Client:
    def __init__(self, sock: ssl.SSLSocket, addr) -> None:
        self.sock = sock
        self.addr = addr
        self.lock = threading.Lock()
        self.source_id: str | None = None
        self.closed = False


class FakeReceiverNet:
    """One localhost Chromecast: TLS, CASTv2 framing, receiver + our namespace, 4 Hz states."""

    def __init__(
        self,
        app_id: str = C.CONFIG_DEFAULTS["APP_ID"],
        host: str = "127.0.0.1",
        port: int = 0,
        clock_offset_ms: float = 86_400_000.0,
        rtt_ms: tuple[float, float] = (0.0, 40.0),
        split: float = 0.5,
        path_asym_ms: float = 0.0,
        asym_jitter_ms: float = 0.5,
        state_hz: float = 4.0,
        seed: int = 1,
        log=None,
        mute_hello: bool = False,
    ) -> None:
        self.app_id = app_id
        self.host, self.port = host, port
        self.clock_offset_ms = float(clock_offset_ms)
        self.rtt_ms = (float(rtt_ms[0]), float(rtt_ms[1]))
        self.split = float(split)
        self.path_asym_ms = float(path_asym_ms)
        self.asym_jitter_ms = float(asym_jitter_ms)
        self.state_hz = float(state_hz)
        self.rng = random.Random(seed)
        self.log = log or (lambda *a, **k: None)
        #: a wedged app instance: the transport connects but the first hello never comes
        self.mute_hello = bool(mute_hello)
        self._hello_muted_once = False

        # app / session
        self.app: dict | None = None
        self.foreign_app: dict | None = None
        self.volume = {"level": 0.5, "muted": False}

        # media model (same kinematics as tests/v020/fakes/fake_receiver.py)
        self.media = 0.0
        self.rate = 1.0
        self.applied_rate = 1.0
        self.player_state = C.PLAYER_IDLE
        self.buffered_end = 0.0
        self._anchor_ms: float | None = None
        self._playing = False
        self.media_loaded = False

        # counters, useful in tests and in evidence
        self.last_seq = 0
        self.frames = 0
        self.hellos_sent = 0
        self.states_sent = 0
        self.clients: list[_Client] = []
        self._guard = threading.Lock()
        self._stopping = threading.Event()
        self._srv: socket.socket | None = None
        self._ctx: ssl.SSLContext | None = None
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> "FakeReceiverNet":
        crt, pem = _cert_pair()
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(crt, pem)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(8)
        srv.settimeout(0.2)
        self._srv = srv
        self.port = srv.getsockname()[1]
        self._spawn(self._accept_loop, "accept")
        if self.state_hz > 0:
            self._spawn(self._state_loop, "states")
        self.log("fake_started", host=self.host, port=self.port)
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        self._close_clients()
        for th in self._threads:
            th.join(timeout=1.0)
        self._threads.clear()

    def _spawn(self, fn, name: str) -> None:
        th = threading.Thread(target=fn, name=f"fake-{name}", daemon=True)
        th.start()
        self._threads.append(th)

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                raw, addr = self._srv.accept()
            except TimeoutError:
                continue  # srv has a 0.2 s timeout: keep accepting
            except (OSError, AttributeError):
                return
            if self._stopping.is_set():
                try:
                    raw.close()
                except OSError:
                    pass
                return
            th = threading.Thread(target=self._client_loop, args=(raw, addr), daemon=True)
            th.start()

    def _close_clients(self) -> None:
        with self._guard:
            clients, self.clients = self.clients, []
        for client in clients:
            self._shutdown(client)

    def _shutdown(self, client: _Client) -> None:
        client.closed = True
        try:
            client.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.sock.close()
        except OSError:
            pass

    # -- wire ---------------------------------------------------------------------------------
    def _client_loop(self, raw: socket.socket, addr) -> None:
        try:
            tls = self._ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            self.log("tls_failed", peer=addr[0], error=f"{type(exc).__name__}: {exc}")
            try:
                raw.close()
            except OSError:
                pass
            return
        try:
            # Without this, Nagle + delayed ACK adds up to ~40 ms to a small write: that is the
            # sporadic RTT outlier the clock estimator was seeing, not queueing.
            tls.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        client = _Client(tls, addr)
        with self._guard:
            self.clients.append(client)
        # An OpenSSL SSL* must not be used by two threads at once (castv2 says so too). Every
        # send (state thread, test thread, this one) and this receive take `client.lock`, and
        # the receive uses a short timeout so a write never waits long behind it.
        try:
            tls.settimeout(0.005)
        except OSError:
            pass
        buf = b""
        try:
            while not self._stopping.is_set() and not client.closed:
                try:
                    with client.lock:
                        if client.closed:
                            break
                        data = tls.recv(65536)
                except TimeoutError:
                    continue
                except (ssl.SSLError, OSError):
                    break
                if not data:
                    break
                buf += data
                while len(buf) >= 4:
                    (n,) = (int.from_bytes(buf[:4], "big"),)
                    if len(buf) < 4 + n:
                        break
                    body, buf = buf[4 : 4 + n], buf[4 + n :]
                    msg = _pb().CastMessage()
                    msg.ParseFromString(body)
                    self.frames += 1
                    try:
                        payload = json.loads(msg.payload_utf8) if msg.payload_utf8 else {}
                    except json.JSONDecodeError:
                        payload = {}
                    try:
                        self._on_message(client, msg, payload, msg.payload_binary)
                    except Exception as exc:  # noqa: BLE001
                        self.log("dispatch_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            with self._guard:
                if client in self.clients:
                    self.clients.remove(client)
            client.closed = True
            try:
                tls.close()
            except OSError:
                pass

    def _send(
        self, client: _Client, namespace: str, payload: dict, source: str = "receiver-0", dest: str = "*"
    ) -> None:
        if client.closed:
            return
        msg = _pb().CastMessage(
            protocol_version=_pb().CastMessage.CASTV2_1_0,
            source_id=source,
            destination_id=dest,
            namespace=namespace,
            payload_type=_pb().CastMessage.STRING,
            payload_utf8=json.dumps(payload),
        )
        blob = msg.SerializeToString()
        with client.lock:
            try:
                client.sock.sendall(len(blob).to_bytes(4, "big") + blob)
            except (OSError, ssl.SSLError):
                client.closed = True

    def broadcast(self, namespace: str, payload: dict, source: str = "receiver-0") -> None:
        with self._guard:
            clients = list(self.clients)
        for client in clients:
            self._send(client, namespace, payload, source=source)

    # -- protocol ----------------------------------------------------------------------------
    def _on_message(self, client: _Client, msg, payload: dict, binary: bytes = b"") -> None:
        ns = msg.namespace
        client.source_id = msg.source_id
        if ns == NS_HB:
            if payload.get("type") == "PING":
                self._send(client, NS_HB, {"type": "PONG"}, dest=msg.source_id)
            return
        if ns == NS_CONN:
            if payload.get("type") == "CONNECT":
                self._on_connect(client, msg.destination_id, msg.source_id)
            return
        if ns == NS_RECEIVER:
            self._on_receiver(client, payload, msg.source_id)
            return
        if ns == NS_OURS:
            self._on_ours(client, payload, msg.source_id)
            return

    def _on_connect(self, client: _Client, dst: str, src: str) -> None:
        if dst == "receiver-0":
            self._send(client, NS_RECEIVER, self._status_payload(), dest=src)
        elif self.app and dst == self.app["transportId"]:
            if self.mute_hello and not self._hello_muted_once:
                self._hello_muted_once = True
                self.log("mute_hello: first hello withheld")
                return
            self.hellos_sent += 1
            self._send(
                client,
                NS_OURS,
                {
                    "type": C.MSG_HELLO,
                    "seq": self.last_seq,
                    "rx_ms": self._rx_ms(),
                    "ua": "fake_receiver_net/0.2",
                    "codecs": {"h264": True, "hevc": False, "vp9": False, "av1": False},
                    "version": "0.2.0-fake-net",
                },
                source=self.app["transportId"],
                dest=src,
            )

    def _on_receiver(self, client: _Client, payload: dict, src: str) -> None:
        rid = payload.get("requestId")
        typ = payload.get("type")

        def reply(body: dict) -> None:
            if rid is not None:
                body = {**body, "requestId": rid}
            self._send(client, NS_RECEIVER, body, dest=src)

        if typ == "GET_STATUS":
            reply(self._status_payload())
        elif typ == "LAUNCH":
            self.app = {
                "appId": payload.get("appId") or self.app_id,
                "sessionId": str(uuid.uuid4()),
                "transportId": f"transport-{uuid.uuid4().hex[:8]}",
                "displayName": "VideoNetCaster",
            }
            self.foreign_app = None
            self.log("launched", app_id=self.app["appId"], transport=self.app["transportId"])
            reply(self._status_payload())
            self.broadcast(NS_RECEIVER, self._status_payload())
        elif typ == "STOP":
            self.log("stopped", session_id=payload.get("sessionId"))
            self._drop_session()
            reply(self._status_payload())
            self.broadcast(NS_RECEIVER, self._status_payload())
        elif typ == "SET_VOLUME":
            vol = payload.get("volume") or {}
            self.volume.update({k: v for k, v in vol.items() if k in ("level", "muted")})
            reply(self._status_payload())
            self.broadcast(NS_RECEIVER, self._status_payload())

    def _on_ours(self, client: _Client, payload: dict, src: str) -> None:
        if not self.app or self.app.get("appId") != self.app_id:
            return  # the app is not ours any more: no namespace
        typ = payload.get("type")
        seq = payload.get("seq")
        if isinstance(seq, int):
            self.last_seq = seq
        transport = self.app["transportId"]

        def emit_state() -> None:
            state = self._state_payload(seq)
            self.states_sent += 1
            self._send(client, NS_OURS, state, source=transport, dest=src)

        if typ == C.MSG_CLOCK:
            d_in, d_out = self._delay_pair()
            if d_in:
                time.sleep(d_in)
            rx = self._rx_ms()
            if d_out:
                time.sleep(d_out)
            self._send(
                client,
                NS_OURS,
                {"type": C.MSG_CLOCK_REPLY, "seq": seq, "rx_ms": rx},
                source=transport,
                dest=src,
            )
            return
        if typ == C.MSG_LOAD:
            self.media = float(payload.get("media", 0.0))
            self._anchor_ms = self._rx_ms()
            self._playing = bool(payload.get("autoplay", False))
            self.player_state = C.PLAYER_PLAYING if self._playing else C.PLAYER_PAUSED
            self.buffered_end = self.media + 30.0
            self.media_loaded = True
            emit_state()
            return
        if typ == C.MSG_PLAY_AT:
            self.media = float(payload["media"])
            self._anchor_ms = float(payload["rx_ms"])
            self._playing = True
            self.player_state = C.PLAYER_PLAYING
            self.media_loaded = True
            emit_state()
            return
        if typ == C.MSG_PAUSE:
            self.media = self._media_at(self._rx_ms())
            self._playing = False
            self.player_state = C.PLAYER_PAUSED
            emit_state()
            return
        if typ == C.MSG_SEEK:
            self.media = float(payload["media"])
            self._anchor_ms = self._rx_ms()
            emit_state()
            return
        if typ == C.MSG_RATE:
            self.media = self._media_at(self._rx_ms())
            self._anchor_ms = self._rx_ms()
            self.rate = float(payload["value"])
            self.applied_rate = 1.0  # evidence/17: the real device echoes and ignores
            emit_state()
            return
        if typ in (C.MSG_STOP, C.MSG_IDLE):
            self._playing = False
            self.player_state = C.PLAYER_IDLE
            self.media = 0.0
            self.media_loaded = False
            emit_state()
            return

    # -- clock / media model ------------------------------------------------------------------
    def _rx_ms(self) -> float:
        """The fake receiver's clock: monotonic + an offset, like a page's timeOrigin."""
        return time.monotonic() * 1000.0 + self.clock_offset_ms

    def _media_at(self, rx_ms: float) -> float:
        if not self._playing or self._anchor_ms is None:
            return self.media
        return self.media + (rx_ms - self._anchor_ms) / 1000.0 * self.applied_rate

    def _state_payload(self, seq) -> dict:
        rx = self._rx_ms()  # one clock read for the timestamp and the position
        return {
            "type": C.MSG_STATE,
            "seq": seq,
            "rx_ms": rx,
            "media": self._media_at(rx),
            "rate": self.rate,
            "player_state": self.player_state,
            "buffered_end": self.media + 30.0,
            "dropped": 0,
        }

    def _delay_pair(self) -> tuple[float, float]:
        lo, hi = self.rtt_ms
        rtt = self.rng.uniform(lo, hi)
        asym = self.path_asym_ms
        if self.asym_jitter_ms:
            asym += self.rng.uniform(-self.asym_jitter_ms, self.asym_jitter_ms)
        d_in = max(0.0, rtt * self.split + asym)
        d_out = max(0.0, rtt * (1.0 - self.split) - asym)
        return d_in / 1000.0, d_out / 1000.0

    def _state_loop(self) -> None:
        period = 1.0 / self.state_hz if self.state_hz > 0 else 0.25
        while not self._stopping.wait(period):
            if self.media_loaded:
                self.states_sent += 1
                self.broadcast(
                    NS_OURS,
                    self._state_payload(self.last_seq),
                    source=self.app["transportId"] if self.app else "receiver-0",
                )

    # -- receiver status ----------------------------------------------------------------------
    def _status_payload(self) -> dict:
        apps = []
        if self.app:
            apps.append(
                {
                    "appId": self.app["appId"],
                    "appType": "WEB",
                    "displayName": self.app.get("displayName", "VideoNetCaster"),
                    "iconUrl": "",
                    "isIdleScreen": False,
                    "launchedFromCloud": False,
                    "namespaces": [{"name": NS_OURS}, {"name": NS_CONN}],
                    "sessionId": self.app["sessionId"],
                    "statusText": "fake localhost receiver",
                    "transportId": self.app["transportId"],
                }
            )
        elif self.foreign_app:
            apps.append(self.foreign_app)
        return {"type": "RECEIVER_STATUS", "status": {"applications": apps, "volume": dict(self.volume)}}

    def _drop_session(self) -> None:
        self.app = None
        self.media_loaded = False
        self._playing = False
        self.player_state = C.PLAYER_IDLE
        self.media = 0.0

    # -- test controls ------------------------------------------------------------------------
    def takeover(self, app_id: str = "9A1B2C3D", display_name: str = "YouTube") -> None:
        """A foreign sender casts: our app is gone, another one owns the device (§3.3)."""
        self.app = None
        self._drop_session()
        self.foreign_app = {
            "appId": app_id,
            "appType": "WEB",
            "displayName": display_name,
            "iconUrl": "",
            "isIdleScreen": False,
            "launchedFromCloud": False,
            "namespaces": [{"name": "urn:x-cast:com.google.cast.media"}],
            "sessionId": "foreign-session",
            "statusText": "cast by another sender",
            "transportId": "foreign-transport",
        }
        self.broadcast(NS_RECEIVER, self._status_payload())
        self.log("takeover", app_id=app_id)

    def stop_by_other_sender(self) -> None:
        """Another sender stopped the app: RECEIVER_STATUS without applications (§3.3)."""
        self._drop_session()
        self.foreign_app = None
        self.broadcast(NS_RECEIVER, self._status_payload())
        self.log("stopped_by_other_sender")

    def restart_transport(self) -> str:
        """The receiver page reconnects: new transport id, same app, media kept."""
        if not self.app:
            raise RuntimeError("no app running")
        self.app = {
            **self.app,
            "sessionId": str(uuid.uuid4()),
            "transportId": f"transport-{uuid.uuid4().hex[:8]}",
        }
        self.broadcast(NS_RECEIVER, self._status_payload())
        self.log("transport_restarted", transport=self.app["transportId"])
        return self.app["transportId"]

    def drop_clients(self) -> int:
        """Forced socket close, as a Wi-Fi blip or a receiver restart would do."""
        with self._guard:
            clients, self.clients = self.clients, []
        for client in clients:
            self._shutdown(client)
        self.log("clients_dropped", n=len(clients))
        return len(clients)

    def emit_state(self) -> dict:
        """Broadcast one ``state`` now, from the current media model (public test hook)."""
        if not self.app:
            return {}
        payload = self._state_payload(self.last_seq)
        self.states_sent += 1
        self.broadcast(NS_OURS, payload, source=self.app["transportId"])
        return payload

    def burst_states(self, n: int) -> int:
        """``n`` states at once, media increasing by 1 ms each: ordering is checkable."""
        transport = self.app["transportId"] if self.app else "receiver-0"
        with self._guard:
            clients = list(self.clients)
        base = self._rx_ms()
        sent = 0
        for i in range(n):
            payload = {
                "type": C.MSG_STATE,
                "seq": self.last_seq,
                "rx_ms": base + i,
                "media": 1000.0 + i / 1000.0,
                "rate": 1.0,
                "player_state": C.PLAYER_PAUSED,
                "buffered_end": 2000.0,
                "dropped": 0,
            }
            for client in clients:
                self._send(client, NS_OURS, payload, source=transport, dest="*")
            sent += 1
        self.states_sent += sent
        return sent

    def jump_clock(self, delta_ms: float) -> float:
        self.clock_offset_ms += float(delta_ms)
        return self.clock_offset_ms

    def set_rtt(self, lo_ms: float, hi_ms: float) -> None:
        self.rtt_ms = (float(lo_ms), float(hi_ms))

    def set_path_asym(self, ms: float, jitter_ms: float | None = None) -> None:
        self.path_asym_ms = float(ms)
        if jitter_ms is not None:
            self.asym_jitter_ms = float(jitter_ms)

    def set_state_hz(self, hz: float) -> None:
        self.state_hz = float(hz)

    def client_count(self) -> int:
        with self._guard:
            return len(self.clients)


# --- bench: the numbers archived in evidence/42-* -------------------------------------------


def _summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0
    return {
        "n": n,
        "median": round(median, 4),
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
    }


def _new_link(fake: FakeReceiverNet, **kw):
    from v020.chromecast import SenderLink

    return SenderLink(fake.host, fake.app_id, fake.port, log=kw.pop("log", lambda *a, **k: None))


def bench(seed: int = 7, trials: int = 12, pings: int = 8, log_path: str | None = None) -> dict:
    """Measure what notes/labo-link.md claims. Localhost only, no device, no sound."""
    from v020.chromecast import INBOX_MAX, LinkDown, LostDevice, estimate_clock

    out: dict = {"seed": seed, "clock": {}, "takeover": {}, "reconnect": {}, "inbox": {}, "protocol": {}}
    log = open(log_path, "w") if log_path else None

    def say(ev: str, **fields) -> None:
        if log:
            log.write(json.dumps({"ev": ev, **fields}, default=str) + "\n")
            log.flush()

    say("bench_start", seed=seed, trials=trials, pings=pings)

    # 1. clock: a known offset, a 0-40 ms RTT distribution, ±1 ms of path asymmetry.
    fake = FakeReceiverNet(
        rtt_ms=(0.0, 40.0), asym_jitter_ms=0.5, state_hz=0.0, seed=seed, log=say
    ).start()
    link = _new_link(fake)
    status = link.launch()
    errors, residuals, rtts, bounds = [], [], [], []
    per_trial = []
    for i in range(trials):
        est = link.ping(pings)
        err = est.offset_ms - fake.clock_offset_ms
        errors.append(abs(err))
        residuals.append(est.residual_ms)
        rtts.append(est.rtt_ms)
        bounds.append(float((link.last_ping_stats or {}).get("bound_ms", 0.0)))
        per_trial.append(
            {
                "trial": i + 1,
                "error_ms": round(err, 4),
                "residual_ms": round(est.residual_ms, 4),
                "rtt_ms": round(est.rtt_ms, 4),
            }
        )
    out["clock"] = {
        "truth_offset_ms": fake.clock_offset_ms,
        "rtt_distribution_ms": [0.0, 40.0],
        "path_asymmetry_ms": {"fixed": 0.0, "jitter": 0.5},
        "per_trial": per_trial,
        "abs_error_ms": _summary(errors),
        "residual_ms": _summary(residuals),
        "min_rtt_ms": _summary(rtts),
        "absolute_bound_rtt_over_2_ms": _summary(bounds),
        "launch_app_id": (status.get("applications") or [{}])[0].get("appId"),
    }
    say("clock_done", **{k: v for k, v in out["clock"].items() if k != "per_trial"})

    # 2. the blind spot: a fixed path asymmetry no one-sided estimator can see.
    #    (An RTT floor above twice the asymmetry keeps the model's d_out positive.)
    fake.set_rtt(12.0, 40.0)
    fake.set_path_asym(6.0, jitter_ms=0.0)
    biased = [link.ping(pings) for _ in range(4)]
    out["clock"]["fixed_6ms_asymmetry"] = {
        "errors_ms": [round(e.offset_ms - fake.clock_offset_ms, 4) for e in biased],
        "residuals_ms": [round(e.residual_ms, 4) for e in biased],
        "note": "the estimate follows the asymmetry; residual stays blind to it",
    }
    say("asymmetry_done", **out["clock"]["fixed_6ms_asymmetry"])
    fake.set_rtt(0.0, 40.0)
    fake.set_path_asym(0.0, jitter_ms=0.5)

    # 3. a receiver clock jump is flagged, not followed; reset_clock accepts it.
    before = link.ping(pings).offset_ms
    fake.jump_clock(5000.0)
    after_flagged = link.ping(pings)
    flagged = {
        "offset_before_ms": round(before, 4),
        "offset_reported_ms": round(after_flagged.offset_ms, 4),
        "jump_ms": link.clock_jump_ms,
        "flagged": link.clock_flagged,
    }
    link.reset_clock()
    adopted = link.ping(pings)
    flagged["offset_after_reset_ms"] = round(adopted.offset_ms, 4)
    flagged["followed_after_reset"] = abs(adopted.offset_ms - before) > 4000.0
    out["clock"]["jump"] = flagged
    say("jump_done", **flagged)

    # 4. latest() carries a typed Sample from the last state.
    link.send(
        "load",
        url="http://127.0.0.1/media/x/audio.m4a",
        content_type="audio/mp4",
        media=10.0,
        autoplay=True,
    )
    time.sleep(0.4)
    sample = link.latest()
    out["protocol"]["sample"] = (
        None
        if sample is None
        else {
            "rx_ms": round(sample.rx_ms, 3),
            "media": round(sample.media, 4),
            "rate": sample.rate,
            "player_state": sample.player_state,
            "buffered_end": sample.buffered_end,
        }
    )
    out["protocol"]["hellos"] = link.hellos
    out["protocol"]["hello"] = {k: link.hello.get(k) for k in ("ua", "version", "seq")}
    out["protocol"]["seq"] = link.seq
    try:
        link.send("bogus")
        out["protocol"]["unknown_type"] = "ACCEPTED (bug)"
    except ValueError:
        out["protocol"]["unknown_type"] = "ValueError"
    link.close()
    fake.stop()

    # 5. takeover: a foreign app, detected by one poll and by one read.
    fake = FakeReceiverNet(rtt_ms=(0.0, 5.0), state_hz=0.0, seed=seed + 1, log=say).start()
    link = _new_link(fake)
    link.launch()
    link.send(
        "load",
        url="http://127.0.0.1/media/x/audio.m4a",
        content_type="audio/mp4",
        media=3.0,
        autoplay=True,
    )
    time.sleep(0.05)
    fake.takeover()
    t0 = time.monotonic()
    ours = link.is_ours()
    poll_ms = (time.monotonic() - t0) * 1000.0
    out["takeover"] = {
        "via_poll": {
            "returned": ours,
            "polls": 1,
            "poll_ms": round(poll_ms, 3),
            "reason": link.lost_reason,
        },
        "lost_device_raised": None,
    }
    try:
        link.send("pause")
        out["takeover"]["lost_device_raised"] = False
    except LostDevice:
        out["takeover"]["lost_device_raised"] = True
    link.close()
    fake.stop()

    # ... and the event path: an unsolicited RECEIVER_STATUS, caught by one read.
    fake = FakeReceiverNet(rtt_ms=(0.0, 5.0), state_hz=0.0, seed=seed + 2, log=say).start()
    link = _new_link(fake)
    link.launch()
    fake.takeover()
    t0 = time.monotonic()
    link.drain()
    read_ms = (time.monotonic() - t0) * 1000.0
    out["takeover"]["via_read"] = {
        "detected": link.lost_reason is not None,
        "reads": 1,
        "read_ms": round(read_ms, 3),
        "reason": link.lost_reason,
    }
    link.close()
    fake.stop()

    # ... and a stop from another sender (IDLE device while we own it).
    fake = FakeReceiverNet(rtt_ms=(0.0, 5.0), state_hz=0.0, seed=seed + 3, log=say).start()
    link = _new_link(fake)
    link.launch()
    fake.stop_by_other_sender()
    link.drain()
    out["takeover"]["stop_by_other_sender"] = {
        "detected": link.lost_reason is not None,
        "reason": link.lost_reason,
    }
    link.close()
    fake.stop()

    # 6. forced socket close, then a bounded-backoff reconnect that keeps seq and hellos.
    fake = FakeReceiverNet(rtt_ms=(0.0, 5.0), state_hz=0.0, seed=seed + 4, log=say).start()
    link = _new_link(fake)
    link.launch()
    link.send(
        "load",
        url="http://127.0.0.1/media/x/audio.m4a",
        content_type="audio/mp4",
        media=5.0,
        autoplay=True,
    )
    seq_before = link.seq
    hellos_before = link.hellos
    link._service(timeout=0.05)
    transport_before = link.transport
    fake.drop_clients()
    time.sleep(0.1)
    down = None
    try:
        link.send("pause")
        down = "send still worked (bug)"
    except LinkDown as exc:
        down = f"LinkDown: {exc}"
    t0 = time.monotonic()
    link.reconnect()
    reconnect_ms = (time.monotonic() - t0) * 1000.0
    ours_after = link.is_ours()
    seq_after = link.send("pause")  # seq continues where it stopped
    out["reconnect"] = {
        "send_while_down": down,
        "reconnect_ms": round(reconnect_ms, 3),
        "attempts_used": 1 if reconnect_ms < 1000 else None,
        "transport_before": transport_before,
        "transport_after": link.transport,
        "seq_before": seq_before,
        "seq_after_send": seq_after,
        "hellos_before": hellos_before,
        "hellos_after": link.hellos,
        "is_ours_after": ours_after,
        "reconnects": link.reconnects,
    }
    say("reconnect_done", **out["reconnect"])

    # 7. receiver transport restart (new hello) without a socket close.
    hellos_before_restart = link.hellos
    fake.restart_transport()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and link.hellos == hellos_before_restart:
        link._service(timeout=0.05)
    out["reconnect"]["restart_transport"] = {
        "transport": link.transport,
        "hellos": link.hellos,
        "new_hello": link.hellos > hellos_before_restart,
    }
    link.close()
    fake.stop()

    # 8. the bounded inbox under a 1000-message burst (A23).
    fake = FakeReceiverNet(rtt_ms=(0.0, 5.0), state_hz=0.0, seed=seed + 5, log=say).start()
    link = _new_link(fake)
    link.launch()
    link.drain()  # the hello is not part of the burst
    injected = 1000
    link.link.tls.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4_000_000)
    writer = threading.Thread(target=fake.burst_states, args=(injected,), daemon=True)
    writer.start()
    time.sleep(0.5)  # a client that is not draining lets it pile up
    received = 0
    drains = 0
    biggest = 0
    ordered = True
    last_media = -1.0
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and received + link.dropped < injected:
        batch = link.drain()
        drains += 1
        biggest = max(biggest, len(batch))
        for msg in batch:
            if msg.get("type") != C.MSG_STATE:
                continue
            received += 1
            media = msg.get("media", 0.0)
            if media <= last_media:
                ordered = False
            last_media = media
    writer.join(timeout=5.0)
    out["inbox"] = {
        "injected": injected,
        "received": received,
        "dropped": link.dropped,
        "drains": drains,
        "largest_drain": biggest,
        "bound": INBOX_MAX,
        "ordered": ordered,
        "accounted": received + link.dropped >= injected,
    }
    say("inbox_done", **out["inbox"])

    # 9. the estimator without a socket: min-RTT selection on synthetic samples.
    rng = random.Random(seed)
    checks = []
    for _ in range(200):
        truth = 1_234_567.0
        samples = []
        for _ in range(pings):
            rtt = rng.uniform(0.0, 40.0)
            d_in = rtt * 0.5 + rng.uniform(-0.5, 0.5)
            samples.append((1000.0, 1000.0 + d_in + truth, 1000.0 + rtt))
        est, stats = estimate_clock(samples)
        checks.append((abs(est.offset_ms - truth), est.residual_ms))
    out["clock"]["synthetic_offline"] = {
        "trials": len(checks),
        "pings": pings,
        "abs_error_ms": _summary([c[0] for c in checks]),
        "residual_ms": _summary([c[1] for c in checks]),
    }
    link.close()
    fake.stop()

    if log:
        log.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="localhost CASTv2 receiver (contracts §3)")
    ap.add_argument("--bench", action="store_true", help="run the evidence measurements")
    ap.add_argument("--out", default=None, help="write the bench JSON here")
    ap.add_argument("--log", default=None, help="write the bench event log here")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--pings", type=int, default=8)
    args = ap.parse_args(argv)

    if args.bench:
        result = bench(seed=args.seed, trials=args.trials, pings=args.pings, log_path=args.log)
        result["host"] = socket.gethostname()
        result["python"] = sys.version.split()[0]
        result["utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result["where"] = "127.0.0.1 only: TLS + CASTv2 framing, no device, no audio"
        text = json.dumps(result, indent=2)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(text + "\n")
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0

    fake = FakeReceiverNet(log=lambda ev, **kw: print(json.dumps({"ev": ev, **kw})))
    fake.start()
    print(f"localhost CASTv2 receiver on {fake.host}:{fake.port} (ctrl-c to stop)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        fake.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
