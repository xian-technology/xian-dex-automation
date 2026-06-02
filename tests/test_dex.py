from __future__ import annotations

import asyncio
from decimal import Decimal

from xian_dex_automation.config import AutomationConfig, SwapExactInActionConfig
from xian_dex_automation.dex import DexClient, PairSnapshot


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
