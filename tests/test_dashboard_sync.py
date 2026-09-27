import pytest

import main as cli
import budget as budget_module
from budget import BudgetExceeded, EntityBudgetManager
from cache_manager import CacheManager
from collector import plan
from config import Settings
from storage import read_json, write_json
from test_collector import config


def prepare(tmp_path, monkeypatch):
    output = tmp_path / "output"
    write_json(output / "entity_usage.json", {"hard_limit": 1050, "estimated_used": 1016})
    monkeypatch.setattr(cli, "OUTPUT_DIR", output)
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli.Settings, "load", lambda: Settings("offline", 1300))
    def blocked(*args, **kwargs):
        pytest.fail("Sync must not enter collection, geocoding, or diagnostic code")
    for name in ("TripadvisorClient", "BusanGeocoder", "CacheManager", "run_v2_diagnostic", "run_diagnostics"):
        monkeypatch.setattr(cli, name, blocked)
    return output


def test_confirmed_sync_backup_history_and_following_attempt(tmp_path, monkeypatch):
    output = prepare(tmp_path, monkeypatch)
    before = (output / "entity_usage.json").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert cli.main(["--sync-dashboard-usage", "534"]) == 0
    backups = list(output.glob("entity_usage.backup_*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    state = read_json(output / "entity_usage.json")
    assert state["estimated_used"] == state["dashboard_synced_usage"] == 534
    assert state["previous_estimated_used"] == 1016 and state["hard_limit"] == 1300
    manager = EntityBudgetManager(output / "entity_usage.json", 1300)
    manager.limit_run(50)
    assert manager.run_ceiling == 584
    manager.reserve(1)
    saved = read_json(manager.path)
    assert saved["estimated_used"] == 535
    assert saved["dashboard_synced_at"] == state["dashboard_synced_at"]
    assert saved["dashboard_sync_history"] == state["dashboard_sync_history"]
    manager.reserve(49)
    with pytest.raises(BudgetExceeded):
        manager.reserve(1)


@pytest.mark.parametrize("answer", ["n", ""])
def test_decline_no_ledger_changes(tmp_path, monkeypatch, answer):
    output = prepare(tmp_path, monkeypatch)
    before = (output / "entity_usage.json").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert cli.main(["--sync-dashboard-usage", "534"]) == 0
    assert (output / "entity_usage.json").read_bytes() == before
    assert not list(output.glob("entity_usage.backup_*.json"))


@pytest.mark.parametrize("extra", [["--dry-run"], ["--diagnose"], ["--diagnose-compare"],
                                  ["--diagnose-v2"], ["--refresh"], ["--config", "a.json"],
                                  ["--max-locations", "10"], ["--target-reviewed-locations", "5"],
                                  ["--run-entity-budget", "50"], ["--search-strategy", "shared"]])
def test_sync_rejects_combined_options(extra):
    with pytest.raises(SystemExit):
        cli.main(["--sync-dashboard-usage", "534", *extra])


def test_negative_and_over_limit_rejected(tmp_path, monkeypatch):
    output = prepare(tmp_path, monkeypatch)
    before = (output / "entity_usage.json").read_bytes()
    with pytest.raises(SystemExit):
        cli.main(["--sync-dashboard-usage", "-1"])
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Reject before confirmation"))
    assert cli.main(["--sync-dashboard-usage", "1301"]) == 2
    assert (output / "entity_usage.json").read_bytes() == before


def test_sync_write_failure_preserves_ledger_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "entity_usage.json"
    write_json(path, {"estimated_used": 1016})
    before = path.read_bytes()
    manager = EntityBudgetManager(path, 1300)
    def fail(*args):
        raise PermissionError("locked")
    monkeypatch.setattr(budget_module, "write_json", fail)
    with pytest.raises(PermissionError):
        manager.sync_dashboard_usage(534)
    assert path.read_bytes() == before and manager.estimated_used == 1016
    assert next(tmp_path.glob("entity_usage.backup_*.json")).read_bytes() == before


def test_plan_counts_attempts_and_review_not_retried(tmp_path):
    manager = EntityBudgetManager(tmp_path / "entity_usage.json")
    estimate = plan(config(), CacheManager(tmp_path / "cache"), manager)
    assert estimate["additional_entities_without_retries"] == 1 + 5 + 5
    assert estimate["additional_entities_with_all_retries"] == (1 + 5) * 3 + 5


def test_paid_warning_and_1300_config(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("HARD_ENTITY_BUDGET=1300\n")
    assert Settings.load(env).hard_limit == 1300
    path = tmp_path / "entity_usage.json"
    write_json(path, {"estimated_used": 534})
    cli.paid_usage_warning(EntityBudgetManager(path, 1300))
    output = capsys.readouterr().out
    assert all(value in output for value in ("WARNING", "1300", "534", "466", "300"))
