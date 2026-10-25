#!/bin/sh
# Record the room through the phone's microphone over wireless ADB (scrcpy, no app needed).
#   tools/phone_record.sh IP:PORT SECONDS OUT.wav
# The phone must sit where it hears both the TV and the kitchen speakers.
# Uses the system adb (a second adb server from the scrcpy bundle would drop the link).
set -e
DEV=$1; SECS=${2:-60}; OUT=${3:-evidence/audio/rec-$(date +%Y%m%d-%H%M%S).wav}
S=${SCRCPY_DIR:-/tmp/scrcpy_dl/scrcpy-linux-x86_64-v5.0.1}
[ -x "$S/scrcpy" ] || { echo "scrcpy missing in $S (GitHub release Genymobile/scrcpy linux-x86_64)" >&2; exit 1; }
adb connect "$DEV" >/dev/null
mkdir -p "$(dirname "$OUT")"
ADB=$(command -v adb) SCRCPY_SERVER_PATH=$S/scrcpy-server \
  "$S/scrcpy" -s "$DEV" --no-video --no-playback --no-window --audio-source=mic \
  --audio-codec=raw --record="$OUT" --time-limit="$SECS" >/dev/null 2>&1
echo "$OUT"
