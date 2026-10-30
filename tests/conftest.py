import pytest
from test_reconciliation import Network
from test_submission_recovery import receipt


@pytest.fixture
def network(monkeypatch):
    network = Network()
    network.on_broadcast = lambda prepared: setattr(network, "receipt", receipt(prepared))
    monkeypatch.setattr("xian_dex_automation.execution.SwapTransport", network.transport)
    return network
