from __future__ import annotations

import decimal as decimal_module
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from .config import AutomationConfig, SwapExactInActionConfig

BPS = Decimal("10000")
DEADLINE_SAFETY_MARGIN_SECONDS = 5

logger = logging.getLogger(__name__)


class DexAutomationError(RuntimeError):
    pass


def install_contracting_decimal_context() -> None:
    from xian_runtime_types.decimal import CONTEXT

    decimal_module.setcontext(CONTEXT.copy())


def _resolve_private_key(
    config: AutomationConfig,
) -> tuple[str | None, str | None]:
    env_value = os.environ.get(config.wallet.private_key_env)
    if env_value:
        return env_value.strip(), config.wallet.private_key_env

    file_env_value = os.environ.get(config.wallet.private_key_file_env)
    key_file = file_env_value or config.wallet.private_key_file
    if key_file is None:
        return None, None

    key_path = key_file if isinstance(key_file, str) else str(key_file)
    try:
        with open(key_path, encoding="utf-8") as key_file_obj:
            value = key_file_obj.read().strip()
    except OSError as exc:
        raise DexAutomationError(
            f"unable to read automation wallet key file: {key_path}"
        ) from exc
    return value or None, key_path


def resolve_private_key(config: AutomationConfig) -> str | None:
    value, _source = _resolve_private_key(config)
    return value


def resolve_private_key_source(config: AutomationConfig) -> str | None:
    _value, source = _resolve_private_key(config)
    return source


@dataclass(frozen=True)
class PairSnapshot:
    pair_id: int
    token0: str
    token1: str
    reserve0: Decimal
    reserve1: Decimal

    @property
    def price_token1_per_token0(self) -> Decimal | None:
        if self.reserve0 <= 0 or self.reserve1 <= 0:
            return None
        return self.reserve1 / self.reserve0

    def reserves_for_src(self, src: str) -> tuple[Decimal, Decimal, str]:
        if src == self.token0:
            return self.reserve0, self.reserve1, self.token1
        if src == self.token1:
            return self.reserve1, self.reserve0, self.token0
        raise DexAutomationError(
            f"source token {src!r} is not in pair {self.pair_id}"
        )


@dataclass(frozen=True)
class ContractCallPlan:
    contract: str
    function: str
    kwargs: dict[str, Any]


def decimal_value(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise DexAutomationError(
            f"expected numeric value, got {value!r}"
        ) from exc


def _parse_node_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def xian_deadline(
    seconds_from_now: int,
    *,
    base_time: datetime | None = None,
) -> dict[str, list[int]]:
    if seconds_from_now <= 0:
        raise DexAutomationError("deadline seconds must be positive")
    margin = min(
        DEADLINE_SAFETY_MARGIN_SECONDS,
        max(0, seconds_from_now - 1),
    )
    effective_seconds = seconds_from_now - margin
    future = (base_time or datetime.now(UTC)) + timedelta(
        seconds=effective_seconds
    )
    return {
        "__time__": [
            future.year,
            future.month,
            future.day,
            future.hour,
            future.minute,
            future.second,
            future.microsecond,
        ]
    }


class DexClient:
    def __init__(self, config: AutomationConfig):
        self.config = config
        self._client: Any | None = None
        self.wallet_address: str | None = None

    async def __aenter__(self) -> DexClient:
        install_contracting_decimal_context()

        from xian_py import (
            RetryPolicy,
            WatcherConfig,
            XianAsync,
            XianClientConfig,
        )
        from xian_py.wallet import Wallet

        private_key = resolve_private_key(self.config)
        if self.config.wallet.execute and not private_key:
            raise DexAutomationError(
                "wallet.execute is true but "
                f"{self.config.wallet.private_key_env} is not set"
            )

        wallet = Wallet(private_key) if private_key else Wallet()
        self.wallet_address = wallet.public_key if private_key else None
        strategy = self.config.custody.strategy_vault
        if (
            strategy is not None
            and private_key
            and self.config.wallet.execute
            and self.wallet_address != strategy.keeper_address
        ):
            raise DexAutomationError(
                "configured automation wallet does not match "
                "custody.strategy_vault.keeper_address"
            )
        client_config = XianClientConfig(
            retry=RetryPolicy(max_attempts=3, initial_delay_seconds=0.25),
            watcher=WatcherConfig(
                mode=self.config.network.watcher_mode,
                poll_interval_seconds=(
                    self.config.network.poll_interval_seconds
                ),
                batch_limit=100,
            ),
        )
        self._client = XianAsync(
            self.config.network.rpc_url,
            chain_id=self.config.network.chain_id,
            wallet=wallet,
            config=client_config,
        )
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: object, exc: object, tb: object
    ) -> None:
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)
        self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise DexAutomationError("DEX client is not connected")
        return self._client

    async def get_pair_snapshot(self, pair_id: int) -> PairSnapshot:
        pairs = self.config.dex.pairs_contract
        token0 = await self.client.get_state(pairs, "pairs", pair_id, "token0")
        token1 = await self.client.get_state(pairs, "pairs", pair_id, "token1")
        reserve0 = await self.client.get_state(
            pairs, "pairs", pair_id, "reserve0"
        )
        reserve1 = await self.client.get_state(
            pairs, "pairs", pair_id, "reserve1"
        )
        if not token0 or not token1:
            raise DexAutomationError(f"pair {pair_id} does not exist")
        return PairSnapshot(
            pair_id=pair_id,
            token0=str(token0),
            token1=str(token1),
            reserve0=decimal_value(reserve0),
            reserve1=decimal_value(reserve1),
        )

    async def trade_fee_bps(self) -> int:
        account = self.wallet_address
        value = await self.client.contract(
            self.config.dex.router_contract
        ).call("getTradeFeeBps", account=account)
        return int(value)

    async def quote_exact_in(
        self,
        snapshot: PairSnapshot,
        *,
        src: str,
        amount_in: Decimal,
    ) -> Decimal:
        reserve_in, reserve_out, _token_out = snapshot.reserves_for_src(src)
        fee_bps = Decimal(await self.trade_fee_bps())
        if reserve_in <= 0 or reserve_out <= 0:
            raise DexAutomationError("pair has insufficient liquidity")
        amount_in_with_fee = amount_in * ((BPS - fee_bps) / BPS)
        return (amount_in_with_fee * reserve_out) / (
            reserve_in + amount_in_with_fee
        )

    async def transaction_deadline(
        self,
        seconds_from_now: int,
    ) -> dict[str, list[int]]:
        base_time: datetime | None = None
        try:
            status = await self.client.get_node_status()
            base_time = _parse_node_time(
                getattr(status, "latest_block_time_iso", None)
            )
        except Exception as exc:
            logger.debug(
                "unable to read latest block time; using wall-clock deadline base: %s",
                exc,
            )
        return xian_deadline(seconds_from_now, base_time=base_time)

    async def swap_exact_in(
        self,
        snapshot: PairSnapshot,
        action: SwapExactInActionConfig,
        *,
        amount_out_min: Decimal,
        chi: int | None = None,
    ) -> Any:
        plan = await self.build_swap_exact_in_call(
            snapshot,
            action,
            amount_out_min=amount_out_min,
        )
        return await self.submit_call_plan(plan, chi=chi)

    async def build_swap_exact_in_call(
        self,
        snapshot: PairSnapshot,
        action: SwapExactInActionConfig,
        *,
        amount_out_min: Decimal,
    ) -> ContractCallPlan:
        strategy = self.config.custody.strategy_vault
        if self.config.custody.mode == "strategy_vault":
            if strategy is None:
                raise DexAutomationError("strategy vault is not configured")
            _reserve_in, _reserve_out, token_out = snapshot.reserves_for_src(
                action.src
            )
            if snapshot.pair_id != strategy.pair_id:
                raise DexAutomationError(
                    "swap pair does not match the configured strategy vault"
                )
            if action.src != strategy.src or token_out != strategy.token_out:
                raise DexAutomationError(
                    "swap direction does not match the configured strategy vault"
                )
            return ContractCallPlan(
                contract=strategy.contract,
                function="execute_swap",
                kwargs={
                    "amount_in": action.amount_in,
                    "amount_out_min": amount_out_min,
                    "deadline": await self.transaction_deadline(
                        action.deadline_seconds
                    ),
                },
            )

        recipient = (
            action.recipient
            or self.config.wallet.recipient
            or self.wallet_address
        )
        if recipient is None:
            raise DexAutomationError("swap recipient is not configured")

        return ContractCallPlan(
            contract=self.config.dex.router_contract,
            function="swapExactTokenForToken",
            kwargs={
                "amountIn": action.amount_in,
                "amountOutMin": amount_out_min,
                "pair": snapshot.pair_id,
                "src": action.src,
                "to": recipient,
                "deadline": await self.transaction_deadline(
                    action.deadline_seconds
                ),
            },
        )

    async def submit_call_plan(
        self,
        plan: ContractCallPlan,
        *,
        chi: int | None = None,
    ) -> Any:
        return await self.client.contract(plan.contract).send(
            plan.function,
            **plan.kwargs,
            chi=chi,
            wait_for_tx=True,
        )
