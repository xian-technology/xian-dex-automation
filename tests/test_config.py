from __future__ import annotations

import decimal
from decimal import Decimal
from pathlib import Path

from xian_dex_automation.config import load_config
from xian_dex_automation.dex import install_contracting_decimal_context


def test_load_example_config() -> None:
    path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = load_config(path)

    assert config.network.rpc_url == "http://127.0.0.1:26657"
    assert config.dex.router_contract == "con_dex"
    assert config.wallet.execute is False
    assert config.rules[0].action.amount_in == Decimal("1")
    assert config.database_path.is_absolute()


def test_install_contracting_decimal_context_for_async_tasks() -> None:
    from xian_runtime_types.decimal import ContractingDecimal

    previous = decimal.getcontext().copy()
    try:
        decimal.setcontext(decimal.Context(prec=28))

        install_contracting_decimal_context()

        assert decimal.getcontext().prec > 28
        assert str(ContractingDecimal("9980.099711")) == "9980.099711"
    finally:
        decimal.setcontext(previous)
