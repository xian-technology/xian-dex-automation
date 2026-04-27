from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xian_dex_automation.config import (
    PriceMoveTriggerConfig,
    RuleConfig,
    SwapExactInActionConfig,
)
from xian_dex_automation.dex import PairSnapshot
from xian_dex_automation.rules import RuleRuntimeState, evaluate_price_move


def make_rule(direction: str = "either") -> RuleConfig:
    return RuleConfig(
        id="r1",
        trigger=PriceMoveTriggerConfig(
            pair_id=1,
            direction=direction,
            threshold_bps=100,
            cooldown_seconds=300,
        ),
        action=SwapExactInActionConfig(
            src="currency",
            amount_in=Decimal("1"),
        ),
    )


def make_snapshot(reserve0: str, reserve1: str) -> PairSnapshot:
    return PairSnapshot(
        pair_id=1,
        token0="con_demo",
        token1="currency",
        reserve0=Decimal(reserve0),
        reserve1=Decimal(reserve1),
    )


def test_first_observation_initializes_baseline() -> None:
    decision = evaluate_price_move(
        rule=make_rule(),
        snapshot=make_snapshot("100", "100"),
        state=RuleRuntimeState(),
        now=datetime.now(UTC),
    )

    assert decision.should_execute is False
    assert decision.reason == "baseline_initialized"
    assert decision.next_baseline_price == Decimal("1")


def test_price_move_triggers_when_threshold_is_reached() -> None:
    decision = evaluate_price_move(
        rule=make_rule(),
        snapshot=make_snapshot("100", "102"),
        state=RuleRuntimeState(baseline_price=Decimal("1")),
        now=datetime.now(UTC),
    )

    assert decision.should_execute is True
    assert decision.change_bps == Decimal("200.00")


def test_direction_down_ignores_upward_moves() -> None:
    decision = evaluate_price_move(
        rule=make_rule(direction="down"),
        snapshot=make_snapshot("100", "102"),
        state=RuleRuntimeState(baseline_price=Decimal("1")),
        now=datetime.now(UTC),
    )

    assert decision.should_execute is False
    assert decision.reason == "threshold_not_reached"


def test_cooldown_blocks_repeated_actions() -> None:
    now = datetime.now(UTC)
    decision = evaluate_price_move(
        rule=make_rule(),
        snapshot=make_snapshot("100", "102"),
        state=RuleRuntimeState(
            baseline_price=Decimal("1"),
            last_action_at=now - timedelta(seconds=10),
        ),
        now=now,
    )

    assert decision.should_execute is False
    assert decision.reason == "cooldown_active"
