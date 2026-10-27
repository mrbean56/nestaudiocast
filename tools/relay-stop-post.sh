#!/bin/sh
# cast-relay.service ExecStopPost — silence the kitchen, UNLESS v0.2 owns the group.
#
# v0.1's stop path is lib/emergency.py: it mutes the 5 Nest Audio and stops any app running on
# a safe (audio-only) group. Under a live v0.2 session both of those act on *v0.2's* audio — a
# mute is silent, but it silences the kitchen a v0.2 session is playing into, and its session
# cleanup would stop v0.2's own DMR app. That is the incident this wrapper removes.
#
# lib/emergency.py is NOT modified (it is frozen and shared with the panic path). The gate is
# here, in the unit, where "should the stop hook mute?" is decided:
#
#   ExecStopPost=-/opt/castrelay/tools/relay-stop-post.sh
#
# Decision, one line on stdout (goes to journalctl -u cast-relay):
#   v0.1 stop path: owner=... reason=... age_s=...   -> lib/emergency.py runs (unchanged)
#   skip_mute_v020 owner=... pid=... age_s=...       -> nothing runs, no group packet
#
# The lock is read with the relay's own parser (relay/cast_relay.py:read_group_owner,
# docs/v020-contracts.md §7) so the project has one interpretation of that file, not two.
set -u

HERE=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${CASTRELAY_ROOT:-$HERE}
# The lock lives where cast-sender writes it; /etc/castrelay.conf names it once (OWNER_FILE),
# for run-relay.sh and for this hook alike. Two different defaults is how the relay and the
# sender ended up looking at two different files on CT 121 (review 2026-10-27).
CONF=${CONF:-/etc/castrelay.conf}
# shellcheck disable=SC1090
[ -r "$CONF" ] && . "$CONF"
OWNER_FILE=${GROUP_OWNER_FILE:-${OWNER_FILE:-$ROOT/state/group.owner}}
case "$OWNER_FILE" in /*) ;; *) OWNER_FILE=$ROOT/$OWNER_FILE ;; esac
PY=$ROOT/.venv/bin/python
[ -x "$PY" ] || PY=python3

DECISION=$(
  "$PY" - "$ROOT/relay" "$OWNER_FILE" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from cast_relay import read_group_owner

o = read_group_owner(sys.argv[2])
if o.v020:
    print(f"skip_mute_v020 owner={o.owner} pid={o.pid} age_s={o.age_s}")
else:
    print(f"v0.1 stop path: owner={o.owner} reason={o.reason} age_s={o.age_s}")
PY
) || DECISION=""

case "$DECISION" in
  skip_mute_v020*)
    echo "$DECISION"
    echo "   lib/emergency.py NOT run: a mute or a session stop would act on v0.2's audio"
    exit 0
    ;;
esac

# No live v0.2 owner — including a lock that is absent, stale or malformed, i.e. exactly the
# situations where contracts §7 lets a takeover happen. Then v0.1 behaves as it always did.
echo "${DECISION:-v0.1 stop path: lock unreadable (v0.1 behaviour)}"
exec "$PY" "$ROOT/lib/emergency.py"
