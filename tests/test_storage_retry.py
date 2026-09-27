import errno

import pytest

import storage
from budget import EntityBudgetManager
from test_collector import Session
from tripadvisor_client import TripadvisorClient


@pytest.mark.parametrize("failures", [1, 2])
def test_replace_recovers_and_cleans_temp(tmp_path, monkeypatch, failures):
    path = tmp_path / "usage.json"
    storage.write_json(path, {"old": 1})
    replace = storage.os.replace
    attempts, waits = [], []
    def flaky(source, target):
        attempts.append(1)
        if len(attempts) <= failures:
            raise PermissionError("simulated file lock")
        replace(source, target)
    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", waits.append)
    storage.write_json(path, {"new": 2})
    assert storage.read_json(path) == {"new": 2}
    assert len(attempts) == failures + 1 and len(waits) == failures
    assert list(tmp_path.glob("*.tmp")) == []


def test_permanent_replace_failure_preserves_target_and_blocks_http(tmp_path, monkeypatch):
    path = tmp_path / "entity_usage.json"
    storage.write_json(path, {"estimated_used": 534})
    before = path.read_bytes()
    budget = EntityBudgetManager(path, 1300)
    session = Session([])
    attempts, waits = [], []
    def locked(*args):
        attempts.append(1)
        raise PermissionError("locked")
    monkeypatch.setattr(storage.os, "replace", locked)
    monkeypatch.setattr(storage.time, "sleep", waits.append)
    with pytest.raises(PermissionError):
        TripadvisorClient("offline", budget, session=session).reviews("1")
    assert len(attempts) == 8 and len(waits) == 7
    assert path.read_bytes() == before and budget.estimated_used == 534
    assert not session.calls and not list(tmp_path.glob("*.tmp"))


def test_unrelated_os_error_not_retried(tmp_path, monkeypatch):
    attempts = []
    def failed(*args):
        attempts.append(1)
        raise OSError(errno.ENOSPC, "full")
    monkeypatch.setattr(storage.os, "replace", failed)
    with pytest.raises(OSError):
        storage.write_json(tmp_path / "data.json", {})
    assert len(attempts) == 1


def test_windows_sharing_violation_retries(tmp_path, monkeypatch):
    replace = storage.os.replace
    calls = []
    def sharing(source, target):
        calls.append(1)
        if len(calls) == 1:
            error = OSError("sharing violation")
            error.winerror = 32
            raise error
        replace(source, target)
    monkeypatch.setattr(storage.os, "replace", sharing)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    storage.write_json(tmp_path / "data.json", {"ok": True})
    assert len(calls) == 2
