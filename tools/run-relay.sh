#!/bin/sh
# Service wrapper: refuses to start without a safe, configured group.
set -e
CONF=${CONF:-/etc/castrelay.conf}
[ -r "$CONF" ] || { echo "missing $CONF" >&2; exit 1; }
. "$CONF"
if [ -z "$GROUP" ]; then
  echo "GROUP is empty in $CONF: create the kitchen-only group first" >&2
  exit 78
fi
cd /opt/castrelay
ARGS="--tv ${TV:-192.168.2.80} --group $GROUP --delta ${DELTA:-0.0} --poll ${POLL:-0.25}"
# Group ownership lock (docs/v020-contracts.md §7): read-only, the relay yields while v0.2 owns.
ARGS="$ARGS --owner-file ${OWNER_FILE:-state/group.owner}"
[ "${MIRROR_VOLUME:-1}" = "1" ] && ARGS="$ARGS --mirror-volume --volume-gain ${VOLUME_GAIN:-1.0} --volume-max ${VOLUME_MAX:-1.0}"
exec .venv/bin/python relay/cast_relay.py $ARGS \
  --log "/opt/castrelay/evidence/relay-$(date +%Y%m%d-%H%M%S).jsonl"
