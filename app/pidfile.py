"""PID file supervisor with stale-process detection and cleanup.

Implements FRD B.10:
- startup: detect stale PID (dead process or command mismatch), clean it,
  emit a deferred warning; refuse to start if a live instance of the same
  name is already running
- shutdown: SIGTERM/SIGINT/atexit all call the same shutdown routine which
  deletes the PID file
- file lock prevents a race with a simultaneously-starting second instance
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import psutil

from app.paths import pid_file


@dataclass
class StaleInfo:
    cleaned: bool
    previous_pid: Optional[int]
    reason: str


class AlreadyRunning(RuntimeError):
    def __init__(self, name: str, pid: int):
        super().__init__(f"{name} already running as pid {pid}")
        self.name = name
        self.pid = pid


def _read_pid_file(path: Path) -> Optional[dict]:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"pid": None, "cmd": None, "start_time_epoch": None, "_corrupt": True}


def _process_matches(pid: int, expected_cmd: str) -> bool:
    """True if pid is alive and its recorded command matches expected.
    The cmd check stops us from treating a recycled PID as our own process.
    """
    if pid == os.getpid():
        return True
    try:
        p = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    try:
        cmdline = " ".join(p.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return expected_cmd in cmdline


def check_stale(name: str) -> StaleInfo:
    """Inspect an existing PID file and decide whether it's stale.

    Does not modify anything. Pure check; the caller decides to clean.
    """
    path = pid_file(name)
    data = _read_pid_file(path)
    if data is None:
        return StaleInfo(cleaned=False, previous_pid=None, reason="no pid file")
    if data.get("_corrupt"):
        return StaleInfo(cleaned=False, previous_pid=None, reason="corrupt pid file")
    pid = data.get("pid")
    cmd = data.get("cmd") or name
    if pid is None:
        return StaleInfo(cleaned=False, previous_pid=None, reason="missing pid field")
    if _process_matches(pid, cmd):
        return StaleInfo(cleaned=False, previous_pid=pid, reason="live process")
    return StaleInfo(cleaned=False, previous_pid=pid, reason="dead or wrong cmd")


def _clean_stale(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class PidFile:
    """Context-managed PID file with stale cleanup and signal handlers.

    Usage:
        with PidFile("worker") as pf:
            pf.register_shutdown(my_shutdown_callable)
            run_forever()

    If an instance is already running under the same name, AlreadyRunning is
    raised. If a stale file is found, it is cleaned and pf.stale_info reflects
    what was removed so the caller can emit a user-visible alert.
    """

    def __init__(self, name: str):
        self.name = name
        self.path = pid_file(name)
        self._lock_fd: Optional[int] = None
        self._shutdown_cbs: list[Callable[[], None]] = []
        self._shutting_down = False
        self.stale_info: Optional[StaleInfo] = None

    def __enter__(self) -> "PidFile":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> None:
        info = check_stale(self.name)
        if info.reason == "live process":
            raise AlreadyRunning(self.name, info.previous_pid or -1)
        if info.previous_pid is not None or info.reason == "corrupt pid file":
            _clean_stale(self.path)
            self.stale_info = StaleInfo(cleaned=True, previous_pid=info.previous_pid, reason=info.reason)

        # Open with O_CREAT | O_EXCL via exclusive file lock to race-proof.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._try_exclusive_lock(fd)
        except BlockingIOError as e:
            os.close(fd)
            raise AlreadyRunning(self.name, -1) from e
        self._lock_fd = fd

        record = {
            "pid": os.getpid(),
            "start_time_epoch": int(time.time()),
            "cmd": self.name,
        }
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(record).encode("utf-8"))
        os.fsync(fd)

        self._install_signal_handlers()
        atexit.register(self._atexit_shutdown)

    def register_shutdown(self, cb: Callable[[], None]) -> None:
        self._shutdown_cbs.append(cb)

    def release(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        for cb in reversed(self._shutdown_cbs):
            try:
                cb()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
        if self._lock_fd is not None:
            try:
                self._release_lock(self._lock_fd)
            except OSError:
                pass
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _atexit_shutdown(self) -> None:
        self.release()

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):  # noqa: ARG001
            self.release()
            sys.exit(128 + int(signum))

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    @staticmethod
    def _try_exclusive_lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release_lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
