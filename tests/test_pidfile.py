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
