#!/bin/sh
# The v0.2 offline suite. Nothing here touches a speaker, a group, the TV or the Chromecast:
# tests/v020/conftest.py refuses any socket outside loopback for offline tests.
#   ./tests/v020/run.sh                        # everything
#   ./tests/v020/run.sh test_media_resolver    # one module
#   ./tests/v020/run.sh -k landing             # any pytest arguments
set -e
cd "$(dirname "$0")/../.."
PYTEST=.venv/bin/pytest
[ -x "$PYTEST" ] || { echo "no dev environment: run 'uv sync' (docs/dev.md)" >&2; exit 1; }
case "$1" in
  test_*) mod=$1; shift; exec $PYTEST "tests/v020/$mod.py" "$@" ;;
  *) exec $PYTEST "$@" ;;
esac
