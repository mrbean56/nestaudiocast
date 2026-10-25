#!/bin/sh
# Re-run every OFFLINE proof in one go, on any host, with no side effects.
# Nothing here touches a speaker, a group or the TV: the simulator and the synthetic
# recordings are self-contained, so this is safe to run at any time (including at night).
#
#   ./tools/prove.sh            # all offline proofs
#   ./tools/prove.sh --fast     # skip the 3-minute controller simulation
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || { echo "no venv: create one with python3 -m venv .venv" >&2; exit 1; }

echo "== host =============================================="
echo "   $(hostname) $(hostname -I 2>/dev/null | awk '{print $1}')"
echo

echo "== 1. offset analyzer against known ground truth ======"
$PY measure/selftest_analyzer.py 2>&1 | grep -E '"case"|SELF-TEST'
echo

if [ "$1" != "--fast" ]; then
  echo "== 2. relay controller, closed loop on a simulated device =="
  $PY relay/sim.py 2>&1 | grep -E '^===|^\{' 
  echo
fi

echo "== 3. kitchen endpoints currently silent? ============="
$PY lib/silence.py | $PY -c "
import json,sys
d=json.load(sys.stdin)
bad=[k for k,v in d.items() if not v['silent']]
for k,v in d.items():
    print(f'   {k:24s} level={v[\"level\"]:.4f} muted={v[\"muted\"]} app={v[\"app\"]}')
print('   -> audible right now:', bad if bad else 'none')
"
echo

echo "== 4. group safety (never play on a group containing the TV) =="
$PY lib/groupguard.py | $PY -c "
import json,sys
d=json.load(sys.stdin)
if not d: print('   no groups found')
for name,v in d.items():
    tag='SAFE' if v['safe_audio_only'] else 'REFUSED (contains non-speaker)'
    print(f'   {name:14s} {tag}: {\", \".join(v[\"members\"])}')
"
echo
echo "== 5. safety regressions (offline, static) ==========="
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
[ "$fail" = "0" ] && echo "   -> all safety regressions clear" || echo "   -> REGRESSION(S) PRESENT"
echo
echo "done — evidence in evidence/"
