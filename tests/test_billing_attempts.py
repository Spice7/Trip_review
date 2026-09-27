import pytest

from budget import BudgetExceeded, EntityBudgetManager
from test_collector import Response, Session
from tripadvisor_client import TripadvisorClient


@pytest.mark.parametrize("endpoint", ["/catalog/locations/nearby", "/locations/nearby",
                                      "/locations/1", "/locations/1/reviews"])
def test_every_allowed_endpoint_costs_one_attempt(tmp_path, endpoint):
    budget = EntityBudgetManager(tmp_path / "entity_usage.json", 1300)
    session = Session([Response({"data": []})])
    client = TripadvisorClient("offline", budget, session=session)
    client.get(endpoint, {"size": 5})
    assert len(session.calls) == 1 and budget.estimated_used == 1


def test_search_retry_costs_one_each_and_obeys_run_budget(tmp_path):
    budget = EntityBudgetManager(tmp_path / "entity_usage.json", 1300)
    budget.limit_run(2)
    session = Session([Response({}, 500), Response({}, 500), Response({"data": []})])
    client = TripadvisorClient("offline", budget, session=session, sleep=lambda _: None)
    with pytest.raises(BudgetExceeded):
        client.get("/catalog/locations/nearby", {"size": 5})
    assert len(session.calls) == 2 and budget.estimated_used == 2
