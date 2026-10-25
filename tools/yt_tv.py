#!/usr/bin/env python3
"""Drive the YouTube app on the living-room TV like the phone does (test harness).

  yt_tv.py play VID [VID ...]   launch YouTube on the TV, play VID, queue the rest
  yt_tv.py queue VID            add VID to the TV's YouTube queue
  yt_tv.py next                 skip to the next queued video
  yt_tv.py pause | resume | stop
  yt_tv.py seek SECONDS
  yt_tv.py volume LEVEL         TV receiver volume 0..1
  yt_tv.py status

Refuses to touch the TV when it runs something other than Backdrop or YouTube,
so it never takes over a session someone else started (unless --force).
"""
import json
import os
import sys
import time

import pychromecast
from pychromecast.controllers.youtube import YouTubeController

TV_HOST = "192.168.2.80"
YOUTUBE = "233637DE"
FREE_APPS = {None, "E8C28D3C", YOUTUBE}  # nothing, Backdrop, YouTube


def connect():
    cc = pychromecast.get_chromecast_from_host((TV_HOST, 8009, None, None, None))
    cc.wait(timeout=15)
    return cc


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    if not args:
        print(__doc__)
        return 2
    cmd, rest = args[0], args[1:]
    cc = connect()
    try:
        app = cc.status.app_id if cc.status else None
        if cmd != "status" and app not in FREE_APPS and not force:
            print(json.dumps({"refused": True, "app": cc.status.display_name, "app_id": app}))
            return 3
        mc = cc.media_controller
        if cmd == "status":
            # Read-only: pychromecast's media controller would LAUNCH its own app here.
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "lib"))
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "relay"))
            from cast_relay import RemoteTv
            snap = RemoteTv(TV_HOST).snapshot()
            print(json.dumps({k: snap.get(k) for k in
                              ("app", "app_id", "state", "t", "content", "volume")}))
        elif cmd in ("play", "queue", "next"):
            yt = YouTubeController()
            cc.register_handler(yt)
            if cmd == "play":
                yt.play_video(rest[0])
                for vid in rest[1:]:
                    time.sleep(2)
                    yt.add_to_queue(vid)
            elif cmd == "queue":
                yt.update_screen_id()
                time.sleep(2)
                yt.add_to_queue(rest[0])
            else:
                yt.update_screen_id()
                time.sleep(2)
                yt.play_next(rest[0]) if rest else mc.queue_next()
            print(json.dumps({"ok": cmd, "videos": rest}))
        elif cmd in ("pause", "resume", "stop"):
            mc.update_status()
            time.sleep(1)
            {"pause": mc.pause, "resume": mc.play, "stop": cc.quit_app}[cmd]()
            print(json.dumps({"ok": cmd}))
        elif cmd == "seek":
            mc.update_status()
            time.sleep(1)
            mc.seek(float(rest[0]))
            print(json.dumps({"ok": "seek", "to": float(rest[0])}))
        elif cmd == "volume":
            cc.set_volume(float(rest[0]))
            print(json.dumps({"ok": "volume", "level": float(rest[0])}))
        else:
            print(__doc__)
            return 2
        time.sleep(1)
        return 0
    finally:
        cc.disconnect(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
