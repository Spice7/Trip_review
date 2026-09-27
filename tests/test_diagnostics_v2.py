import json

import pytest
import requests

import diagnostics_v2 as probe
import main as cli
from budget import EntityBudgetManager
from config import API_VERSION, Settings
from storage import write_json
from test_collector import Response, Session


class ProbeSession(Session):
    def mount(self, prefix, adapter):
        assert prefix == "https://" and adapter.max_retries.total == 0


def test_exact_requests_metadata_and_preserved_files(tmp_path, monkeypatch, capsys):
    write_json(tmp_path / "entity_usage.json", {"estimated_used": 114})
    for name in ("reviews.json", "reviews.xlsx", "cache/reviews/test.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserve")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    meta = {"total_in_pool": 6, "matched_language": 0,
            "pool_definition": {"strategy": "recent_months", "months": 6}, "extra": [1, None]}
    session = ProbeSession([Response({"data": [{"text": "PRIVATE_REVIEW"}],
                                      "language_meta": meta, "pagination": {"total_elements": 6}}),
                            Response({"data": [], "language_meta": meta})])
    monkeypatch.setattr(probe.requests, "Session", lambda: session)
    budget = EntityBudgetManager(tmp_path / "entity_usage.json")
    path, rows = probe.run_v2_diagnostic("test-secret", budget, tmp_path)
    assert len(session.calls) == 2 and budget.estimated_used == 116
    for (url, kwargs), identity in zip(session.calls, probe.LOCATION_IDS):
        assert url.endswith(f"/locations/{identity}/reviews")
        assert kwargs["params"] == {"version": 2, "language": "primary", "page": 1,
                                    "size": 3, "sort_by": "MOST_RECENT"}
        assert kwargs["allow_redirects"] is False
    assert str(API_VERSION) == "1"
    assert rows[0]["language_meta"] == meta
    assert [r["returned_count"] for r in rows] == [1, 0]
    assert json.loads(path.read_text(encoding="utf-8")) == rows
    for row in rows:
        assert set(row) == {"location_id", "http_status", "pagination", "language_meta", "returned_count"}
    rendered = capsys.readouterr().out + path.read_text(encoding="utf-8")
    assert "PRIVATE_REVIEW" not in rendered and "test-secret" not in rendered
    for p, contents in before.items():
        if p.name != "entity_usage.json":
            assert p.read_bytes() == contents


@pytest.mark.parametrize("first", [Response({}, 429), Response({}, 500), Response({}, 401),
                                   requests.ConnectionError("secret"), Response({}, 302)])
def test_failure_never_retries_but_attempts_second_id(tmp_path, monkeypatch, first):
    session = ProbeSession([first, Response({"data": []})])
    monkeypatch.setattr(probe.requests, "Session", lambda: session)
    _, rows = probe.run_v2_diagnostic("offline", EntityBudgetManager(tmp_path / "usage.json"), tmp_path)
    assert len(session.calls) == 2 and rows[1]["returned_count"] == 0
    assert rows[0]["returned_count"] is None


@pytest.mark.parametrize("dry", [False, True])
def test_cli_dry_run_and_decline_make_no_requests(tmp_path, monkeypatch, dry):
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cli.Settings, "load", lambda: Settings("offline", 800))
    monkeypatch.setattr("builtins.input", lambda _: "n")
    def blocked(*args, **kwargs):
        pytest.fail("No network or cache access before confirmation")
    monkeypatch.setattr(cli, "run_v2_diagnostic", blocked)
    monkeypatch.setattr(cli, "CacheManager", blocked)
    assert cli.main(["--diagnose-v2"] + (["--dry-run"] if dry else [])) == 0
    assert not (tmp_path / "output/entity_usage.json").exists()


def test_cli_confirmation_runs_probe_once(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cli.Settings, "load", lambda: Settings("offline", 800))
    events = []
    def confirm(_):
        events.append("confirmed")
        return "y"
    def run(*args):
        assert events == ["confirmed"]
        events.append("run")
        return tmp_path / "result.json", [{"http_status": 200, "returned_count": 0}] * 2
    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(cli, "run_v2_diagnostic", run)
    assert cli.main(["--diagnose-v2"]) == 0
    assert events == ["confirmed", "run"]
