from __future__ import annotations

import decimal
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from xian_dex_automation.config import (
    AutomationConfig,
    load_config,
    normalize_config_paths,
)
from xian_dex_automation.dex import install_contracting_decimal_context


def test_load_example_config() -> None:
    path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = load_config(path)

    assert config.network.rpc_url == "http://127.0.0.1:26657"
    assert config.dex.router_contract == "con_dex"
    assert config.wallet.execute is False
    assert config.rules[0].action.amount_in == Decimal("1")
    assert config.database_path.is_absolute()


def test_normalize_config_paths_returns_normalized_copy(tmp_path: Path) -> None:
    config = AutomationConfig()
    config.wallet.private_key_file = Path("wallet.key")

    normalized = normalize_config_paths(
        config,
        config_path=tmp_path / "automation.yaml",
    )

    assert normalized is not config
    assert normalized.wallet is not config.wallet
    assert normalized.database_path == tmp_path / "state/xian-dex-automation.sqlite3"
    assert normalized.wallet.private_key_file == tmp_path / "wallet.key"
    assert config.database_path == Path("state/xian-dex-automation.sqlite3")
    assert config.wallet.private_key_file == Path("wallet.key")


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


def test_strategy_vault_config_accepts_rules_inside_on_chain_envelope() -> None:
    config = AutomationConfig.model_validate(
        {
            "custody": {
                "mode": "strategy_vault",
                "strategy_vault": {
                    "contract": "con_my_strategy",
                    "keeper_address": "a" * 64,
                    "pair_id": 1,
                    "src": "currency",
                    "token_out": "con_token",
                    "max_trade_size": "5",
                    "total_spend_cap": "100",
                    "max_slippage_bps": 100,
                    "cooldown_seconds": 300,
                    "max_deadline_seconds": 300,
                },
            },
            "rules": [
                {
                    "id": "bounded",
                    "trigger": {
                        "pair_id": 1,
                        "threshold_bps": 100,
                        "cooldown_seconds": 300,
                    },
                    "action": {
                        "src": "currency",
                        "amount_in": "5",
                        "max_slippage_bps": 100,
                        "deadline_seconds": 300,
                    },
                }
            ],
        }
    )

    assert config.custody.strategy_vault is not None
    assert config.custody.strategy_vault.total_spend_cap == Decimal("100")


def test_strategy_vault_config_rejects_rules_outside_on_chain_envelope() -> None:
    payload = {
        "custody": {
            "mode": "strategy_vault",
            "strategy_vault": {
                "contract": "con_my_strategy",
                "keeper_address": "a" * 64,
                "pair_id": 1,
                "src": "currency",
                "token_out": "con_token",
                "max_trade_size": "5",
                "total_spend_cap": "100",
                "max_slippage_bps": 100,
                "cooldown_seconds": 300,
                "max_deadline_seconds": 300,
            },
        },
        "rules": [
            {
                "id": "unbounded",
                "trigger": {
                    "pair_id": 2,
                    "threshold_bps": 100,
                    "cooldown_seconds": 299,
                },
                "action": {
                    "src": "con_other",
                    "amount_in": "6",
                    "max_slippage_bps": 101,
                    "deadline_seconds": 301,
                    "recipient": "attacker",
                },
            }
        ],
    }

    with pytest.raises(ValidationError):
        AutomationConfig.model_validate(payload)
