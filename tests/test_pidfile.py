"""Tests for PID file supervisor. FRD B.10."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from app import pidfile as pf
from app.paths import lock_file, pid_file


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    return tmp_path


def test_acquire_writes_pid(tmp_state_dir):
    with pf.PidFile("worker") as p:
        data = json.loads(pid_file("worker").read_text())
        assert data["pid"] == os.getpid()
        assert data["cmd"] == "worker"
        assert p.stale_info is None
    assert not pid_file("worker").exists()


def test_double_acquire_raises(tmp_state_dir):
    with pf.PidFile("worker"):
        with pytest.raises(pf.AlreadyRunning):
            pf.PidFile("worker").acquire()


def test_stale_pid_is_cleaned(tmp_state_dir):
    path = pid_file("worker")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a PID that is very unlikely to belong to a live process.
    path.write_text(json.dumps({"pid": 999999, "cmd": "worker", "start_time_epoch": 0}))

    with pf.PidFile("worker") as p:
        assert p.stale_info is not None
        assert p.stale_info.cleaned is True
        assert p.stale_info.previous_pid == 999999
        data = json.loads(path.read_text())
        assert data["pid"] == os.getpid()


def test_shutdown_callbacks_run_in_reverse(tmp_state_dir):
    calls: list[str] = []
    with pf.PidFile("worker") as p:
        p.register_shutdown(lambda: calls.append("a"))
        p.register_shutdown(lambda: calls.append("b"))
    assert calls == ["b", "a"]


def test_corrupt_pid_file_is_cleaned(tmp_state_dir):
    path = pid_file("worker")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json")
    with pf.PidFile("worker") as p:
        assert p.stale_info is not None
        assert p.stale_info.cleaned is True


def test_different_names_do_not_conflict(tmp_state_dir):
    with pf.PidFile("worker"), pf.PidFile("web"):
        pass


def test_pid_file_readable_while_lock_held(tmp_state_dir):
    """Regression: on Windows, locking the pid file directly used to make it
    unreadable from other processes (PermissionError 13). The fix splits the
    lock onto a separate sentinel file. The pid file must remain freely
    readable while the lock is held — this is what the web UI relies on to
    show the worker status pill.
    """
    with pf.PidFile("worker"):
        # Plain Path.read_text from a fresh handle must succeed.
        text = pid_file("worker").read_text()
        assert json.loads(text)["pid"] == os.getpid()
        # check_stale uses the same code path the web UI calls.
        info = pf.check_stale("worker")
        assert info.reason == "live process"


def test_lock_file_separate_from_pid_file(tmp_state_dir):
    with pf.PidFile("worker"):
        assert pid_file("worker").exists()
        assert lock_file("worker").exists()
        assert pid_file("worker") != lock_file("worker")
    # both deleted on release
    assert not pid_file("worker").exists()
    assert not lock_file("worker").exists()


def test_safe_unlink_warns_on_busy_lock(tmp_state_dir, monkeypatch, caplog):
    """If the lock file cannot be unlinked (simulating Windows kernel handle
    leak from a dead predecessor), the failure must surface as a WARNING log
    rather than being silently swallowed — otherwise the startup hang mode
    the fix is guarding against is diagnostically invisible."""
    victim = tmp_state_dir / "run" / "worker.lock"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("")

    def boom(self):  # noqa: ARG001
        raise PermissionError(13, "locked by another process")

    monkeypatch.setattr(Path, "unlink", boom)

    with caplog.at_level(logging.WARNING, logger="pidfile"):
        ok = pf._safe_unlink(victim, warn_on_busy=True)

    assert ok is False
    assert any(
        "could not unlink" in rec.getMessage() and rec.levelno == logging.WARNING
        for rec in caplog.records
    ), f"expected WARNING about busy unlink, got: {[(r.levelname, r.getMessage()) for r in caplog.records]}"


def test_safe_unlink_silent_without_warn_flag(tmp_state_dir, monkeypatch, caplog):
    """The pid-file path passes warn_on_busy=False, so a racy reader on the
    pid file shouldn't spam warnings. Only the lock path opts in."""
    victim = tmp_state_dir / "run" / "worker.pid"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("")

    monkeypatch.setattr(
        Path, "unlink",
        lambda self: (_ for _ in ()).throw(PermissionError(13, "busy")),
    )

    with caplog.at_level(logging.WARNING, logger="pidfile"):
        ok = pf._safe_unlink(victim, warn_on_busy=False)

    assert ok is False
    assert not any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_stale_cleanup_logs_warning_when_lock_busy(tmp_state_dir, monkeypatch, caplog):
    """End-to-end: a stale pid file plus a lock path whose unlink raises
    PermissionError (simulating the kernel-handle-leak Windows failure mode)
    should still allow acquire() to proceed — because a fresh os.open + lock
    on the same path generally succeeds once the predecessor's lock is gone —
    AND it must log the busy-unlink warning so the operator has a breadcrumb.
    """
    path = pid_file("worker")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 999999, "cmd": "worker", "start_time_epoch": 0}))
    lock_path = lock_file("worker")
    lock_path.write_text("")

    real_unlink = Path.unlink

    def selective_unlink(self):
        if self == lock_path:
            raise PermissionError(13, "lock still held by dead process handle")
        return real_unlink(self)

    monkeypatch.setattr(Path, "unlink", selective_unlink)

    with caplog.at_level(logging.WARNING, logger="pidfile"):
        try:
            with pf.PidFile("worker") as p:
                assert p.stale_info is not None
                assert p.stale_info.cleaned is True
        finally:
            monkeypatch.setattr(Path, "unlink", real_unlink)

    assert any(
        "could not unlink" in rec.getMessage() and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_lock_acquisition_timeout_raises_already_running(tmp_state_dir, monkeypatch):
    """If `_try_exclusive_lock` itself hangs (modeling msvcrt.locking's
    10-retry-with-sleep behavior on a file a dead predecessor's handle is
    still anchoring), the timeout ceiling must convert this into a clean
    `AlreadyRunning` rather than a silent multi-minute freeze."""
    import threading

    block = threading.Event()

    def hang(_fd):
        block.wait()  # never set; emulates a wedged blocking syscall

    monkeypatch.setattr(pf.PidFile, "_try_exclusive_lock", staticmethod(hang))
    monkeypatch.setattr(pf, "LOCK_ACQUIRE_TIMEOUT_S", 0.5)

    try:
        with pytest.raises(pf.AlreadyRunning):
            pf.PidFile("worker").acquire()
    finally:
        block.set()  # release the daemon worker thread so it can exit


def test_acquire_emits_debug_instrumentation(tmp_state_dir, caplog):
    """The fix adds DEBUG breadcrumbs around each risky call in acquire() so
    that next time a worker start hangs we can tell exactly where. Smoke-check
    that the expected breadcrumb stages show up during a clean acquire."""
    with caplog.at_level(logging.DEBUG, logger="pidfile"):
        with pf.PidFile("worker"):
            pass

    messages = [rec.getMessage() for rec in caplog.records if rec.name == "pidfile"]
    joined = "\n".join(messages)
    assert "acquire start" in joined
    assert "stale check" in joined
    assert "opening + locking" in joined
    assert "exclusive lock acquired" in joined
    assert "pid file written" in joined
    assert "acquire done" in joined
