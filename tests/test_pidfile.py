"""Tests for PID file supervisor. FRD B.10."""
from __future__ import annotations

import json
import os

import pytest

from app import pidfile as pf
from app.paths import pid_file


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
    from app.paths import lock_file as _lock_file
    with pf.PidFile("worker"):
        assert pid_file("worker").exists()
        assert _lock_file("worker").exists()
        assert pid_file("worker") != _lock_file("worker")
    # both deleted on release
    assert not pid_file("worker").exists()
    assert not _lock_file("worker").exists()


def test_pid_recycled_with_matching_cmd_is_treated_as_stale(tmp_state_dir):
    """If a prior pidfile records pid=P and start_time=T, but the OS has
    since handed P to an unrelated process that happens to have 'worker'
    somewhere in its cmdline, we must not treat it as live. The cross-check
    against the process's kernel create_time rules it out."""
    import psutil

    me = psutil.Process(os.getpid())
    path = pid_file("worker")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Claim our own PID but fake an ancient start time.
    path.write_text(
        json.dumps({"pid": os.getpid(), "cmd": "worker", "start_time_epoch": int(me.create_time()) - 3600})
    )
    info = pf.check_stale("worker")
    assert info.reason == "dead or wrong cmd"  # i.e., not "live process"
    assert info.previous_pid == os.getpid()
