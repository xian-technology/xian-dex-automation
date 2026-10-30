from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .config import AutomationConfig, RuleConfig
from .dex import BPS, DexClient, PairSnapshot
from .execution import reconcile_execution
from .rules import RuleRuntimeState, evaluate_price_move
from .storage import AutomationStore

logger = logging.getLogger(__name__)


def _decimal_details(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _execution_blocked(result: dict[str, Any]) -> bool:
    return result["status"] == "pending" or result["reason"] in {
        "execution_busy", "configuration_draining",
    }


class ConfigurationChanged(Exception):
    """Internal signal to close the old watcher and recreate its client."""


class AutomationWorker:
    def __init__(self, config: AutomationConfig, store: AutomationStore):
        self.config = config
        self.store = store
        self.config_generation = store.config_generation()
        self.last_error: str | None = None

    async def run_forever(self) -> None:
        while True:
            self.config = self.store.active_config(self.config)
            self.config_generation = self.store.config_generation()
            try:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._run_configuration())
                    tasks.create_task(self._watch_configuration())
            except* ConfigurationChanged:
                pass
            except* Exception:
                self.last_error = "worker could not start or continue; check service logs"
                logger.exception("automation worker failed; retrying with active configuration")
                await asyncio.sleep(1)

    async def _watch_configuration(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            if self._configuration_changed():
                raise ConfigurationChanged()

    def _configuration_changed(self) -> bool:
        return (
            self.store.config_generation() != self.config_generation
            or self.store.active_config(self.config) != self.config
        )

    async def _run_configuration(self) -> None:
        # Recovery needs the persisted network and bytes, never a signing key.
        while self.store.pending_execution() is not None:
            await self.recover_once()
            await asyncio.sleep(self.config.network.poll_interval_seconds)
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._watch_events())
            tasks.create_task(self._recovery_loop())

    async def _recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.network.poll_interval_seconds)
            await self.recover_once()

    async def recover_once(self) -> dict[str, Any] | None:
        store = self.store
        with store.execution_lock() as claimed:
            if not claimed:
                return {"status": "skipped", "reason": "execution_busy"}
            config = store.active_config(self.config)
            # A drain owns recovery policy (including disabling broadcasts).
            with store.configuration_lock(shared=True) as admitted:
                if not admitted:
                    return {"status": "skipped", "reason": "configuration_draining"}
            return await reconcile_execution(store, allow_broadcast=config.wallet.execute)

    async def _watch_events(self) -> None:
        cursor_name = f"{self.config.dex.pairs_contract}:Sync"
        cursor = self.store.get_cursor(cursor_name)
        async with AsyncExitStack() as clients:
            with self.store.configuration_lock(shared=True) as admitted:
                if not admitted or self._configuration_changed():
                    await asyncio.sleep(0.05)
                    raise ConfigurationChanged()
                dex = await clients.enter_async_context(DexClient(self.config))
                self.last_error = None
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
                for pair_id in self._pair_ids_from_event(event):
                    await self._evaluate_event_pair(dex, pair_id)
                if self._configuration_changed():
                    raise ConfigurationChanged()
                if event.id is not None:
                    self.store.save_cursor(cursor_name, int(event.id))
                    cursor = int(event.id)

    async def _evaluate_event_pair(self, dex: DexClient, pair_id: int) -> None:
        while True:
            results = await self.evaluate_pair(dex, pair_id)
            if any(result["reason"] == "configuration_changed" for result in results):
                raise ConfigurationChanged()
            if any(result["reason"] == "submission_pending" for result in results):
                # This event already created a durable submission. Reconcile it,
                # without evaluating the same event into another signed swap.
                await self._wait_for_execution()
                return
            if not any(_execution_blocked(result) for result in results):
                return
            await asyncio.sleep(self.config.network.poll_interval_seconds)

    async def _wait_for_execution(self) -> None:
        while True:
            await asyncio.sleep(self.config.network.poll_interval_seconds)
            result = await self.recover_once()
            if result is None or not _execution_blocked(result):
                return

    async def evaluate_pair_once(self, pair_id: int) -> list[dict[str, Any]]:
        recovered = await self.recover_once()
        if recovered is not None:
            return [recovered]
        evaluator = AutomationWorker(self.store.active_config(self.config), self.store)
        async with DexClient(evaluator.config) as dex:
            return await evaluator.evaluate_pair(dex, pair_id)

    async def evaluate_pair(
        self,
        dex: DexClient,
        pair_id: int,
    ) -> list[dict[str, Any]]:
        store = self.store
        with store.evaluation_lock() as blocked:
            if blocked is not None:
                return [{"status": "skipped", "reason": blocked, "pair_id": pair_id}]
            if self._configuration_changed():
                return [{"status": "skipped", "reason": "configuration_changed", "pair_id": pair_id}]
            # Bind this evaluation to one configuration and database even if
            # the admin API replaces the worker's settings while we await RPC.
            evaluator = AutomationWorker(self.config.model_copy(deep=True), store)
            return await evaluator._evaluate_pair_locked(dex, pair_id)

    async def _evaluate_pair_locked(self, dex: DexClient, pair_id: int) -> list[dict[str, Any]]:
        recovered = await reconcile_execution(
            self.store, allow_broadcast=self.config.wallet.execute
        )
        if recovered is not None and recovered["status"] == "pending":
            return [{**recovered, "reason": "recovery_pending"}]
        snapshot = await dex.get_pair_snapshot(pair_id)
        results: list[dict[str, Any]] = [recovered] if recovered is not None else []
        for rule in self._matching_rules(pair_id):
            result = await self._evaluate_rule(dex, rule, snapshot)
            results.append(result)
            if result["status"] == "pending":
                break
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
            "custody_mode": self.config.custody.mode,
            "strategy_contract": (
                self.config.custody.strategy_vault.contract
                if self.config.custody.strategy_vault is not None
                else None
            ),
        }

        if decision.next_baseline_price is not None and not decision.should_execute:
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

        amount_out_min = quote * ((BPS - Decimal(rule.action.max_slippage_bps)) / BPS)
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
            prepared = await dex.prepare_swap(
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
                "tx_hash": None,
                "details": details,
            }

        # A failed journal write propagates: nothing may broadcast beforehand.
        self.store.begin_execution(
            rule_id=rule.id,
            prepared=prepared,
            next_baseline_price=decision.next_baseline_price,
            reason=decision.reason,
            details=details,
        )
        return await reconcile_execution(self.store, allow_broadcast=True)

    def _record_action_state(self, rule: RuleConfig, decision: Any, now: datetime) -> None:
        self.store.save_rule_state(
            rule.id,
            RuleRuntimeState(baseline_price=decision.next_baseline_price, last_action_at=now),
        )

    def _matching_rules(self, pair_id: int) -> list[RuleConfig]:
        return [
            rule for rule in self.config.rules if rule.enabled and rule.trigger.pair_id == pair_id
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

    def _pair_ids_from_event(self, event: Any) -> list[int]:
        pair_id = self._pair_id_from_event(event)
        if pair_id is not None:
            return [pair_id]

        # Current con_pairs Sync events contain only reserve values. Evaluate
        # each configured pair so those events still drive deterministic rules.
        return sorted({rule.trigger.pair_id for rule in self.config.rules if rule.enabled})


async def sleep_forever() -> None:
    while True:
        await asyncio.sleep(3600)
