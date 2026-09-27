import requests

from budget import EntityBudgetManager
from diagnostics import diagnostic_plan, run_diagnostics
from test_collector import Session, config
from tripadvisor_client import TripadvisorClient


def test_network_failure_stops_remaining_diagnostic_requests(tmp_path):
    session = Session([requests.ConnectionError("blocked")])
    budget = EntityBudgetManager(tmp_path / "entity_usage.json")
    client = TripadvisorClient("offline", budget, session=session, max_attempts=1)
    _, report = run_diagnostics(diagnostic_plan(config(), tmp_path), config(), client, tmp_path)
    assert report["stopped"] == "network_error"
    assert len(session.calls) == 1
    assert budget.estimated_used == 5
