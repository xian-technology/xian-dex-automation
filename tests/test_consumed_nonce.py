import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from test_execution_journal import begin
from test_reconciliation import Network, recover
from test_submission_recovery import receipt, transport
from test_worker import FakeDex, make_worker

from xian_dex_automation.storage import AutomationStore
from xian_dex_automation.worker import AutomationWorker


@pytest.mark.parametrize("nonce,consumed", [(None, False), (0, True), (1, True)])
def test_consumed_nonce_reads_committed_state_for_saved_signer(nonce, consumed):
    t = transport()
    t.client.get_state = AsyncMock(return_value=nonce)
    assert asyncio.run(t.nonce_consumed()) is consumed
    payload = json.loads(t.prepared.transaction.json_bytes)["payload"]
    t.client.get_state.assert_awaited_once_with("__n", payload["sender"], "")


def test_next_nonce_is_not_committed_nonce():
    from xian_py.transaction import create_tx, prepare_transaction
    from xian_py.wallet import Wallet

    t = transport()
    payload = json.loads(t.prepared.transaction.json_bytes)["payload"]
    payload["nonce"] = 1
    t.prepared = replace(
        t.prepared, transaction=prepare_transaction(create_tx(payload, Wallet("1" * 64)))
    )
    t.client.get_state = AsyncMock(return_value=0)
    assert asyncio.run(t.nonce_consumed()) is False


@pytest.mark.parametrize("nonce", [True, "0", -1, 0.5])
def test_invalid_committed_nonce_is_not_evidence(nonce):
    t = transport()
    t.client.get_state = AsyncMock(return_value=nonce)
    with pytest.raises(ValueError, match="nonce"):
        asyncio.run(t.nonce_consumed())


def test_wrong_chain_cannot_release_queue():
    t = transport()
    t.client.get_chain_id.return_value = "wrong-chain"
    t.client.get_state = AsyncMock(return_value=100)
    with pytest.raises(ValueError, match="chain"):
        asyncio.run(t.nonce_consumed())
    t.client.get_state.assert_not_awaited()


@pytest.mark.parametrize("allow_broadcast", [True, False])
def test_consumed_nonce_releases_queue_without_rebroadcast_or_claiming_success(
    tmp_path, monkeypatch, allow_broadcast
):
    store = AutomationStore(tmp_path / "state.sqlite3")
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    network = Network()
    network.nonce_consumed = True
    result = recover(store, monkeypatch, network, allow_broadcast=allow_broadcast)
    assert result["status"] == "outcome_unknown"
    assert result["reason"] == "nonce_consumed_receipt_missing"
    assert result["details"]["nonce_consumed"] is True
    restored = AutomationStore(store.path)
    assert restored.pending_execution() is None
    assert restored.uncertain_execution("r1").prepared == pending.prepared
    assert restored.get_rule_state("r1").last_action_at is None
    assert len(restored.list_runs()) == 1
    assert network.broadcasts == []


def test_failed_unknown_commit_preserves_pending_journal(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path / "state.sqlite3")
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    with store.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_release BEFORE DELETE ON pending_execution
            BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
    network = Network()
    network.nonce_consumed = True
    assert recover(store, monkeypatch, network)["status"] == "pending"
    assert store.pending_execution() is not None
    assert store.uncertain_execution("r1") is None
    assert store.list_runs()[0]["status"] == "pending"
    assert not network.broadcasts


def test_unknown_receipt_read_error_holds_only_affected_rule(tmp_path, monkeypatch):
    worker, store, rule = make_worker(tmp_path, execute=False)
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    network = Network()
    network.nonce_consumed = True
    recover(store, monkeypatch, network)
    independent = rule.model_copy(deep=True)
    independent.id = "independent"
    worker.config.rules.append(independent)
    from xian_dex_automation.rules import RuleRuntimeState

    store.save_rule_state(independent.id, RuleRuntimeState(baseline_price=Decimal("1")))
    monkeypatch.setattr(
        "xian_dex_automation.execution.SwapTransport",
        lambda prepared: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    results = asyncio.run(worker.evaluate_pair(FakeDex(), 1))
    assert results[0]["reason"] == "rule_outcome_unknown"
    assert results[1]["status"] == "dry_run"
    assert store.uncertain_execution(rule.id) is not None


def test_failed_receipt_resolution_keeps_hold_and_original_rule_state(tmp_path, monkeypatch):
    worker, store, rule = make_worker(tmp_path, execute=True)
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    network = Network()
    network.nonce_consumed = True
    recover(store, monkeypatch, network)
    before = store.get_rule_state(rule.id)
    with store.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_resolve BEFORE DELETE ON uncertain_executions
            BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
    network.receipt = receipt(pending.prepared)
    dex = FakeDex()
    results = asyncio.run(worker.evaluate_pair(dex, 1))
    assert results[0]["reason"] == "rule_outcome_unknown"
    assert store.uncertain_execution(rule.id) is not None
    assert store.get_rule_state(rule.id) == before
    assert store.list_runs()[0]["status"] == "outcome_unknown"
    assert not dex.swap_calls


def test_receipt_endpoint_error_does_not_hide_consumed_nonce(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path / "state.sqlite3")
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    t = transport()
    t.lookup = AsyncMock(side_effect=RuntimeError("receipt index unavailable"))
    t.nonce_consumed = AsyncMock(return_value=True)
    t.client.__aenter__ = AsyncMock()
    t.client.__aexit__ = AsyncMock()
    # Use the small transport fixture's context manager implementation.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def open_transport(prepared):
        yield t

    monkeypatch.setattr("xian_dex_automation.execution.SwapTransport", open_transport)
    from xian_dex_automation.execution import reconcile_execution

    assert (
        asyncio.run(reconcile_execution(store, allow_broadcast=True))["status"] == "outcome_unknown"
    )


@pytest.mark.parametrize("success", [True, False])
def test_unknown_rule_survives_restart_and_reload_while_independent_rule_trades(
    tmp_path, monkeypatch, success
):
    worker, store, rule = make_worker(tmp_path, execute=True)
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    network = Network()
    network.nonce_consumed = True
    recover(store, monkeypatch, network)
    config = worker.config.model_copy(deep=True)
    independent = rule.model_copy(deep=True)
    independent.id = "independent"
    config.rules.append(independent)
    config.network.rpc_url = "http://127.0.0.1:27757"
    store.save_active_config(config, previous=worker.config)
    from xian_dex_automation.rules import RuleRuntimeState

    store.save_rule_state(independent.id, RuleRuntimeState(baseline_price=Decimal("1")))
    worker = AutomationWorker(config, AutomationStore(store.path))
    network.on_broadcast = lambda tx: setattr(network, "receipt", receipt(tx))
    dex = FakeDex()
    results = asyncio.run(worker.evaluate_pair(dex, 1))
    assert results[0]["reason"] == "rule_outcome_unknown"
    assert results[1]["status"] == "submitted"
    assert len(dex.swap_calls) == 1
    assert len(store.list_runs()) == 2
    # Receipt restoration resolves the held rule, but reinitializes its market
    # state instead of replaying its original price decision, even on failure.
    network.receipt = receipt(pending.prepared, success)
    results = asyncio.run(worker.evaluate_pair(dex, 1))
    assert results[0]["reason"] == "baseline_initialized"
    assert store.uncertain_execution(rule.id) is None
    assert len(dex.swap_calls) == 1
    assert len(store.list_runs()) == 2
    original = next(run for run in store.list_runs() if run["rule_id"] == rule.id)
    assert original["status"] == ("submitted" if success else "failed")
