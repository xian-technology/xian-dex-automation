import asyncio
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from test_execution_journal import begin
from test_submission_recovery import receipt
from test_worker import FakeDex, make_worker


def test_configuration_change_drains_active_evaluation_and_gates_other_workers(tmp_path, network):
    from xian_dex_automation.configuration import ConfigurationManager
    from xian_dex_automation.worker import AutomationWorker

    worker, store, _ = make_worker(tmp_path, execute=True)
    other = AutomationWorker(worker.config, store)
    manager = ConfigurationManager(worker)
    target = worker.config.model_copy(deep=True)
    target.network.rpc_url = "http://127.0.0.1:26658"
    committed = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedDex(FakeDex):
            async def quote_exact_in(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return await super().quote_exact_in(*args, **kwargs)

        trade = asyncio.create_task(worker.evaluate_pair(PausedDex(), 1))
        await entered.wait()
        change = asyncio.create_task(manager.update(target, lambda: committed.append(True)))
        await asyncio.sleep(0.01)
        assert not committed
        assert (await other.evaluate_pair(FakeDex(), 1))[0]["reason"] == "configuration_draining"
        release.set()
        await trade
        await change
        assert store.active_config(worker.config) == target
        assert committed == [True]
        assert (await other.evaluate_pair(FakeDex(), 1))[0]["reason"] == "configuration_changed"

    asyncio.run(scenario())


def test_reload_waits_for_unknown_receipt_and_can_be_cancelled_without_applying(tmp_path, network):
    from xian_dex_automation.configuration import ConfigurationManager

    worker, store, _ = make_worker(tmp_path, execute=True)
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    network.on_broadcast = None
    target = worker.config.model_copy(deep=True)
    target.wallet.execute = False
    committed = []

    async def scenario():
        task = asyncio.create_task(
            ConfigurationManager(worker).update(target, lambda: committed.append(True))
        )
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not network.broadcasts
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not committed
        assert store.active_config(worker.config) == worker.config
        with store.configuration_lock() as acquired:
            assert acquired
        assert store.pending_execution() is not None

    asyncio.run(scenario())


def test_reload_reconciles_on_requested_rpc_before_committing(tmp_path, monkeypatch, network):
    from xian_dex_automation.configuration import ConfigurationManager

    worker, store, _ = make_worker(tmp_path, execute=True)
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    network.receipt = receipt(pending.prepared)
    target = worker.config.model_copy(deep=True)
    target.network.rpc_url = "http://127.0.0.1:26658"
    seen = []

    def transport(prepared):
        seen.append(prepared)
        return network.transport(prepared)

    monkeypatch.setattr("xian_dex_automation.execution.SwapTransport", transport)
    asyncio.run(ConfigurationManager(worker).update(target, lambda: None))
    assert seen[0].rpc_url == target.network.rpc_url
    assert seen[0].chain_id == pending.prepared.chain_id
    assert seen[0].transaction == pending.prepared.transaction
    assert not network.broadcasts
    assert store.pending_execution() is None
    assert store.active_config(worker.config) == target


def test_unusable_replacement_rpc_falls_back_to_original_for_drain(tmp_path, monkeypatch, network):
    from xian_dex_automation.configuration import ConfigurationManager

    worker, store, _ = make_worker(tmp_path, execute=True)
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    network.receipt = receipt(pending.prepared)
    target = worker.config.model_copy(deep=True)
    target.network.rpc_url = "http://127.0.0.1:26658"
    seen = []

    def transport(prepared):
        seen.append(prepared.rpc_url)
        if prepared.rpc_url == target.network.rpc_url:
            raise ValueError("RPC chain does not match the persisted transaction chain")
        return network.transport(prepared)

    monkeypatch.setattr("xian_dex_automation.execution.SwapTransport", transport)

    async def scenario():
        async with asyncio.timeout(0.5):
            await ConfigurationManager(worker).update(target, lambda: None)

    asyncio.run(scenario())
    assert seen == [target.network.rpc_url, pending.prepared.rpc_url]
    assert not network.broadcasts
    assert store.pending_execution() is None


def test_enabling_execution_can_drain_a_previously_disabled_signed_transaction(tmp_path, network):
    from xian_dex_automation.configuration import ConfigurationManager

    worker, store, _ = make_worker(tmp_path, execute=False)
    begin(store)
    target = worker.config.model_copy(deep=True)
    target.wallet.execute = True

    async def scenario():
        async with asyncio.timeout(0.5):
            await ConfigurationManager(worker).update(target, lambda: None)

    asyncio.run(scenario())
    assert len(network.broadcasts) == 1
    assert store.pending_execution() is None


@pytest.mark.parametrize("change", ["rpc", "chain", "dex", "pair", "threshold"])
def test_reload_keeps_cooldown_but_reinitializes_changed_market_state(tmp_path, change):
    from xian_dex_automation.configuration import ConfigurationManager
    from xian_dex_automation.rules import RuleRuntimeState

    worker, store, rule = make_worker(tmp_path, execute=False)
    now = datetime.now(UTC)
    store.save_rule_state(rule.id, RuleRuntimeState(Decimal("1"), now))
    store.save_cursor("con_pairs:Sync", 42)
    target = worker.config.model_copy(deep=True)
    if change == "rpc":
        target.network.rpc_url = "http://127.0.0.1:26658"
    elif change == "chain":
        target.network.chain_id = "changed-chain"
    elif change == "dex":
        target.dex.pairs_contract = "con_other_pairs"
    elif change == "pair":
        target.rules[0].trigger.pair_id = 2
    else:
        target.rules[0].trigger.threshold_bps = 123
    asyncio.run(ConfigurationManager(worker).update(target, lambda: None))
    state = store.get_rule_state(rule.id)
    assert state.last_action_at == now
    assert state.baseline_price == (Decimal("1") if change == "threshold" else None)
    assert store.get_cursor("con_pairs:Sync") == (None if change in {"rpc", "chain", "dex"} else 42)


def test_competing_configuration_write_is_rejected(tmp_path):
    from xian_dex_automation.configuration import ConfigurationBusy, ConfigurationManager

    worker, store, _ = make_worker(tmp_path, execute=False)
    with store.configuration_lock() as acquired:
        assert acquired
        with pytest.raises(ConfigurationBusy):
            asyncio.run(ConfigurationManager(worker).update(worker.config, lambda: None))
    assert store.config_generation() == 0


@pytest.mark.parametrize("draining", [True, False])
def test_watcher_cannot_open_signer_during_drain_or_with_stale_revision(tmp_path, monkeypatch, draining):
    from xian_dex_automation.worker import ConfigurationChanged

    worker, store, _ = make_worker(tmp_path, execute=False)
    if not draining:
        store.save_active_config(worker.config, previous=worker.config)
    client = Mock(side_effect=AssertionError("signer opened before configuration was ready"))
    monkeypatch.setattr("xian_dex_automation.worker.DexClient", client)
    gate = store.configuration_lock() if draining else nullcontext(True)
    with gate as acquired:
        assert acquired
        with pytest.raises(ConfigurationChanged):
            asyncio.run(worker._watch_events())
    client.assert_not_called()


def test_explicit_chain_switch_reconciles_only_on_original_chain(tmp_path, monkeypatch, network):
    from xian_dex_automation.configuration import ConfigurationManager

    worker, store, _ = make_worker(tmp_path, execute=True)
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    network.receipt = receipt(pending.prepared)
    target = worker.config.model_copy(deep=True)
    target.network.rpc_url = "http://127.0.0.1:26658"
    target.network.chain_id = "different-chain"
    seen = []

    def transport(prepared):
        seen.append(prepared.rpc_url)
        return network.transport(prepared)

    monkeypatch.setattr("xian_dex_automation.execution.SwapTransport", transport)
    asyncio.run(ConfigurationManager(worker).update(target, lambda: None))
    assert seen == [pending.prepared.rpc_url]
    assert store.active_config(worker.config) == target


def test_api_restarts_watcher_rpc_and_signer_after_config_and_key_changes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_service import ADMIN_TOKEN, admin_headers
    from xian_py.wallet import Wallet

    from xian_dex_automation.config import AutomationConfig
    from xian_dex_automation.dex import DexClient
    from xian_dex_automation.service import create_app

    opened, closed = [], []

    class Events:
        async def watch(self, **kwargs):
            await asyncio.Event().wait()
            yield  # A quiet chain must not prevent configuration reload.

    class ObservedDex(DexClient):
        async def __aenter__(self):
            await super().__aenter__()
            self.client.events = lambda *args: Events()
            opened.append((self.config.network.rpc_url, self.wallet_address))
            return self

        async def __aexit__(self, *args):
            closed.append(self.config.network.rpc_url)
            await super().__aexit__(*args)

    monkeypatch.setattr("xian_dex_automation.worker.DexClient", ObservedDex)
    config = AutomationConfig(database_path=tmp_path / "db.sqlite3")
    app = create_app(
        config, config_path=tmp_path / "config.yaml", start_worker=True, admin_token=ADMIN_TOKEN
    )

    def wait_for(predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    with TestClient(app) as client:
        wait_for(lambda: bool(opened))
        new_yaml = client.get("/config.yaml", headers=admin_headers()).text.replace(
            "26657", "26658"
        )
        response = client.put(
            "/config.yaml",
            content=new_yaml,
            headers={**admin_headers(), "content-type": "text/plain"},
        )
        assert response.status_code == 200
        wait_for(lambda: len(opened) == 2)
        assert closed == ["http://127.0.0.1:26657"]
        assert opened[-1][0] == "http://127.0.0.1:26658"
        response = client.post(
            "/wallet/import", json={"private_key": "2" * 64}, headers=admin_headers()
        )
        assert response.status_code == 200
        wait_for(lambda: len(opened) == 3)
        assert opened[-1][1] == Wallet("2" * 64).public_key
        response = client.post(
            "/wallet/import",
            json={"private_key": "3" * 64, "overwrite": True},
            headers=admin_headers(),
        )
        assert response.status_code == 200
        wait_for(lambda: len(opened) == 4)
        assert opened[-1][1] == Wallet("3" * 64).public_key
        assert client.get("/health").json()["configuration_status"] == "active"
    assert len(closed) == 4


def test_failed_config_write_keeps_active_settings_and_releases_gate(tmp_path):
    from xian_dex_automation.configuration import ConfigurationManager

    worker, store, _ = make_worker(tmp_path, execute=False)
    target = worker.config.model_copy(deep=True)
    target.wallet.recipient = "changed"

    def fail():
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        asyncio.run(ConfigurationManager(worker).update(target, fail))
    assert store.active_config(worker.config) == worker.config
    assert store.config_generation() == 0
    with store.configuration_lock() as acquired:
        assert acquired


def test_interrupted_config_replacement_preserves_readable_previous_file(tmp_path, monkeypatch):
    from xian_dex_automation.config import load_config, save_config

    worker, _, _ = make_worker(tmp_path, execute=False)
    path = tmp_path / "config.yaml"
    save_config(worker.config, path)
    before = path.read_bytes()
    target = worker.config.model_copy(deep=True)
    target.wallet.execute = True

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr("os.replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        save_config(target, path)
    assert path.read_bytes() == before
    assert load_config(path).wallet.execute is False
    assert not list(tmp_path.glob(".config.yaml.*"))


def test_worker_recovers_from_client_start_failure_after_config_is_corrected(tmp_path, monkeypatch):
    from xian_dex_automation.configuration import ConfigurationManager

    worker, store, _ = make_worker(tmp_path, execute=False)
    worker.config.network.poll_interval_seconds = 0.01

    async def run_configuration():
        if worker.config.wallet.recipient is None:
            raise ValueError("invalid client settings")
        worker.last_error = None
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_run_configuration", run_configuration)

    async def scenario():
        task = asyncio.create_task(worker.run_forever())
        try:
            async with asyncio.timeout(2):
                while getattr(worker, "last_error", None) is None:
                    assert not task.done()
                    await asyncio.sleep(0.01)
                target = worker.config.model_copy(deep=True)
                target.wallet.recipient = "fixed"
                await ConfigurationManager(worker).update(target, lambda: None)
                while worker.last_error is not None:
                    await asyncio.sleep(0.01)
                assert worker.config == target
                assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
