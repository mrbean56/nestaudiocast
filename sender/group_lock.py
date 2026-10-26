"""One owner at a time for the Cuisine group — contracts §7, and the rollback hooks of §8.

``state/group.owner`` is a small JSON file ``{"owner", "pid", "since"}``:

* created atomically with ``O_EXCL`` — the v0.1 relay uses the same mechanism for the same file;
* refreshed as a heartbeat every 5 s (the file is rewritten, its mtime is the heartbeat);
* stale after 15 s: a takeover is then allowed **and logged**;
* released on exit, but only when this process still owns it — a new owner's file is never
  deleted by a dying process.

The v0.2 owner always wins (§7): a lock held by ``cast-relay`` is preempted at the moment of a
share, because a share is an explicit user action and the relay's lock never is. A lock held by
*another cast-sender* is not preempted (that would be two of us on one group).

Staleness is read from the file's mtime on purpose: the documented record has three keys, and
adding a fourth to carry a timestamp would be a contract change.
"""

from __future__ import annotations

import json
import os
import threading
import time

from v020.contracts import (
    MSG_IDLE, MSG_STOP, OWNER_HEARTBEAT_S, OWNER_RELAY, OWNER_SENDER, OWNER_STALE_S,
)

RECORD_KEYS = ("owner", "pid", "since")


# ---------------------------------------------------------------------------------------------
# state/group.owner
# ---------------------------------------------------------------------------------------------
class GroupLock:
    """The writer/reader of ``state/group.owner`` (contracts §7)."""

    def __init__(self, path: str, *, owner: str = OWNER_SENDER, pid: int | None = None,
                 clock=time.time, log=None, stale_s: float = OWNER_STALE_S,
                 heartbeat_s: float = OWNER_HEARTBEAT_S,
                 preempt: tuple[str, ...] = (OWNER_RELAY,)) -> None:
        self.path = path
        self.owner = owner
        self.pid = os.getpid() if pid is None else int(pid)
        self.clock = clock
        self.log = log or (lambda *a, **k: None)
        self.stale_s = float(stale_s)
        self.heartbeat_s = float(heartbeat_s)
        self.preempt = tuple(preempt)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    # -- reading ----------------------------------------------------------------------------
    def read(self) -> dict | None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def age(self) -> float | None:
        """Seconds since the heartbeat (mtime), or None when there is no file."""
        try:
            return self.clock() - os.stat(self.path).st_mtime
        except OSError:
            return None

    def owner_name(self) -> str | None:
        data = self.read()
        return None if data is None else str(data.get("owner") or "")

    def is_mine(self, data: dict | None = None) -> bool:
        data = self.read() if data is None else data
        if not data:
            return False
        try:
            return str(data.get("owner")) == self.owner and int(data.get("pid")) == self.pid
        except (TypeError, ValueError):
            return False

    # -- writing ----------------------------------------------------------------------------
    def _payload(self) -> dict:
        return {"owner": self.owner, "pid": self.pid, "since": round(self.clock(), 3)}

    def _write_new(self, payload: dict) -> bool:
        """``O_EXCL`` create: the only race-free way to take a free lock."""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.write("\n")
        except Exception:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            raise
        return True

    def _replace(self, payload: dict, *, previous: dict | None = None, reason: str = "") -> None:
        """Atomic rewrite (heartbeat or takeover): readers never see half a file."""
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
        if previous is not None:
            observation = {
                "previous_owner": previous.get("owner"),
                "previous_pid": previous.get("pid"),
                "age_s": None if self.age() is None else round(self.age(), 3),
                "reason": reason or "unknown",
            }
            self.log("lock_takeover", **observation)
        os.replace(tmp, self.path)

    # -- ownership --------------------------------------------------------------------------
    def acquire(self) -> bool:
        """Take the lock if it is free, stale, or held by the relay (§7). False = contended."""
        with self._lock:
            payload = self._payload()
            if self._write_new(payload):
                self.log("lock_acquired", owner=self.owner, pid=self.pid, path=self.path)
                return True
            current = self.read()
            if current is None:
                # Raced with a release; one more attempt is enough (no loop, no sleep).
                if self._write_new(payload):
                    self.log("lock_acquired", owner=self.owner, pid=self.pid, path=self.path,
                             raced=True)
                    return True
                current = self.read() or {}
            if self.is_mine(current):
                self._replace(payload)
                return True
            age = self.age()
            previous_owner = str(current.get("owner") or "")
            if previous_owner in self.preempt:
                self._replace(payload, previous=current, reason="owner_wins")
                return True
            if age is not None and age > self.stale_s:
                self._replace(payload, previous=current, reason="stale")
                return True
            self.log("lock_contended", holder=previous_owner, pid=current.get("pid"),
                     age_s=None if age is None else round(age, 3))
            return False

    def heartbeat(self) -> bool:
        """Refresh the heartbeat; False when the lock is no longer ours (someone took it)."""
        with self._lock:
            current = self.read()
            if not self.is_mine(current):
                self.log("lock_lost", holder=None if current is None else current.get("owner"),
                         pid=None if current is None else current.get("pid"))
                return False
            self._replace(self._payload())
            return True

    def release(self) -> bool:
        """Drop the lock, but never a file another owner wrote."""
        with self._lock:
            current = self.read()
            if not self.is_mine(current):
                self.log("lock_release_skipped",
                         holder=None if current is None else current.get("owner"))
                return False
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                return False
            self.log("lock_released", owner=self.owner, pid=self.pid)
            return True

    # -- background heartbeat ---------------------------------------------------------------
    def start_heartbeat(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(self.heartbeat_s):
                try:
                    if not self.heartbeat():
                        return
                except Exception as exc:  # noqa: BLE001 - a heartbeat must never kill the app
                    self.log("lock_heartbeat_error", error=f"{type(exc).__name__}: {exc}")

        self._thread = threading.Thread(target=loop, name="cast-sender-lock", daemon=True)
        self._thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)


# ---------------------------------------------------------------------------------------------
# state/session.json — what a boot must clean up (contracts §8)
# ---------------------------------------------------------------------------------------------
def session_record_path(state_dir: str) -> str:
    return os.path.join(state_dir, "session.json")


def read_session_record(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def write_session_record(path: str, record: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def clear_session_record(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def boot_cleanup(path: str, *, group=None, link=None, proxy=None, log=None,
                 owner: str = OWNER_SENDER) -> dict:
    """Stop only the sessions **we** recorded, undo only the mutes **we** set (contracts §8).

    Called on startup and from ``ExecStopPost``. A missing record means "we created nothing",
    and then nothing is touched — not the group, not the Chromecast, not a volume.
    """
    log = log or (lambda *a, **k: None)
    record = read_session_record(path)
    actions: list[str] = []
    if not isinstance(record, dict):
        log("boot_cleanup_nothing_recorded", path=path)
        return {"session": None, "actions": actions, "record": None}
    if str(record.get("owner") or "") != owner:
        log("boot_cleanup_foreign_record", path=path, record_owner=record.get("owner"))
        return {"session": record.get("session_id"), "actions": actions, "record": record}

    session_id = record.get("session_id")
    log("boot_cleanup_start", session_id=session_id, sid=record.get("sid"),
        group_prepared=bool(record.get("group_prepared")))

    # 1. the group session, only if this record says we prepared it
    if group is not None and record.get("group_prepared"):
        try:
            group.stop()
            actions.append("group_stopped")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"group_stop_failed:{type(exc).__name__}")
            log("boot_cleanup_error", step="group_stop", error=str(exc))

    # 2. our receiver app on the Chromecast (stop, not idle: we are leaving)
    if link is not None and record.get("receiver_launched"):
        try:
            link.send(MSG_STOP)
            actions.append("receiver_stopped")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"receiver_stop_failed:{type(exc).__name__}")
            log("boot_cleanup_error", step="receiver_stop", error=str(exc))

    # 3. the media session id we opened
    if proxy is not None and record.get("sid"):
        try:
            proxy.close_session(str(record["sid"]))
            actions.append("proxy_closed")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"proxy_close_failed:{type(exc).__name__}")
            log("boot_cleanup_error", step="proxy_close", error=str(exc))

    # 4. only the mutes we set, restored to the state they had before we touched them
    for mute in record.get("mutes") or []:
        if not isinstance(mute, dict) or str(mute.get("by") or owner) != owner:
            continue
        if group is None:
            continue
        try:
            group.set_volume(level=mute.get("level"), muted=mute.get("muted"))
            actions.append(f"mute_undone:{mute.get('name', 'group')}")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"mute_undo_failed:{type(exc).__name__}")
            log("boot_cleanup_error", step="mute_undo", error=str(exc))

    clear_session_record(path)
    log("boot_cleanup_done", session_id=session_id, actions=actions)
    return {"session": session_id, "actions": actions, "record": record}


def stop_receiver_idle(link, log=None) -> bool:
    """Leave the Chromecast on its idle page without unloading someone else's media (§8)."""
    log = log or (lambda *a, **k: None)
    try:
        link.send(MSG_IDLE)
        return True
    except Exception as exc:  # noqa: BLE001
        log("receiver_idle_failed", error=f"{type(exc).__name__}: {exc}")
        return False
