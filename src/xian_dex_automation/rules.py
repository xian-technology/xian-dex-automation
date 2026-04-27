from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .config import RuleConfig
from .dex import PairSnapshot

BPS = Decimal("10000")


@dataclass(frozen=True)
class RuleRuntimeState:
    baseline_price: Decimal | None = None
    last_action_at: datetime | None = None


@dataclass(frozen=True)
class RuleDecision:
    rule_id: str
    should_execute: bool
    reason: str
    current_price: Decimal | None
    change_bps: Decimal | None = None
    next_baseline_price: Decimal | None = None


def _threshold_reached(
    *,
    direction: str,
    change_bps: Decimal,
    threshold_bps: int,
) -> bool:
    threshold = Decimal(threshold_bps)
    if direction == "up":
        return change_bps >= threshold
    if direction == "down":
        return change_bps <= -threshold
    return abs(change_bps) >= threshold


def evaluate_price_move(
    *,
    rule: RuleConfig,
    snapshot: PairSnapshot,
    state: RuleRuntimeState,
    now: datetime,
) -> RuleDecision:
    current_price = snapshot.price_token1_per_token0
    if current_price is None:
        return RuleDecision(
            rule_id=rule.id,
            should_execute=False,
            reason="invalid_reserves",
            current_price=None,
        )

    if state.baseline_price is None:
        return RuleDecision(
            rule_id=rule.id,
            should_execute=False,
            reason="baseline_initialized",
            current_price=current_price,
            next_baseline_price=current_price,
        )

    if state.baseline_price <= 0:
        return RuleDecision(
            rule_id=rule.id,
            should_execute=False,
            reason="invalid_baseline",
            current_price=current_price,
            next_baseline_price=current_price,
        )

    change_bps = (
        (current_price - state.baseline_price) / state.baseline_price
    ) * BPS
    trigger = rule.trigger
    if not _threshold_reached(
        direction=trigger.direction,
        change_bps=change_bps,
        threshold_bps=trigger.threshold_bps,
    ):
        return RuleDecision(
            rule_id=rule.id,
            should_execute=False,
            reason="threshold_not_reached",
            current_price=current_price,
            change_bps=change_bps,
        )

    if state.last_action_at is not None and trigger.cooldown_seconds > 0:
        cooldown_until = state.last_action_at + timedelta(
            seconds=trigger.cooldown_seconds
        )
        if now < cooldown_until:
            return RuleDecision(
                rule_id=rule.id,
                should_execute=False,
                reason="cooldown_active",
                current_price=current_price,
                change_bps=change_bps,
            )

    return RuleDecision(
        rule_id=rule.id,
        should_execute=True,
        reason="price_move_threshold_reached",
        current_price=current_price,
        change_bps=change_bps,
        next_baseline_price=current_price,
    )
