"""PID file supervisor with stale-process detection and cleanup.

Implements FRD B.10:
- startup: detect stale PID (dead process or command mismatch), clean it,
  emit a deferred warning; refuse to start if a live instance of the same
  name is already running
- shutdown: SIGTERM/SIGINT/atexit all call the same shutdown routine which
  deletes the PID file
- file lock prevents a race with a simultaneously-starting second instance

Two-file layout (Windows-safe):
- `run/<name>.lock` — sentinel that the running process holds an OS exclusive
  lock on. No metadata. Other processes never read this file.
- `run/<name>.pid`  — JSON metadata (pid, cmd, start_time_epoch). Plain
  readable file written atomically via tempfile+rename, so other processes
  (e.g. the web UI checking "is the worker alive?") never hit a half-written
  state and never collide with the lock holder.

This split is required because msvcrt.locking() on Windows is mandatory:
locking the pid file directly causes PermissionError when other processes
try to read it. fcntl.flock on Linux is advisory and would not exhibit this,
so the bug only surfaced on Windows.

Windows hang protection. `msvcrt.locking(fd, LK_NBLCK, 1)` is documented as
non-blocking but in practice retries ~10 times with 1-second sleeps before
raising, and `os.open()` on a `.lock` file can itself block if a dead
predecessor still has a kernel handle without FILE_SHARE_*. To bound the
worst case, the open+lock step runs in a worker thread with a hard
`LOCK_ACQUIRE_TIMEOUT_S` ceiling. Exceeding the ceiling is reported as
AlreadyRunning so the caller gets a clean, logged error instead of a silent
hang.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import psutil

from app.paths import lock_file, pid_file

log = logging.getLogger("pidfile")

# Hard ceiling on how long we'll wait for the exclusive byte-range lock on the
# .lock sentinel. Must exceed msvcrt.locking()'s internal retry window
# (~10 seconds) so genuine contention gets a real shot, but short enough that
# a wedged predecessor is surfaced quickly rather than silently hanging.
LOCK_ACQUIRE_TIMEOUT_S = 15.0


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
    except PermissionError:
        # Should not happen with the split-file layout, but be defensive on
        # Windows in case a legacy single-file pid file from an older version
        # is still on disk and locked.
        return {"pid": None, "cmd": None, "start_time_epoch": None, "_locked_legacy": True}
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
    """Inspect existing pid + lock files and decide whether they are stale.

    Does not modify anything. Pure check; the caller decides to clean.
    """
    p_path = pid_file(name)
    data = _read_pid_file(p_path)

    if data is None:
        return StaleInfo(cleaned=False, previous_pid=None, reason="no pid file")
    if data.get("_locked_legacy"):
        # Legacy pid file from a prior version is OS-locked. Treat as live to
        # avoid clobbering a running instance; the user can stop & restart to
        # migrate to the new layout.
        return StaleInfo(cleaned=False, previous_pid=None, reason="live process")
    if data.get("_corrupt"):
        return StaleInfo(cleaned=False, previous_pid=None, reason="corrupt pid file")

    pid = data.get("pid")
    cmd = data.get("cmd") or name
    if pid is None:
        return StaleInfo(cleaned=False, previous_pid=None, reason="missing pid field")
    if _process_matches(pid, cmd):
        return StaleInfo(cleaned=False, previous_pid=pid, reason="live process")
    return StaleInfo(cleaned=False, previous_pid=pid, reason="dead or wrong cmd")


def _safe_unlink(path: Path, warn_on_busy: bool = False) -> bool:
    """Remove `path` if it exists. Return True on success, False otherwise.

    On Windows, the OS can keep a file handle alive for seconds-to-minutes
    after the owning process dies (especially if the file was opened without
    FILE_SHARE_DELETE), causing unlink to raise PermissionError. We don't want
    this to abort startup — but for the lock-file path it's a strong signal
    that a dead predecessor's handle is still lingering, so pass
    ``warn_on_busy=True`` at that site to make the failure visible in logs
    rather than swallowing it silently.
    """
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except PermissionError as e:
        if warn_on_busy:
            log.warning(
                "pidfile: could not unlink %s (handle still held by another process?): %s",
                path, e,
            )
        return False


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via a tempfile + os.replace, so readers never see
    a partial file. tempfile is created in the same directory so the rename is
    atomic on Windows and POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        self.lock_path = lock_file(name)
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
        log.debug("pidfile[%s]: acquire start (pid=%d)", self.name, os.getpid())
        info = check_stale(self.name)
        log.debug(
            "pidfile[%s]: stale check -> reason=%r previous_pid=%s",
            self.name, info.reason, info.previous_pid,
        )
        if info.reason == "live process":
            raise AlreadyRunning(self.name, info.previous_pid or -1)
        if info.previous_pid is not None or info.reason in ("corrupt pid file", "missing pid field"):
            log.debug("pidfile[%s]: cleaning stale pid + lock files", self.name)
            _safe_unlink(self.path)
            _safe_unlink(self.lock_path, warn_on_busy=True)
            self.stale_info = StaleInfo(cleaned=True, previous_pid=info.previous_pid, reason=info.reason)

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        log.debug(
            "pidfile[%s]: opening + locking %s (timeout=%.1fs)",
            self.name, self.lock_path, LOCK_ACQUIRE_TIMEOUT_S,
        )
        try:
            fd = self._open_and_lock_with_timeout(LOCK_ACQUIRE_TIMEOUT_S)
        except TimeoutError as e:
            log.error(
                "pidfile[%s]: open+lock exceeded %.1fs; treating as already-running. "
                "A previous %s may have died leaving a kernel handle on %s.",
                self.name, LOCK_ACQUIRE_TIMEOUT_S, self.name, self.lock_path,
            )
            raise AlreadyRunning(self.name, -1) from e
        except (BlockingIOError, PermissionError, OSError) as e:
            raise AlreadyRunning(self.name, -1) from e
        log.debug("pidfile[%s]: exclusive lock acquired (fd=%d)", self.name, fd)
        self._lock_fd = fd

        record = {
            "pid": os.getpid(),
            "start_time_epoch": int(time.time()),
            "cmd": self.name,
        }
        _atomic_write_text(self.path, json.dumps(record))
        log.debug("pidfile[%s]: pid file written at %s", self.name, self.path)

        self._install_signal_handlers()
        atexit.register(self._atexit_shutdown)
        log.debug("pidfile[%s]: acquire done", self.name)

    def _open_and_lock_with_timeout(self, timeout: float) -> int:
        """Run os.open + _try_exclusive_lock in a worker thread, bounded by
        `timeout` seconds. Returns the locked fd on success.

        Why a thread. Two independent failure modes on Windows can stall the
        main thread past a user-tolerable deadline:

        1. `os.open(lock_path, O_RDWR | O_CREAT)` can block if a dead
           predecessor's kernel handle is still present without FILE_SHARE_*.
        2. `msvcrt.locking(fd, LK_NBLCK, 1)` silently retries ~10 times with
           1-second sleeps before raising, which looks indistinguishable from
           a hang to a user watching the terminal.

        Wrapping both calls in a `threading.Thread(...).join(timeout=...)` gives
        a hard upper bound that works regardless of which syscall is the
        culprit. If the thread is still alive at timeout, we abandon it as a
        daemon (the OS will release any stray handles on process exit) and
        raise TimeoutError, which `acquire()` converts to AlreadyRunning so the
        caller path is uniform.
        """
        result: dict = {"fd": None, "exc": None}

        def target() -> None:
            try:
                fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            except BaseException as e:
                result["exc"] = e
                return
            try:
                self._try_exclusive_lock(fd)
            except BaseException as e:
                try:
                    os.close(fd)
                except OSError:
                    pass
                result["exc"] = e
                return
            result["fd"] = fd

        t = threading.Thread(target=target, daemon=True, name=f"pidfile-{self.name}-lock")
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            # Thread is wedged in a blocking syscall. We can't interrupt it in
            # Python, but daemon=True means it won't keep the interpreter alive
            # past main-thread exit. Abandon it and signal the caller.
            raise TimeoutError(f"pidfile[{self.name}] lock acquisition exceeded {timeout:.1f}s")

        if result["exc"] is not None:
            raise result["exc"]
        assert result["fd"] is not None
        return result["fd"]

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
        _safe_unlink(self.path)
        _safe_unlink(self.lock_path)

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
                # The byte to unlock must be the same one we locked. Seek to 0.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
