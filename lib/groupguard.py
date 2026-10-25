"""Group safety guard.

Hard-won fact (evidence/09-group-membership-multizone.json): on this LAN *every*
existing Google Cast group contains the living-room Chromecast. Starting a session
on such a group takes the TV over — which is exactly how the TV got interrupted
during early testing. So: no code in this project may start a session on a group
unless every member is an audio-only speaker (capabilities == 196612).

`MULTIZONE_STATUS` returns the member list read-only, with no playback, so this
check costs nothing and disturbs nobody.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from castv2 import CastLink  # noqa: E402

NS_MZ = "urn:x-cast:com.google.cast.multizone"
SPEAKER_CAPS = 196612
TV_ID = "dea0f72e-3ce7-e3f8-bd2c-dde1fb77d259"  # Chromecast Salon


class UnsafeGroup(RuntimeError):
    pass


def discover_groups(timeout: float = 6.0) -> dict[str, tuple[str, int]]:
    """All Google Cast groups on the LAN: name -> (host, port)."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: dict[str, tuple[str, int]] = {}

    class L(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=3000)
            if not info:
                return
            props = {(k.decode() if isinstance(k, bytes) else k):
                     (v.decode() if isinstance(v, bytes) else v)
                     for k, v in (info.properties or {}).items()}
            if props.get("md") != "Google Cast Group":
                return
            addrs = info.parsed_scoped_addresses()
            if addrs:
                found[props.get("fn", name)] = (addrs[0], info.port)

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    ServiceBrowser(zc, "_googlecast._tcp.local.", L())
    time.sleep(timeout)
    zc.close()
    return found


def device_map(timeout: float = 5.0) -> dict[str, str]:
    """uuid -> IP for every Cast device on the LAN (mDNS), to reach group members directly."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: dict[str, str] = {}

    class L(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=3000)
            if not info:
                return
            props = {(k.decode() if isinstance(k, bytes) else k):
                     (v.decode() if isinstance(v, bytes) else v)
                     for k, v in (info.properties or {}).items()}
            addrs = [a for a in info.parsed_scoped_addresses() if ":" not in a]
            if props.get("id") and addrs:
                found[str(props["id"]).replace("-", "").lower()] = addrs[0]

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    ServiceBrowser(zc, "_googlecast._tcp.local.", L())
    time.sleep(timeout)
    zc.close()
    return found


def members(host: str, port: int) -> list[dict]:
    l = CastLink(host, port)
    try:
        l.connect_platform()
        st = l.request("receiver-0", NS_MZ, {"type": "GET_STATUS"}, timeout=6)["payload"]["status"]
        return [{"name": d["name"].strip(), "id": d["deviceId"],
                 "level": (d.get("volume") or {}).get("level"),
                 "muted": (d.get("volume") or {}).get("muted"),
                 "caps": d["capabilities"],
                 "audio_only": d["capabilities"] == SPEAKER_CAPS}
                for d in st.get("devices", [])]
    finally:
        l.close()


def is_safe(host: str, port: int) -> tuple[bool, list[dict]]:
    ms = members(host, port)
    return bool(ms) and all(m["audio_only"] for m in ms), ms


def require_safe_group(host: str, port: int, label: str = "") -> list[dict]:
    ok, ms = is_safe(host, port)
    if not ok:
        bad = ", ".join(f"{m['name']}({'TV' if m['id'] == TV_ID else 'video'})"
                        for m in ms if not m["audio_only"])
        raise UnsafeGroup(
            f"refusing to play on group {label or f'{host}:{port}'}: it contains "
            f"non-speaker members [{bad}]. A session there takes over the TV. "
            f"Create a kitchen-only group (Nest Audio only) first.")
    return ms


def report() -> dict:
    out = {}
    for name, (h, p) in discover_groups().items():
        ok, ms = is_safe(h, p)
        out[name] = {"endpoint": f"{h}:{p}", "safe_audio_only": ok,
                     "members": [f"{m['name']}{'' if m['audio_only'] else ' [VIDEO]'}" for m in ms]}
    return out


if __name__ == "__main__":
    import json

    print(json.dumps(report(), indent=2, ensure_ascii=False))
