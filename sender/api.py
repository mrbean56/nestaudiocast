"""The one HTTP server of the owner app — contracts §5.

One stdlib ``ThreadingHTTPServer`` on ``192.168.2.21:7640`` that answers, in JSON:

    POST /api/share     202 {"session_id", "state"}
    POST /api/control   200 {"ok": true, "state"}
    GET  /api/status    200 {state, session_id, video_id, …}   (the §5 field list)
    GET  /healthz       200 {"ok": true, "owner": "cast-sender"}
    GET  /receiver.html the frozen path, served from receiver/ (404 with a clear message)
    GET  /              a minimal remote UI: no framework, no CDN, no build step
    GET  /media/*       delegated to MediaProxy

Only clients of ``192.168.2.0/24`` are served; everybody else gets ``403`` and a JSON error.
Every error is ``{"error": ...}`` with the right code. Bodies are the §5 bodies, and unknown
fields are refused rather than ignored — a typo in a body must not silently do nothing.

Two additions the contract does not spell out, both required to make §5 work and both proposed
as amendments in ``notes/labo-sender.md``:

* ``GET /assets/measure/<file>`` — read-only, ``assets/measure/`` only, ``.m4a``/``.wav`` only,
  no traversal. ``measure: true`` has to get the click train to the group somehow.
* ``MediaProxy.serve_http(method, path, headers) -> (status, headers, body)`` — optional hook
  for the byte-range endpoints of §4; absent, ``/media/<sid>/<n>`` answers ``501`` and says so.
"""

from __future__ import annotations

import ipaddress
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from v020 import contracts as C

from .session import ControlError, ShareError

LAN_NETWORK = ipaddress.ip_network("192.168.2.0/24")
MEASURE_PREFIX = "/assets/measure/"
MEASURE_SUFFIXES = (".m4a", ".wav")
DEFAULT_MAX_BODY = 64 * 1024

_STATUS_FIELDS = (
    "state", "session_id", "video_id", "position_video", "position_group",
    "digital_error_ms", "p95_ms", "rate", "steady_since", "quiet", "group_owner", "delta_v2",
    "last_event", "last_error",
)


class ApiError(Exception):
    """An answer that is not a normal reply: ``code`` + ``{"error": kind, "detail": …}``."""

    def __init__(self, code: int, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.detail = detail


class SenderAPI:
    """Routing, the LAN allow-list and the JSON contract — the state machine stays in session.py."""

    def __init__(self, sender, *, config, log=None, proxy=None, receiver_dir: str | None = None,
                 assets_dir: str | None = None, extra_allowed=(), max_body: int = DEFAULT_MAX_BODY,
                 ui_title: str = "cast-sender") -> None:
        self.sender = sender
        self.config = config
        self.log = log or (lambda *a, **k: None)
        self.proxy = proxy if proxy is not None else getattr(sender, "_proxy", None)
        self.receiver_dir = receiver_dir or getattr(config, "receiver_dir", None) or "receiver"
        self.assets_dir = assets_dir or getattr(config, "assets_dir", None) or "assets"
        self.extra_allowed = frozenset(str(ip) for ip in extra_allowed)
        self.max_body = int(max_body)
        self.ui_title = ui_title

    # -- LAN only ---------------------------------------------------------------------------
    def allowed(self, client_ip: str) -> bool:
        try:
            address = ipaddress.ip_address(str(client_ip).split("%")[0])
        except ValueError:
            return False
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        if str(address) in self.extra_allowed:
            return True
        try:
            return address in LAN_NETWORK
        except TypeError:      # an IPv6 address is never in an IPv4 network
            return False

    # -- server -----------------------------------------------------------------------------
    def make_server(self, bind: str | None = None, port: int | None = None) -> "SenderServer":
        host = self.config.http_bind if bind is None else bind
        number = self.config.http_port if port is None else int(port)
        return SenderServer((host, number), self)

    def serve_forever(self, bind: str | None = None, port: int | None = None) -> "SenderServer":
        server = self.make_server(bind, port)
        self.logger("http_listening", bind=server.server_address[0],
                    port=server.server_address[1], lan_only=True)
        server.serve_forever()
        return server

    def logger(self, name: str, **fields) -> None:
        try:
            self.log(name, **fields)
        except Exception:  # noqa: BLE001 - logging must never break an answer
            pass

    # -- answers ----------------------------------------------------------------------------
    def status_doc(self) -> dict:
        doc = self.sender.status()
        missing = [key for key in _STATUS_FIELDS if key not in doc]
        if missing:
            raise ApiError(500, "internal_error", f"status is missing {missing}")
        return doc


class SenderServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, api: SenderAPI) -> None:
        self.api = api
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address):  # keep a broken client quiet
        try:
            self.api.logger("http_connection_error", client=str(client_address[0]))
        except Exception:  # noqa: BLE001
            pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cast-sender/0.2.0"
    sys_version = ""

    # the stdlib logger writes to stderr in its own format; we keep one JSONL instead
    def log_message(self, fmt, *args) -> None:  # noqa: D401 - stdlib hook
        pass

    def log_error(self, fmt, *args) -> None:
        pass

    # -- verbs ------------------------------------------------------------------------------
    def do_GET(self) -> None:            # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:           # noqa: N802
        self._dispatch("POST")

    def do_HEAD(self) -> None:           # noqa: N802
        self._dispatch("HEAD")

    def do_PUT(self) -> None:            # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:          # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:         # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:        # noqa: N802
        self._dispatch("OPTIONS")

    # -- dispatch ---------------------------------------------------------------------------
    @property
    def api(self) -> SenderAPI:
        return self.server.api

    def _dispatch(self, method: str) -> None:
        try:
            client = str(self.client_address[0]) if self.client_address else ""
            if not self.api.allowed(client):
                self.api.logger("http_denied", client=client, path=self.path, method=method)
                raise ApiError(403, "forbidden",
                               "only clients of 192.168.2.0/24 may use this service")
            path = urlsplit(self.path).path or "/"
            if path.startswith(C.PATH_MEDIA_PREFIX):
                return self._media(method, path)
            if path.startswith(MEASURE_PREFIX):
                return self._measure_asset(method, path)
            route = _ROUTES.get(path) or _ROUTES.get(path.rstrip("/") or "/")
            if route is None:
                raise ApiError(404, "not_found", f"no route for {path}")
            allowed_method, name = route
            if method != allowed_method:
                raise ApiError(405, "method_not_allowed", f"{path} accepts {allowed_method}")
            return getattr(self, name)()
        except ApiError as exc:
            self._fail(exc.code, exc.message, exc.detail)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - never leave a client without an answer
            self.api.logger("http_exception", path=self.path, error=f"{type(exc).__name__}: {exc}")
            self._fail(500, "internal_error", f"{type(exc).__name__}: {exc}")

    # -- routes -----------------------------------------------------------------------------
    def _h_health(self) -> None:
        self._json(200, {"ok": True, "owner": "cast-sender"})

    def _h_status(self) -> None:
        self._json(200, self.api.status_doc())

    def _h_receiver(self) -> None:
        path = os.path.join(self.api.receiver_dir, "receiver.html")
        if not os.path.isfile(path):
            raise ApiError(404, "receiver_missing",
                           f"{path} is missing: the receiver page is not built in this "
                           f"checkout (it belongs to the receiver task)")
        with open(path, "rb") as handle:
            body = handle.read()
        self._send(200, "text/html; charset=utf-8", body, cache="no-cache")

    def _h_ui(self) -> None:
        self._send(200, "text/html; charset=utf-8", _ui_page(self.api.ui_title).encode(),
                   cache="no-store")

    def _h_share(self) -> None:
        body = self._read_json()
        _reject_unknown(body, {"url", "start", "measure"})
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ApiError(400, "invalid_body", "url (non-empty string) is required")
        start = body.get("start", 0.0)
        if start is None:
            start = 0.0
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            raise ApiError(400, "invalid_body", "start must be a number of media seconds")
        if float(start) < 0:
            raise ApiError(400, "invalid_body", "start must be >= 0")
        measure = body.get("measure", False)
        if not isinstance(measure, bool):
            raise ApiError(400, "invalid_body", "measure must be a boolean")
        try:
            reply = self.api.sender.share(url, start=float(start), measure=measure)
        except ShareError as exc:
            raise ApiError(400, "invalid_body", str(exc)) from None
        except ControlError as exc:
            raise ApiError(exc.code, exc.kind, str(exc)) from None
        self._json(202, {"session_id": reply["session_id"], "state": reply["state"]})

    def _h_control(self) -> None:
        body = self._read_json()
        _reject_unknown(body, {"action", "position", "level", "muted"})
        action = body.get("action")
        if not isinstance(action, str) or action not in C.ACTIONS:
            raise ApiError(400, "invalid_body",
                           f"action must be one of {list(C.ACTIONS)}")
        position, level, muted = body.get("position"), body.get("level"), body.get("muted")
        if position is not None and (isinstance(position, bool)
                                     or not isinstance(position, (int, float))):
            raise ApiError(400, "invalid_body", "position must be a number")
        if level is not None and (isinstance(level, bool) or not isinstance(level, (int, float))):
            raise ApiError(400, "invalid_body", "level must be a number")
        if muted is not None and not isinstance(muted, bool):
            raise ApiError(400, "invalid_body", "muted must be a boolean")
        try:
            reply = self.api.sender.control(
                action,
                position=None if position is None else float(position),
                level=None if level is None else float(level),
                muted=muted)
        except ControlError as exc:
            raise ApiError(exc.code, exc.kind, str(exc)) from None
        self._json(200, {"ok": bool(reply.get("ok")), "state": reply.get("state")})

    # -- /media/* ---------------------------------------------------------------------------
    def _media(self, method: str, path: str) -> None:
        if method not in ("GET", "HEAD"):
            raise ApiError(405, "method_not_allowed", f"{path} accepts GET")
        proxy = self.api.proxy
        if proxy is None:
            raise ApiError(503, "media_proxy_unavailable", "no media proxy is wired")
        relative = path[len(C.PATH_MEDIA_PREFIX):]
        sid, _, name = relative.partition("/")
        if not sid or not name:
            raise ApiError(404, "not_found", "expected /media/<sid>/<name>")
        if name == "manifest.mpd":
            try:
                manifest = proxy.mpd_for(sid)
            except KeyError:
                raise ApiError(410, "session_gone",
                               "this media session id is not open (revoked on session end)")
            except NotImplementedError:
                raise ApiError(501, "media_proxy_stub",
                               "the media proxy is a stub (task B)")
            self._send(200, "application/dash+xml; charset=utf-8", str(manifest).encode(),
                       cache="no-store")
            return
        serve = getattr(proxy, "serve_http", None)
        if serve is None:
            # Task B's proxy serves by mounting itself on the request handler (it can stream a
            # body straight from the upstream socket, which a tuple-returning hook cannot).
            # Contracts §10.4 allows either; `handle` wins when it exists.
            handle = getattr(proxy, "handle", None)
            if handle is not None and handle(self):
                return
            if handle is not None:
                raise ApiError(404, "not_found", f"{path} is not served by the media proxy")
            raise ApiError(501, "media_stream_unavailable",
                           "the media proxy has no byte endpoint (contracts §4/§5, §10.4)")
        try:
            status, headers, body = serve(method, path, dict(self.headers.items()))
        except KeyError:
            raise ApiError(410, "session_gone", "this media session id is not open") from None
        if isinstance(body, (bytes, bytearray)):
            payload, length = bytes(body), None
        else:
            payload, length = b"".join(body), None
        self.send_response(int(status))
        for key, value in (headers or {}).items():
            self.send_header(str(key), str(value))
        self.send_header("Content-Length", str(len(payload) if length is None else length))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    # -- /assets/measure/* (amendment; read-only, two suffixes, no traversal) -----------------
    def _measure_asset(self, method: str, path: str) -> None:
        if method not in ("GET", "HEAD"):
            raise ApiError(405, "method_not_allowed", f"{path} accepts GET")
        name = path[len(MEASURE_PREFIX):]
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ApiError(400, "invalid_path", "a bare file name is expected")
        if not name.lower().endswith(MEASURE_SUFFIXES):
            raise ApiError(403, "forbidden", "only .m4a and .wav measurement assets are served")
        base = os.path.realpath(os.path.join(self.api.assets_dir, "measure"))
        target = os.path.realpath(os.path.join(base, name))
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
            raise ApiError(404, "not_found", f"{name} is not in assets/measure/")
        with open(target, "rb") as handle:
            body = handle.read()
        ctype = "audio/mp4" if name.lower().endswith(".m4a") else "audio/wav"
        self._send(200, ctype, body, cache="no-store")

    # -- bodies -----------------------------------------------------------------------------
    def _read_json(self) -> dict:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            # A browser form cannot set this header: it is what keeps a stray page on the LAN
            # from driving the speakers.
            raise ApiError(415, "unsupported_media_type", "POST bodies must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiError(411, "length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError(400, "invalid_body", "Content-Length is not a number") from None
        if length < 0:
            raise ApiError(400, "invalid_body", "Content-Length is negative")
        if length > self.api.max_body:
            self.close_connection = True
            raise ApiError(413, "payload_too_large",
                           f"at most {self.api.max_body} bytes are accepted")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            raise ApiError(400, "invalid_body", "a JSON object is required")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "invalid_json", str(exc)) from None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "the body must be a JSON object")
        return body

    # -- writing ----------------------------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=False).encode("utf-8")
        self._send(code, "application/json; charset=utf-8", body, cache="no-store")

    def _fail(self, code: int, message: str, detail: str | None = None) -> None:
        payload = {"error": message}
        if detail:
            payload["detail"] = detail
        try:
            self._json(code, payload)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        # A browser asks for /favicon.ico on its own; that must not become "last_event".
        if code >= 400 and not self.path.startswith("/favicon"):
            self.api.logger("http_error", code=code, error=message, path=self.path,
                            method=self.command, client=str(self.client_address[0])
                            if self.client_address else None)

    def _send(self, code: int, content_type: str, body: bytes, *, cache: str = "no-store") -> None:
        try:
            reason = HTTPStatus(code).phrase
        except ValueError:
            reason = None
        self.send_response(code, reason)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


_ROUTES = {
    C.PATH_HEALTH: ("GET", "_h_health"),
    C.PATH_STATUS: ("GET", "_h_status"),
    C.PATH_UI: ("GET", "_h_ui"),
    C.PATH_RECEIVER: ("GET", "_h_receiver"),
    C.PATH_SHARE: ("POST", "_h_share"),
    C.PATH_CONTROL: ("POST", "_h_control"),
}


def _reject_unknown(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise ApiError(400, "invalid_body",
                       f"unknown field(s) {unknown}; accepted: {sorted(allowed)}")


def _ui_page(title: str) -> str:
    """The minimal remote UI of §5: no framework, no CDN, one file, no build step."""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#111; --bg:#f7f7f5; --card:#fff; --muted:#666;
           --accent:#1668c1; --bad:#b3261e; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg:#eee; --bg:#111214; --card:#1b1d20; --muted:#9aa0a6; --accent:#7cb0ff;
             --bad:#ff8a80; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:1rem; font:16px/1.4 system-ui, sans-serif; color:var(--fg);
          background:var(--bg); }}
  main {{ max-width: 34rem; margin: 0 auto; display: grid; gap: 1rem; }}
  h1 {{ font-size:1.15rem; margin:0; }}
  .card {{ background:var(--card); border-radius:.75rem; padding:1rem;
           box-shadow:0 1px 3px rgba(0,0,0,.12); }}
  dl {{ display:grid; grid-template-columns:auto 1fr; gap:.25rem .75rem; margin:0; }}
  dt {{ color:var(--muted); }}
  dd {{ margin:0; font-variant-numeric: tabular-nums; overflow-wrap:anywhere; }}
  input, button {{ font:inherit; }}
  input[type=text], input[type=number] {{ width:100%; padding:.5rem;
          border:1px solid #8884; border-radius:.5rem; background:transparent; color:inherit; }}
  .row {{ display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.5rem; }}
  button {{ padding:.5rem .8rem; border:1px solid #8884; border-radius:.5rem;
            background:var(--card); color:inherit; cursor:pointer; }}
  button.primary {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
  button:disabled {{ opacity:.45; cursor:default; }}
  .err {{ color:var(--bad); min-height:1.2em; }}
  .hint {{ color:var(--muted); font-size:.85rem; }}
</style>
</head>
<body>
<main>
  <h1>{title} — v0.2</h1>

  <section class="card">
    <dl id="status">
      <dt>état</dt><dd id="s-state">…</dd>
      <dt>session</dt><dd id="s-session">—</dd>
      <dt>vidéo</dt><dd id="s-video">—</dd>
      <dt>position (groupe / vidéo)</dt><dd id="s-pos">—</dd>
      <dt>erreur numérique (p95)</dt><dd id="s-error">—</dd>
      <dt>quiet</dt><dd id="s-quiet">—</dd>
      <dt>groupe</dt><dd id="s-owner">—</dd>
      <dt>dernier événement</dt><dd id="s-last">—</dd>
    </dl>
    <div class="err" id="last-error"></div>
  </section>

  <section class="card">
    <label for="url">Vidéo à envoyer (URL YouTube)</label>
    <input id="url" type="text" placeholder="www.youtube.com/watch?v=…"
           autocomplete="off" spellcheck="false">
    <div class="row">
      <label class="hint"><input id="measure" type="checkbox"> bande de clics (mesure)</label>
    </div>
    <div class="row">
      <button class="primary" id="share">Envoyer</button>
      <button id="play">Lecture</button>
      <button id="pause">Pause</button>
      <button id="stop">Arrêt</button>
    </div>
  </section>

  <section class="card">
    <label for="seek">Position (secondes)</label>
    <div class="row">
      <input id="seek" type="number" min="0" step="1" value="0" style="flex:1 1 6rem">
      <button id="do-seek">Seek</button>
    </div>
    <label for="vol">Volume du groupe</label>
    <div class="row">
      <input id="vol" type="number" min="0.05" max="1" step="0.05" value="0.3"
             style="flex:1 1 6rem">
      <button id="do-vol">Appliquer</button>
      <button id="do-mute">Mute</button>
      <button id="do-unmute">Démute</button>
    </div>
    <p class="hint">Un niveau de 0 est refusé : pour faire silence, on mute (A37).</p>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let busy = false;

async function api(path, body) {{
  const options = body === undefined ? {{}} : {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(body),
  }};
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {{}};
  try {{ data = text ? JSON.parse(text) : {{}}; }} catch (e) {{ data = {{ error: text }}; }}
  if (!response.ok) throw new Error(data.error + (data.detail ? " — " + data.detail : ""));
  return data;
}}

function show(state) {{
  $("s-state").textContent = state.state;
  $("s-session").textContent = state.session_id || "—";
  $("s-video").textContent = state.video_id || "—";
  const pos = (v) => v === null || v === undefined ? "—" : v.toFixed(2) + " s";
  $("s-pos").textContent = pos(state.position_group) + " / " + pos(state.position_video);
  $("s-error").textContent = (state.digital_error_ms === null ? "—" : state.digital_error_ms + " ms")
    + " (" + (state.p95_ms === null ? "—" : state.p95_ms + " ms") + ")";
  const q = state.quiet || {{}};
  $("s-quiet").textContent = q.enabled
    ? q.start + "–" + q.end + " · " + q.decision : "désactivé";
  $("s-owner").textContent = state.group_owner || "libre";
  $("s-last").textContent = state.last_event || "—";
  $("last-error").textContent = state.last_error || "";
  const active = state.state === "playing" || state.state === "paused";
  $("play").disabled = !active;
  $("pause").disabled = !active;
  $("do-seek").disabled = !active;
  $("do-vol").disabled = !active;
}}

async function refresh() {{
  try {{ show(await api("/api/status")); }}
  catch (error) {{ $("last-error").textContent = error.message; }}
}}

async function act(action, extra) {{
  if (busy) return;
  busy = true;
  try {{
    const reply = await api("/api/control", Object.assign({{ action }}, extra || {{}}));
    show(await api("/api/status"));
    if (reply.state) $("s-state").textContent = reply.state;
  }} catch (error) {{ $("last-error").textContent = error.message; }}
  finally {{ busy = false; }}
}}

$("share").onclick = async () => {{
  const url = $("url").value.trim();
  if (!url) return;
  try {{
    await api("/api/share", {{ url, measure: $("measure").checked }});
    $("last-error").textContent = "";
    await refresh();
  }} catch (error) {{ $("last-error").textContent = error.message; }}
}};
$("play").onclick = () => act("play");
$("pause").onclick = () => act("pause");
$("stop").onclick = () => act("stop");
$("do-seek").onclick = () => act("seek", {{ position: Number($("seek").value) }});
$("do-vol").onclick = () => act("volume", {{ level: Number($("vol").value) }});
$("do-mute").onclick = () => act("volume", {{ level: Number($("vol").value), muted: true }});
$("do-unmute").onclick = () => act("volume", {{ level: Number($("vol").value), muted: false }});

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


def serve_in_thread(api: SenderAPI, *, bind: str = "127.0.0.1", port: int = 0):
    """Test/self-test helper: bind an ephemeral port and serve in a daemon thread."""
    server = api.make_server(bind, port)
    # a short poll interval: shutdown() waits for one poll, and a test suite shuts down a lot
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                              name="cast-sender-http", daemon=True)
    thread.start()
    return server, thread
