from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .config import AutomationConfig, RuleConfig
from .dex import BPS, DexClient, PairSnapshot
from .rules import RuleRuntimeState, evaluate_price_move
from .storage import AutomationStore

logger = logging.getLogger(__name__)


def _decimal_details(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


class AutomationWorker:
    def __init__(self, config: AutomationConfig, store: AutomationStore):
        self.config = config
        self.store = store

    async def run_forever(self) -> None:
        cursor_name = f"{self.config.dex.pairs_contract}:Sync"
        cursor = self.store.get_cursor(cursor_name)
        async with DexClient(self.config) as dex:
            event_client = dex.client.events(
                self.config.dex.pairs_contract,
                "Sync",
            )
            logger.info(
                "watching %s Sync events after cursor %s",
                self.config.dex.pairs_contract,
                cursor,
            )
            async for event in event_client.watch(after_id=cursor):
                pair_id = self._pair_id_from_event(event)
                if pair_id is not None:
                    await self.evaluate_pair(dex, pair_id)
                if event.id is not None:
                    self.store.save_cursor(cursor_name, int(event.id))
                    cursor = int(event.id)

    async def evaluate_pair_once(self, pair_id: int) -> list[dict[str, Any]]:
        async with DexClient(self.config) as dex:
            return await self.evaluate_pair(dex, pair_id)

    async def evaluate_pair(
        self,
        dex: DexClient,
        pair_id: int,
    ) -> list[dict[str, Any]]:
        snapshot = await dex.get_pair_snapshot(pair_id)
        results: list[dict[str, Any]] = []
        for rule in self._matching_rules(pair_id):
            result = await self._evaluate_rule(dex, rule, snapshot)
            results.append(result)
        return results

    async def _evaluate_rule(
        self,
        dex: DexClient,
        rule: RuleConfig,
        snapshot: PairSnapshot,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        state = self.store.get_rule_state(rule.id)
        decision = evaluate_price_move(
            rule=rule,
            snapshot=snapshot,
            state=state,
            now=now,
        )
        details = {
            "pair_id": snapshot.pair_id,
            "token0": snapshot.token0,
            "token1": snapshot.token1,
            "reserve0": str(snapshot.reserve0),
            "reserve1": str(snapshot.reserve1),
            "current_price": _decimal_details(decision.current_price),
            "change_bps": _decimal_details(decision.change_bps),
            "execute_enabled": self.config.wallet.execute,
        }

        if (
            decision.next_baseline_price is not None
            and not decision.should_execute
        ):
            self.store.save_rule_state(
                rule.id,
                RuleRuntimeState(
                    baseline_price=decision.next_baseline_price,
                    last_action_at=state.last_action_at,
                ),
            )

        if not decision.should_execute:
            return {
                "rule_id": rule.id,
                "status": "skipped",
                "reason": decision.reason,
                "details": details,
            }

        try:
            quote = await dex.quote_exact_in(
                snapshot,
                src=rule.action.src,
                amount_in=rule.action.amount_in,
            )
        except Exception as exc:
            details["error"] = str(exc)
            self.store.append_run(
                rule_id=rule.id,
                status="failed",
                reason="quote_failed",
                tx_hash=None,
                details=details,
            )
            return {
                "rule_id": rule.id,
                "status": "failed",
                "reason": "quote_failed",
                "details": details,
            }

        amount_out_min = quote * (
            (BPS - Decimal(rule.action.max_slippage_bps)) / BPS
        )
        details.update(
            {
                "amount_in": str(rule.action.amount_in),
                "quoted_amount_out": str(quote),
                "amount_out_min": str(amount_out_min),
                "src": rule.action.src,
            }
        )

        if not self.config.wallet.execute:
            self._record_action_state(rule, decision, now)
            self.store.append_run(
                rule_id=rule.id,
                status="dry_run",
                reason=decision.reason,
                tx_hash=None,
                details=details,
            )
            return {
                "rule_id": rule.id,
                "status": "dry_run",
                "reason": decision.reason,
                "details": details,
            }

        try:
            submission = await dex.swap_exact_in(
                snapshot,
                rule.action,
                amount_out_min=amount_out_min,
            )
        except Exception as exc:
            details["error"] = str(exc)
            self.store.append_run(
                rule_id=rule.id,
                status="failed",
                reason="submission_failed",
                tx_hash=None,
                details=details,
            )
            return {
                "rule_id": rule.id,
                "status": "failed",
                "reason": "submission_failed",
                "details": details,
            }

        tx_hash = getattr(submission, "tx_hash", None)
        self._record_action_state(rule, decision, now)
        self.store.append_run(
            rule_id=rule.id,
            status="submitted",
            reason=decision.reason,
            tx_hash=tx_hash,
            details=details,
        )
        return {
            "rule_id": rule.id,
            "status": "submitted",
            "reason": decision.reason,
            "tx_hash": tx_hash,
            "details": details,
        }

    def _record_action_state(
        self,
        rule: RuleConfig,
        decision: Any,
        now: datetime,
    ) -> None:
        self.store.save_rule_state(
            rule.id,
            RuleRuntimeState(
                baseline_price=decision.next_baseline_price,
                last_action_at=now,
            ),
        )

    def _matching_rules(self, pair_id: int) -> list[RuleConfig]:
        return [
            rule
            for rule in self.config.rules
            if rule.enabled and rule.trigger.pair_id == pair_id
        ]

    @staticmethod
    def _pair_id_from_event(event: Any) -> int | None:
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            return None
        value = data.get("pair")
        if value is None:
            return None
        return int(value)


async def sleep_forever() -> None:
    while True:
        await asyncio.sleep(3600)
