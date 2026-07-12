from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from xian_py.wallet import Wallet

from xian_dex_automation.config import (
    AutomationConfig,
    CustodyConfig,
    StrategyVaultConfig,
    SwapExactInActionConfig,
)
from xian_dex_automation.dex import (
    DEADLINE_SAFETY_MARGIN_SECONDS,
    DexAutomationError,
    DexClient,
    PairSnapshot,
    xian_deadline,
)


class FakeContract:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, function: str, **kwargs):
        self.calls.append({"function": function, "kwargs": kwargs})
        return object()


class FakeClient:
    def __init__(self) -> None:
        self.contracts: dict[str, FakeContract] = {}

    def contract(self, name: str) -> FakeContract:
        contract = self.contracts.get(name)
        if contract is None:
            contract = FakeContract()
            self.contracts[name] = contract
        return contract

    async def get_node_status(self):
        return SimpleNamespace(
            latest_block_time_iso="2026-07-12T12:00:00Z"
        )


def test_swap_exact_in_preserves_decimal_payloads() -> None:
    fake_client = FakeClient()
    dex = DexClient(AutomationConfig())
    dex._client = fake_client
    dex.wallet_address = "automation-wallet"
    snapshot = PairSnapshot(
        pair_id=1,
        token0="con_token",
        token1="currency",
        reserve0=Decimal("1000"),
        reserve1=Decimal("1000"),
    )
    action = SwapExactInActionConfig(
        src="currency",
        amount_in=Decimal("5.25"),
        max_slippage_bps=100,
        deadline_seconds=300,
    )

    asyncio.run(
        dex.swap_exact_in(
            snapshot,
            action,
            amount_out_min=Decimal("4.123456789"),
        )
    )

    call = fake_client.contracts["con_dex"].calls[0]
    assert call["function"] == "swapExactTokenForToken"
    assert call["kwargs"]["amountIn"] == Decimal("5.25")
    assert call["kwargs"]["amountOutMin"] == Decimal("4.123456789")
    assert call["kwargs"]["deadline"] == {
        "__time__": [2026, 7, 12, 12, 4, 55, 0]
    }


def test_strategy_vault_swap_calls_only_the_constrained_entrypoint() -> None:
    fake_client = FakeClient()
    config = AutomationConfig(
        custody=CustodyConfig(
            mode="strategy_vault",
            strategy_vault=StrategyVaultConfig(
                contract="con_my_strategy",
                keeper_address="a" * 64,
                pair_id=1,
                src="currency",
                token_out="con_token",
                max_trade_size=Decimal("5"),
                total_spend_cap=Decimal("100"),
            ),
        )
    )
    dex = DexClient(config)
    dex._client = fake_client
    dex.wallet_address = "a" * 64
    snapshot = PairSnapshot(
        pair_id=1,
        token0="con_token",
        token1="currency",
        reserve0=Decimal("1000"),
        reserve1=Decimal("1000"),
    )
    action = SwapExactInActionConfig(
        src="currency",
        amount_in=Decimal("5"),
        max_slippage_bps=100,
        deadline_seconds=300,
    )

    asyncio.run(
        dex.swap_exact_in(
            snapshot,
            action,
            amount_out_min=Decimal("4.1"),
        )
    )

    call = fake_client.contracts["con_my_strategy"].calls[0]
    assert call["function"] == "execute_swap"
    assert call["kwargs"]["amount_in"] == Decimal("5")
    assert call["kwargs"]["amount_out_min"] == Decimal("4.1")
    assert "recipient" not in call["kwargs"]
    assert "con_dex" not in fake_client.contracts
    assert call["kwargs"]["deadline"] == {
        "__time__": [2026, 7, 12, 12, 4, 55, 0]
    }


def test_strategy_vault_execution_requires_the_configured_keeper_key(
    monkeypatch,
) -> None:
    wallet = Wallet()
    monkeypatch.setenv("TEST_STRATEGY_KEEPER_KEY", wallet.private_key)
    config = AutomationConfig(
        wallet={
            "private_key_env": "TEST_STRATEGY_KEEPER_KEY",
            "execute": True,
        },
        custody=CustodyConfig(
            mode="strategy_vault",
            strategy_vault=StrategyVaultConfig(
                contract="con_my_strategy",
                keeper_address="b" * 64,
                pair_id=1,
                src="currency",
                token_out="con_token",
                max_trade_size=Decimal("5"),
                total_spend_cap=Decimal("100"),
            ),
        ),
    )

    with pytest.raises(DexAutomationError, match="does not match"):
        asyncio.run(DexClient(config).__aenter__())


def test_exact_cap_deadline_uses_chain_time_and_explicit_margin() -> None:
    chain_time = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)

    deadline = xian_deadline(300, base_time=chain_time)

    assert DEADLINE_SAFETY_MARGIN_SECONDS == 5
    assert deadline == {"__time__": [2026, 7, 12, 12, 4, 55, 0]}


def test_short_deadline_clamps_margin_and_remains_after_chain_time() -> None:
    chain_time = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)

    deadline = xian_deadline(3, base_time=chain_time)

    assert deadline == {"__time__": [2026, 7, 12, 12, 0, 1, 0]}
