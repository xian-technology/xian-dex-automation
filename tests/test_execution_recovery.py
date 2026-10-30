import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_worker import FakeDex, make_snapshot, make_worker

from xian_dex_automation.storage import AutomationStore
from xian_dex_automation.worker import AutomationWorker


def test_overlapping_workers_do_not_submit_the_same_rule_twice(tmp_path, network):
    worker, store, _ = make_worker(tmp_path, execute=True)
    other = AutomationWorker(worker.config, AutomationStore(store.path))

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedDex(FakeDex):
            async def get_pair_snapshot(self, pair_id):
                return make_snapshot()

            async def quote_exact_in(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return await super().quote_exact_in(*args, **kwargs)

        dex = PausedDex()
        first = asyncio.create_task(worker.evaluate_pair(dex, 1))
        await entered.wait()
        second = asyncio.create_task(other.evaluate_pair(dex, 1))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        assert len(dex.swap_calls) == 1
        assert len(network.broadcasts) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "blocked",
    [
        {"status": "pending", "reason": "recovery_pending"},
        {"status": "skipped", "reason": "execution_busy"},
    ],
)
def test_watcher_retries_event_before_advancing_cursor(tmp_path, monkeypatch, blocked):
    worker, store, _ = make_worker(tmp_path, execute=False)
    worker.config.network.poll_interval_seconds = 0.001

    class Events:
        async def watch(self, **kwargs):
            yield SimpleNamespace(id=42, data={"pair": 1})
            assert store.get_cursor("con_pairs:Sync") == 42

    class Client:
        async def __aenter__(self):
            self.client = SimpleNamespace(events=lambda *args: Events())
            return self

        async def __aexit__(self, *args):
            pass

    calls = 0

    async def evaluate(*args):
        nonlocal calls
        calls += 1
        assert store.get_cursor("con_pairs:Sync") is None
        return [blocked] if calls == 1 else [{"status": "skipped", "reason": "cooldown"}]

    monkeypatch.setattr("xian_dex_automation.worker.DexClient", lambda config: Client())
    monkeypatch.setattr(worker, "evaluate_pair", evaluate)
    asyncio.run(worker._watch_events())
    assert calls == 2


def test_event_waits_for_its_pending_swap_without_evaluating_again(tmp_path, monkeypatch):
    worker, _, _ = make_worker(tmp_path, execute=True)
    worker.config.network.poll_interval_seconds = 0.001
    evaluate = AsyncMock(return_value=[{"status": "pending", "reason": "submission_pending"}])
    recover = AsyncMock(
        side_effect=[
            {"status": "skipped", "reason": "execution_busy"},
            {"status": "pending", "reason": "submission_pending"},
            {"status": "submitted", "reason": "price_move_threshold_reached"},
        ]
    )
    monkeypatch.setattr(worker, "evaluate_pair", evaluate)
    monkeypatch.setattr(worker, "recover_once", recover)

    async def scenario():
        async with asyncio.timeout(0.1):
            await worker._evaluate_event_pair(FakeDex(), 1)

    asyncio.run(scenario())
    assert evaluate.await_count == 1
    assert recover.await_count == 3


def test_pending_execution_blocks_preparing_another_swap(tmp_path, network):
    from test_execution_journal import begin

    worker, store, _ = make_worker(tmp_path, execute=True)
    begin(store)
    network.on_broadcast = None
    dex = FakeDex()
    assert asyncio.run(worker.evaluate_pair(dex, 1))[0]["status"] == "pending"
    assert not dex.swap_calls


@pytest.mark.parametrize("confirmed", [False, True])
def test_multiple_rules_continue_only_after_confirmed_completion(tmp_path, network, confirmed):
    worker, store, rule = make_worker(tmp_path, execute=True)
    worker.config.rules.append(rule.model_copy(update={"id": "r2"}, deep=True))
    store.save_rule_state("r2", store.get_rule_state(rule.id))
    if not confirmed:
        network.on_broadcast = None
    dex = FakeDex()
    results = asyncio.run(worker.evaluate_pair(dex, 1))
    assert len(results) == (2 if confirmed else 1)
    assert len(dex.swap_calls) == len(results)
    assert all(result["status"] == ("submitted" if confirmed else "pending") for result in results)


def test_journal_is_committed_before_any_network_submission(tmp_path, network):
    worker, store, _ = make_worker(tmp_path, execute=True)

    def inspect(prepared):
        pending = AutomationStore(store.path).pending_execution()
        assert pending.broadcast_attempted
        assert pending.prepared.transaction == prepared.transaction
        assert store.list_runs()[0]["status"] == "pending"

    network.on_broadcast = inspect
    assert asyncio.run(worker.evaluate_pair(FakeDex(), 1))[0]["status"] == "pending"


def test_journal_write_failure_prevents_broadcast(tmp_path, network, monkeypatch):
    worker, store, _ = make_worker(tmp_path, execute=True)
    monkeypatch.setattr(
        store, "begin_execution", lambda **kwargs: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(worker.evaluate_pair(FakeDex(), 1))
    assert not network.broadcasts


def test_background_recovery_does_not_depend_on_events_or_signer(tmp_path, network, monkeypatch):
    from test_execution_journal import begin

    worker, store, _ = make_worker(tmp_path, execute=True)
    pending = begin(store)
    worker.config.rules = []
    worker.config.network.rpc_url = "http://changed.invalid:26657"
    worker.config.network.poll_interval_seconds = 0.001
    monkeypatch.setattr(worker, "_watch_events", AsyncMock())

    async def scenario():
        task = asyncio.create_task(worker.run_forever())
        try:
            async with asyncio.timeout(2):
                while store.pending_execution() is not None:
                    await asyncio.sleep(0.001)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert network.broadcasts == [pending.prepared.transaction.json_bytes]
    assert store.list_runs()[0]["status"] == "submitted"


def test_periodic_recovery_handles_a_submission_after_watching_starts(tmp_path, network, monkeypatch):
    from test_execution_journal import begin

    worker, store, _ = make_worker(tmp_path, execute=True)
    worker.config.network.poll_interval_seconds = 0.001

    async def watch():
        begin(store)  # Models a separate API writer, with no DEX events.

    monkeypatch.setattr(worker, "_watch_events", watch)

    async def scenario():
        task = asyncio.create_task(worker.run_forever())
        try:
            async with asyncio.timeout(1):
                while not store.list_runs() or store.pending_execution() is not None:
                    await asyncio.sleep(0.001)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert store.list_runs()[0]["status"] == "submitted"
    assert len(network.broadcasts) == 1


def test_busy_recovery_does_not_touch_pending_execution(tmp_path, network):
    from test_execution_journal import begin

    worker, store, _ = make_worker(tmp_path, execute=True)
    pending = begin(store)
    with store.execution_lock() as acquired:
        assert acquired
        assert asyncio.run(worker.recover_once())["reason"] == "execution_busy"
    assert store.pending_execution() == pending
    assert not network.broadcasts


def test_event_wait_finishes_when_another_process_completed_recovery(tmp_path, monkeypatch):
    worker, _, _ = make_worker(tmp_path, execute=True)
    worker.config.network.poll_interval_seconds = 0.001
    recover = AsyncMock(return_value=None)
    monkeypatch.setattr(worker, "recover_once", recover)
    asyncio.run(worker._wait_for_execution())
    recover.assert_awaited_once()
