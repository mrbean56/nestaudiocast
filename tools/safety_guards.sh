#!/bin/sh
# Static safety regressions (offline): each check guards a bug already paid for
# (notes/00-labo.md A23, A36, A37; contracts §7). Exits 1 if any check fails, so it can
# gate a commit (pre-commit), CI, and tools/prove.sh §5.
cd "$(dirname "$0")/.."
fail=0
if grep -n "level=0" lib/silence.py lib/emergency.py >/dev/null 2>&1; then
  echo "   FAIL: a silence path zeroes the level (zeroing destroyed the level needed to"
  echo "         restore, and a group master multiplies members' levels -> silent kitchen)"
  fail=1
else
  echo "   ok: silencing mutes only, never zeroes levels"
fi
if grep -q "group_unmuted" relay/cast_relay.py; then
  echo "   ok: the relay undoes its own stop-mute at startup (per member)"
else
  echo "   FAIL: no startup unmute — a service restart would leave the kitchen muted"; fail=1
fi
if grep -q "def device_map" lib/groupguard.py; then
  echo "   ok: member uuid -> IP mapping present (needed to unmute members directly)"
else
  echo "   FAIL: no device_map"; fail=1
fi
if grep -q "WARNING group members are silent" relay/cast_relay.py; then
  echo "   ok: warns when every group member is silent"
else
  echo "   FAIL: no silent-members warning"; fail=1
fi
if grep -q "def read_group_owner" relay/cast_relay.py && \
   grep -q 'state", "group.owner"' relay/cast_relay.py; then
  echo "   ok: the relay reads state/group.owner and yields the group to a live v0.2 owner"
else
  echo "   FAIL: no group.owner lock read — the relay would fight a v0.2 session for the group"
  fail=1
fi
if grep -q "O_EXCL\|O_CREAT" relay/cast_relay.py; then
  echo "   FAIL: the relay creates a file exclusively — contracts §7 gives the lock to the owner"
  fail=1
else
  echo "   ok: the lock is read-only for the relay (never created, refreshed or deleted)"
fi
if [ -x tools/relay-stop-post.sh ] && grep -q "skip_mute_v020" tools/relay-stop-post.sh && \
   grep -q "lib/emergency.py" tools/relay-stop-post.sh; then
  echo "   ok: ExecStopPost wrapper skips emergency.py while v0.2 owns the group"
else
  echo "   FAIL: no ExecStopPost wrapper — stopping v0.1 would mute the kitchen under v0.2"
  fail=1
fi
if grep -q "^ExecStopPost=.*relay-stop-post.sh" systemd/cast-relay.service 2>/dev/null && \
   ! grep -q "^ExecStopPost=.*emergency.py" systemd/cast-relay.service; then
  echo "   ok: the relay unit stops through the wrapper, never emergency.py directly"
else
  echo "   FAIL: systemd/cast-relay.service calls emergency.py directly (or is missing)"
  fail=1
fi
if grep -q "^OWNER_FILE=/opt/castsender/state/group.owner" castrelay.conf.example; then
  echo "   ok: the relay reads the lock where cast-sender writes it"
else
  echo "   FAIL: castrelay.conf.example does not point OWNER_FILE at cast-sender's lock"
  fail=1
fi
[ "$fail" = "0" ] && echo "   -> all safety regressions clear" || echo "   -> REGRESSION(S) PRESENT"
[ "$fail" = "0" ]
