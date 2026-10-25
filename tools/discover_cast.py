#!/usr/bin/env python3
"""Discover Google Cast devices on the LAN via mDNS (_googlecast._tcp)."""
import sys
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

FOUND = {}


class L(ServiceListener):
    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name, timeout=3000)
        if not info:
            return
        props = {
            k.decode(errors="replace"): (v.decode(errors="replace") if v else "")
            for k, v in (info.properties or {}).items()
        }
        key = props.get("id") or name
        FOUND[key] = {
            "name": name,
            "friendly": props.get("fn", "?"),
            "model": props.get("md", "?"),
            "id": props.get("id", "?"),
            "addresses": [a for a in info.parsed_scoped_addresses()],
            "port": info.port,
            "status": props.get("rs", ""),
            "group": props.get("grp", ""),
            "caps": props.get("ca", ""),
            "proto": props.get("ve", ""),
        }

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)

    def remove_service(self, zc, type_, name):
        pass


def main(duration=8):
    zc = Zeroconf()
    ServiceBrowser(zc, "_googlecast._tcp.local.", L())
    time.sleep(duration)
    zc.close()
    rows = sorted(FOUND.values(), key=lambda r: r["friendly"].lower())
    print(f"{len(rows)} Cast device(s) seen\n")
    for r in rows:
        print(f"- {r['friendly']}  [{r['model']}]")
        print(f"    addr   : {', '.join(r['addresses'])}:{r['port']}")
        print(f"    id     : {r['id']}")
        print(f"    group  : {r['group'] or '(standalone)'}   caps={r['caps']}  status={r['status']}")
        print(f"    proto  : {r['proto']}")
        print(f"    service: {r['name']}")
    return rows


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
