#!/bin/sh
# Start/stop the virtual Cast receiver without ever matching this script's own
# command line (a pkill -f on the script name kills the calling shell).
set -e
cd "$(dirname "$0")/.."
PIDF=logs/virtual-receiver.pid
case "${1:-start}" in
  start)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "already running pid=$(cat "$PIDF")"; exit 0
    fi
    setsid nohup .venv/bin/python probe/virtual_receiver.py --port "${2:-8009}" \
      --name "${3:-Relais Cast PoC}" >logs/virtual-receiver.out 2>&1 < /dev/null &
    echo $! > "$PIDF"
    sleep 1.5
    echo "started pid=$(cat "$PIDF")"
    ;;
  stop)
    if [ -f "$PIDF" ]; then
      kill "$(cat "$PIDF")" 2>/dev/null || true
      rm -f "$PIDF"
      echo stopped
    else
      echo "not running"
    fi
    ;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "running pid=$(cat "$PIDF")"
    else
      echo "not running"
    fi
    ;;
esac
