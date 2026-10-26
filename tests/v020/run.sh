#!/bin/sh
# The v0.2 offline suite. Nothing here touches a speaker, a group, the TV or the Chromecast.
#   ./tests/v020/run.sh          # everything
#   ./tests/v020/run.sh test_media_resolver   # one module
set -e
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
if [ -n "$1" ]; then
  exec $PY -m unittest -v "discover" -s tests/v020 -p "$1.py"
fi
exec $PY -m unittest discover -s tests/v020 -v
