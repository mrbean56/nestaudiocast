#!/bin/sh
# Re-measure the perceived offset at the CURRENT deployed settings (30 ms tolerance), then
# put the service back. Run unattended; logs to logs/remeasure-30ms.out.
set -e
cd /home/mrbean/opencode/nestaudiocast
ssh root@192.168.2.21 'systemctl stop cast-relay' || true
.venv/bin/python - <<'PY'
import sys, time
sys.path.insert(0, "lib"); sys.path.insert(0, "tools")
from castv2 import CastLink
import groupguard
for ip, lvl in {"192.168.2.61":0.08332952857017517, "192.168.2.62":0.55, "192.168.2.63":0.55,
                "192.168.2.64":0.08332952857017517, "192.168.2.65":0.08332952857017517}.items():
    l = CastLink(ip); l.connect_platform(); l.set_receiver_volume(level=lvl, muted=False); l.close()
g = groupguard.discover_groups()["Cuisine"]
l = CastLink(*g); l.connect_platform(); l.set_receiver_volume(level=0.25, muted=False); l.close()
t = CastLink("192.168.2.80"); t.connect_platform(); t.set_receiver_volume(level=0.15); t.close()
print("measurement levels: kitchen 0.25, TV 0.15", flush=True)
PY
.venv/bin/python tools/acoustic_session.py 192.168.2.148:39509 --delta 0.265 --minutes 11 \
    --hold-to 11.2 --minutes-at 1.5,5,8.5 --tag v015 > logs/remeasure-30ms.out 2>&1 || true
ssh root@192.168.2.21 'systemctl start cast-relay' || true
echo done
