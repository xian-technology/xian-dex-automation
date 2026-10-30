import asyncio
from decimal import Decimal

import pytest
from test_execution_journal import begin
from test_submission_recovery import receipt

from xian_dex_automation.storage import AutomationStore


class Network:
    def __init__(self):
        self.broadcasts = []
        self.receipt = None
        self.error = None
        self.rejection = None
        self.on_broadcast = None
        self.nonce_consumed = False

    def transport(self, prepared):
        network = self

        class Transport:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def lookup(self):
                return network.receipt

            async def nonce_consumed(self):
                return network.nonce_consumed

            async def broadcast(self):
                network.broadcasts.append(prepared.transaction.json_bytes)
                if network.on_broadcast:
                    network.on_broadcast(prepared)
                if network.error:
                    raise network.error
                return network.rejection

        return Transport()


def recover(store, monkeypatch, network, *, allow_broadcast=True):
    from xian_dex_automation.execution import reconcile_execution

    monkeypatch.setattr("xian_dex_automation.execution.SwapTransport", network.transport)
    with store.execution_lock() as acquired:
        assert acquired
        return asyncio.run(reconcile_execution(store, allow_broadcast=allow_broadcast))


def test_crash_after_broadcast_recovers_receipt_without_resigning(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path / "state.sqlite3")
    pending = begin(store)
    network = Network()
    network.error = asyncio.CancelledError()
    network.on_broadcast = lambda tx: setattr(network, "receipt", receipt(tx))
    with pytest.raises(asyncio.CancelledError):
        recover(store, monkeypatch, network)
    assert store.pending_execution().broadcast_attempted
    assert store.get_rule_state("r1").baseline_price is None
    network.error = None
    result = recover(AutomationStore(store.path), monkeypatch, network)
    assert result["status"] == "submitted"
    assert len(network.broadcasts) == 1
    assert result["tx_hash"] == pending.prepared.transaction.tx_hash
    assert len(store.list_runs()) == 1
    assert store.get_rule_state("r1").baseline_price == Decimal("1.02")
    assert store.pending_execution() is None


def test_unknown_result_rebroadcasts_only_exact_original_bytes(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path / "state.sqlite3")
    pending = begin(store)
    network = Network()
    network.error = TimeoutError("response lost")
    assert recover(store, monkeypatch, network)["status"] == "pending"
    network.error = None
    network.rejection = "nonce already consumed"
    assert recover(store, monkeypatch, network)["status"] == "pending"
    assert store.pending_execution() is not None
    assert network.broadcasts == [pending.prepared.transaction.json_bytes] * 2
    network.receipt = receipt(pending.prepared, False)
    assert recover(store, monkeypatch, network)["status"] == "failed"
    assert len(network.broadcasts) == 2


def test_first_definitive_rejection_finishes_without_starting_cooldown(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path / "state.sqlite3")
    begin(store)
    network = Network()
    network.rejection = "insufficient funds"
    assert recover(store, monkeypatch, network)["status"] == "failed"
    assert store.pending_execution() is None
    assert store.get_rule_state("r1").last_action_at is None


def test_dry_run_mode_reconciles_but_does_not_broadcast(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path / "state.sqlite3")
    pending = begin(store)
    network = Network()
    assert recover(store, monkeypatch, network, allow_broadcast=False)["status"] == "pending"
    assert not network.broadcasts
    store.mark_broadcast_attempted(pending.run_id)
    network.receipt = receipt(pending.prepared)
    assert recover(store, monkeypatch, network, allow_broadcast=False)["status"] == "submitted"
    assert not network.broadcasts


def test_completion_failure_retries_receipt_without_rebroadcast(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path / "state.sqlite3")
    pending = begin(store)
    network = Network()
    network.on_broadcast = lambda tx: setattr(network, "receipt", receipt(tx))
    with store.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_completion BEFORE DELETE ON pending_execution
            BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
    assert recover(store, monkeypatch, network)["status"] == "pending"
    assert store.pending_execution().broadcast_attempted
    assert store.get_rule_state("r1").baseline_price is None
    with store.connect() as connection:
        connection.execute("DROP TRIGGER fail_completion")
    assert recover(store, monkeypatch, network)["status"] == "submitted"
    assert network.broadcasts == [pending.prepared.transaction.json_bytes]
    assert len(store.list_runs()) == 1
