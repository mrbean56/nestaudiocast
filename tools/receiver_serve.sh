#!/bin/sh
# Serve the receiver page on the one LAN address the Cast console registered, and nothing else.
#
#   ./tools/receiver_serve.sh            # deploy receiver/ to CT 121 and (re)start the server
#   ./tools/receiver_serve.sh start
#   ./tools/receiver_serve.sh stop
#   ./tools/receiver_serve.sh restart
#   ./tools/receiver_serve.sh status
#   ./tools/receiver_serve.sh deploy     # copy the files, leave the server alone
#
# Deploys receiver/ to 192.168.2.21:/opt/nac-dev/receiver and runs receiver/serve.py
# (stdlib http.server) on 192.168.2.21:7640. Idempotent: a second start redeploys the files and
# reports "already running" instead of opening a second server.
#
# What it deliberately never does: send a packet to :8099, run systemctl, touch
# cast-relay.service, or bind anything but 192.168.2.21:7640. The v0.1 relay keeps running
# exactly as it is; the only thing this script asks it is whether it is still active.
set -eu

HOST=${NAC_HOST:-192.168.2.21}
PORT=${NAC_PORT:-7640}
DEST=/opt/nac-dev
PIDFILE=/run/nac-receiver-dev.pid
LOGFILE=/var/log/nac-receiver-dev.log
HERE=$(cd "$(dirname "$0")/.." && pwd)

ssh_() { ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$HOST" "$@"; }

stamp() {
  # /build.txt is what makes a page update cost a relaunch instead of a reboot: the loader reads
  # it no-store and fetches /receiver.js?v=<stamp>
  sum=$( (sha1sum "$HERE/receiver/receiver.js" 2>/dev/null || openssl dgst -sha1 -r "$HERE/receiver/receiver.js") | cut -c1-12)
  ssh_ "printf '%s\n' '$sum' > $DEST/receiver/build.txt" && echo "   build=$sum"
}

deploy() {
  echo "== deploy receiver/ -> $HOST:$DEST/receiver/"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$HERE/receiver/" "root@$HOST:$DEST/receiver/"
    return 0
  fi
  # No rsync on the authoring host: tar over ssh, then prune what the source no longer has.
  # Same end state: the directory on CT 121 is exactly receiver/.
  ( cd "$HERE/receiver" && tar -cf - . ) \
    | ssh_ "mkdir -p $DEST/receiver && tar -C $DEST/receiver -xf -"
  ( cd "$HERE/receiver" && find . -type f -printf '%P\n' ) \
    | ssh_ "cd $DEST/receiver && while IFS= read -r f; do [ -e \"\$f\" ] || { echo \"   prune \$f\"; rm -f \"\$f\"; }; done"
  stamp
}

probe_remote() {
  # one connection, all the facts: pid, aliveness, listener, health, page, relay state
  ssh_ "
    p=\$(cat $PIDFILE 2>/dev/null || true)
    echo \"pid=\${p:-}\"
    if [ -n \"\$p\" ] && kill -0 \$p 2>/dev/null; then echo alive=yes; else echo alive=no; fi
    if ss -ltn 2>/dev/null | awk '{print \$4}' | grep -qx '$HOST:$PORT'; then echo listening=yes; else echo listening=no; fi
    echo \"health=\$(python3 - <<'PY' 2>/dev/null || true
import urllib.request
try:
    print(urllib.request.urlopen('http://$HOST:$PORT/healthz', timeout=3).read().decode())
except Exception as e:
    print('ERR', e)
PY
)\"
    echo \"page=\$(python3 - <<'PY' 2>/dev/null || true
import urllib.request
try:
    b = urllib.request.urlopen('http://$HOST:$PORT/receiver.html', timeout=5).read()
    print('200:%d' % len(b))
except Exception as e:
    print('ERR', e)
PY
)\"
    echo \"relay=\$(systemctl is-active cast-relay 2>/dev/null || true)\"
    echo \"log=\$(tail -2 $LOGFILE 2>/dev/null | tr '\n' '|')\"
  "
}

start() {
  deploy
  info=$(probe_remote)
  case "$info" in
    *alive=yes*)
      echo "$info" | grep -q 'listening=yes' && {
        echo "== already running on $HOST:$PORT — files redeployed, server untouched"
        echo "$info"; return 0; }
      ;;
  esac
  if echo "$info" | grep -q 'listening=yes'; then
    echo "REFUSING to start: something already listens on $HOST:$PORT" >&2
    ssh_ "ss -ltnp 2>/dev/null | grep ':$PORT '" >&2 || true
    exit 3
  fi
  echo "== start on $HOST:$PORT"
  ssh_ "cd $DEST/receiver; setsid python3 serve.py --bind $HOST --port $PORT >>$LOGFILE 2>&1 </dev/null & echo \$! > $PIDFILE"
  sleep 1.5
  info=$(probe_remote)
  case "$info" in
    *listening=yes*) echo "$info" ;;
    *) echo "FAILED: nothing listening on $HOST:$PORT; last log lines:" >&2
       ssh_ "tail -20 $LOGFILE" >&2 || true
       exit 4 ;;
  esac
}

stop() {
  info=$(probe_remote)
  p=$(echo "$info" | sed -n 's/^pid=//p')
  if [ -n "$p" ] && echo "$info" | grep -q 'alive=yes'; then
    echo "== stop pid $p"
    ssh_ "kill $p 2>/dev/null || true; i=0; while kill -0 $p 2>/dev/null && [ \$i -lt 25 ]; do sleep 0.2; i=\$((i+1)); done; kill -9 $p 2>/dev/null || true; rm -f $PIDFILE"
  else
    echo "== not running (pidfile: ${p:-none})"
    ssh_ "rm -f $PIDFILE"
  fi
  echo "== after stop:"
  probe_remote | grep -E 'listening=|relay=' || true
}

status() { probe_remote; }

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)  status ;;
  deploy)  deploy ;;
  *) echo "usage: $0 [start|stop|restart|status|deploy]" >&2; exit 2 ;;
esac
