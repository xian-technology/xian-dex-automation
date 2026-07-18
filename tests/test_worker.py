from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from xian_dex_automation.config import (
    AutomationConfig,
    PriceMoveTriggerConfig,
    RuleConfig,
    SwapExactInActionConfig,
    WalletConfig,
)
from xian_dex_automation.dex import PairSnapshot
from xian_dex_automation.rules import RuleRuntimeState
from xian_dex_automation.storage import AutomationStore
from xian_dex_automation.worker import AutomationWorker


class FakeDex:
    def __init__(
        self,
        *,
        quote: Decimal = Decimal("10"),
        quote_error: Exception | None = None,
        swap_error: Exception | None = None,
    ) -> None:
        self.quote = quote
        self.quote_error = quote_error
        self.swap_error = swap_error
        self.quote_calls: list[dict] = []
        self.swap_calls: list[dict] = []

    async def quote_exact_in(self, snapshot, *, src, amount_in):
        self.quote_calls.append(
            {
                "snapshot": snapshot,
                "src": src,
                "amount_in": amount_in,
            }
        )
        if self.quote_error is not None:
            raise self.quote_error
        return self.quote

    async def swap_exact_in(self, snapshot, action, *, amount_out_min):
        self.swap_calls.append(
            {
                "snapshot": snapshot,
                "action": action,
                "amount_out_min": amount_out_min,
            }
        )
        if self.swap_error is not None:
            raise self.swap_error
        return SimpleNamespace(tx_hash="TX123")


def make_rule() -> RuleConfig:
    return RuleConfig(
        id="r1",
        trigger=PriceMoveTriggerConfig(
            pair_id=1,
            direction="either",
            threshold_bps=100,
            cooldown_seconds=300,
        ),
        action=SwapExactInActionConfig(
            src="currency",
            amount_in=Decimal("5"),
            max_slippage_bps=100,
        ),
    )


def make_snapshot() -> PairSnapshot:
    return PairSnapshot(
        pair_id=1,
        token0="con_token",
        token1="currency",
        reserve0=Decimal("100"),
        reserve1=Decimal("102"),
    )


def make_worker(tmp_path, *, execute: bool) -> tuple[AutomationWorker, AutomationStore, RuleConfig]:
    rule = make_rule()
    config = AutomationConfig(
        wallet=WalletConfig(execute=execute),
        database_path=tmp_path / "automation.sqlite3",
        rules=[rule],
    )
    store = AutomationStore(config.database_path)
    store.save_rule_state("r1", RuleRuntimeState(baseline_price=Decimal("1")))
    return AutomationWorker(config, store), store, rule


def test_worker_records_dry_run_and_updates_cooldown_state(tmp_path) -> None:
    worker, store, rule = make_worker(tmp_path, execute=False)
    dex = FakeDex()

    result = asyncio.run(worker._evaluate_rule(dex, rule, make_snapshot()))

    assert result["status"] == "dry_run"
    assert result["reason"] == "price_move_threshold_reached"
    assert dex.quote_calls[0]["amount_in"] == Decimal("5")
    assert dex.swap_calls == []
    state = store.get_rule_state("r1")
    assert state.baseline_price == Decimal("1.02")
    assert state.last_action_at is not None
    runs = store.list_runs()
    assert runs[0]["status"] == "dry_run"
    assert runs[0]["tx_hash"] is None
    assert runs[0]["details"]["amount_out_min"] == "9.90"


def test_worker_submits_swap_when_execution_is_enabled(tmp_path) -> None:
    worker, store, rule = make_worker(tmp_path, execute=True)
    dex = FakeDex()

    result = asyncio.run(worker._evaluate_rule(dex, rule, make_snapshot()))

    assert result["status"] == "submitted"
    assert result["tx_hash"] == "TX123"
    assert dex.swap_calls[0]["amount_out_min"] == Decimal("9.900")
    runs = store.list_runs()
    assert runs[0]["status"] == "submitted"
    assert runs[0]["tx_hash"] == "TX123"


def test_worker_records_quote_failures(tmp_path) -> None:
    worker, store, rule = make_worker(tmp_path, execute=True)
    dex = FakeDex(quote_error=RuntimeError("quote failed"))

    result = asyncio.run(worker._evaluate_rule(dex, rule, make_snapshot()))

    assert result["status"] == "failed"
    assert result["reason"] == "quote_failed"
    assert dex.swap_calls == []
    runs = store.list_runs()
    assert runs[0]["status"] == "failed"
    assert runs[0]["reason"] == "quote_failed"
    assert runs[0]["details"]["error"] == "quote failed"


def test_worker_records_submission_failures(tmp_path) -> None:
    worker, store, rule = make_worker(tmp_path, execute=True)
    dex = FakeDex(swap_error=RuntimeError("swap failed"))

    result = asyncio.run(worker._evaluate_rule(dex, rule, make_snapshot()))

    assert result["status"] == "failed"
    assert result["reason"] == "submission_failed"
    runs = store.list_runs()
    assert runs[0]["status"] == "failed"
    assert runs[0]["reason"] == "submission_failed"
    assert runs[0]["details"]["error"] == "swap failed"


def test_worker_uses_pair_id_from_sync_event(tmp_path) -> None:
    worker, _, _ = make_worker(tmp_path, execute=False)

    pair_ids = worker._pair_ids_from_event(SimpleNamespace(data={"pair": 7}))

    assert pair_ids == [7]


def test_worker_falls_back_to_configured_pairs_when_sync_event_omits_pair(
    tmp_path,
) -> None:
    worker, _, rule = make_worker(tmp_path, execute=False)
    worker.config.rules = [
        rule.model_copy(update={"trigger": rule.trigger.model_copy(update={"pair_id": 2})}),
        rule.model_copy(update={"id": "r2"}),
        rule.model_copy(update={"id": "r3", "enabled": False}),
    ]

    pair_ids = worker._pair_ids_from_event(
        SimpleNamespace(data={"reserve0": "100", "reserve1": "102"})
    )

    assert pair_ids == [1, 2]
