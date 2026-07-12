from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from contracting.local import ContractingClient
from xian_runtime_types.time import Datetime

ROOT = Path(__file__).resolve().parents[1]
VAULT_PATH = ROOT / "contracts" / "con_dex_strategy_vault.py"

TOKEN_CODE = """
balances = Hash(default_value=0)
approvals = Hash(default_value=0)

@construct
def seed():
    balances[ctx.caller] = 1000000

@export
def balance_of(address: str):
    return balances[address]

@export
def transfer(amount: float, to: str):
    assert amount > 0
    assert balances[ctx.caller] >= amount
    balances[ctx.caller] -= amount
    balances[to] += amount

@export
def approve(amount: float, to: str):
    assert amount >= 0
    approvals[ctx.caller, to] = amount

@export
def transfer_from(amount: float, to: str, main_account: str):
    assert amount > 0
    assert approvals[main_account, ctx.caller] >= amount
    assert balances[main_account] >= amount
    approvals[main_account, ctx.caller] -= amount
    balances[main_account] -= amount
    balances[to] += amount

@export
def mint(amount: float, to: str):
    balances[to] += amount
"""

PAIRS_CODE = """
pairs = Hash()

@construct
def seed():
    pairs[1, "token0"] = "currency"
    pairs[1, "token1"] = "con_output"

@export
def get_pair_tokens(pair: int):
    return pairs[pair, "token0"], pairs[pair, "token1"]
"""

DEX_CODE = """
@export
def getAmountsOut(amountIn: float, src: str, path: list):
    assert src == "currency"
    assert path == [1]
    return [amountIn, amountIn * 2]

@export
def swapExactTokenForToken(
    amountIn: float,
    amountOutMin: float,
    pair: int,
    src: str,
    to: str,
    deadline: datetime.datetime,
):
    assert now < deadline
    assert pair == 1
    assert src == "currency"
    output = amountIn * 2
    assert output >= amountOutMin
    importlib.import_module(src).transfer_from(amountIn, ctx.this, ctx.caller)
    importlib.import_module("con_output").mint(output, to)
    return output
"""


class TestStrategyVaultContract(unittest.TestCase):
    def setUp(self) -> None:
        self._storage_home = tempfile.TemporaryDirectory()
        self.client = ContractingClient(
            storage_home=Path(self._storage_home.name)
        )
        self.client.flush()
        self.client.submit(TOKEN_CODE, name="currency")
        self.client.submit(TOKEN_CODE, name="con_output")
        self.client.submit(PAIRS_CODE, name="con_pairs")
        self.client.submit(DEX_CODE, name="con_dex")
        self.owner = "sys"
        self.keeper = "a" * 64
        with VAULT_PATH.open() as contract_file:
            self.client.submit(
                contract_file.read(),
                name="con_strategy",
                constructor_args={
                    "keeper": self.keeper,
                    "pair": 1,
                    "src": "currency",
                    "token_out": "con_output",
                    "max_trade_size": 100,
                    "total_spend_cap": 150,
                    "max_slippage_bps": 100,
                    "cooldown_seconds": 300,
                    "max_deadline_seconds": 300,
                },
            )
        self.currency = self.client.get_contract_proxy("currency")
        self.output = self.client.get_contract_proxy("con_output")
        self.vault = self.client.get_contract_proxy("con_strategy")
        self.currency.approve(amount=1000, to="con_strategy", signer=self.owner)
        self.vault.deposit(token="currency", amount=1000, signer=self.owner)
        self.vault.set_paused(value=False, signer=self.owner)
        self.now = Datetime(2026, 1, 1, 12, 0, 0)

    def tearDown(self) -> None:
        try:
            self.client.flush()
        finally:
            self.client.raw_driver._store.close()
            self._storage_home.cleanup()

    def execute(self, *, amount_in=100, amount_out_min=198, minute=0, deadline_minute=4):
        return self.vault.execute_swap(
            amount_in=amount_in,
            amount_out_min=amount_out_min,
            deadline=Datetime(2026, 1, 1, 12, deadline_minute, 0),
            signer=self.keeper,
            environment={"now": Datetime(2026, 1, 1, 12, minute, 0)},
        )

    def test_keeper_executes_with_output_retained_and_budget_recorded(self) -> None:
        output = self.execute()

        self.assertEqual(output, 200)
        self.assertEqual(
            self.output.balance_of(address="con_strategy", signer=self.owner),
            200,
        )
        strategy = self.vault.get_strategy(signer=self.owner)
        self.assertEqual(strategy["spent_total"], 100)
        self.assertEqual(strategy["action"], "swap_exact_in")
        self.assertFalse(strategy["paused"])

    def test_keeper_authority_pause_and_owner_only_withdrawal(self) -> None:
        with self.assertRaises(AssertionError):
            self.vault.execute_swap(
                amount_in=10,
                amount_out_min=19.8,
                deadline=Datetime(2026, 1, 1, 12, 4, 0),
                signer="attacker",
                environment={"now": self.now},
            )
        with self.assertRaises(AssertionError):
            self.vault.withdraw(token="currency", amount=1, signer=self.keeper)

        self.vault.set_paused(value=True, signer=self.keeper)
        with self.assertRaises(AssertionError):
            self.vault.set_paused(value=False, signer=self.keeper)
        self.vault.withdraw(token="currency", amount=25, signer=self.owner)
        self.assertEqual(
            self.currency.balance_of(address=self.owner, signer=self.owner),
            999025,
        )

    def test_slippage_trade_deadline_cooldown_and_total_caps_are_enforced(self) -> None:
        with self.assertRaises(AssertionError):
            self.execute(amount_out_min=197)
        with self.assertRaises(AssertionError):
            self.execute(amount_in=101, amount_out_min=200)
        with self.assertRaises(AssertionError):
            self.execute(deadline_minute=6)

        self.execute()
        with self.assertRaises(AssertionError):
            self.execute(amount_in=10, amount_out_min=19.8, minute=1)
        with self.assertRaises(AssertionError):
            self.execute(
                amount_in=51,
                amount_out_min=100.98,
                minute=5,
                deadline_minute=9,
            )

    def test_limits_can_only_tighten_and_keeper_change_pauses(self) -> None:
        with self.assertRaises(AssertionError):
            self.vault.tighten_limits(
                max_trade_size=101,
                total_spend_cap=150,
                max_slippage_bps=100,
                cooldown_seconds=300,
                max_deadline_seconds=300,
                signer=self.owner,
            )

        strategy = self.vault.tighten_limits(
            max_trade_size=50,
            total_spend_cap=100,
            max_slippage_bps=50,
            cooldown_seconds=600,
            max_deadline_seconds=120,
            signer=self.owner,
        )
        self.assertEqual(strategy["max_trade_size"], 50)
        self.assertTrue(strategy["paused"])

        new_keeper = "b" * 64
        self.vault.set_keeper(keeper=new_keeper, signer=self.owner)
        self.assertEqual(
            self.vault.get_strategy(signer=self.owner)["keeper"],
            new_keeper,
        )
        self.assertTrue(self.vault.get_strategy(signer=self.owner)["paused"])

    def test_constructor_rejects_tokens_outside_the_allowed_pair(self) -> None:
        with VAULT_PATH.open() as contract_file:
            with self.assertRaises(AssertionError):
                self.client.submit(
                    contract_file.read(),
                    name="con_invalid_strategy",
                    constructor_args={
                        "keeper": self.keeper,
                        "pair": 1,
                        "src": "currency",
                        "token_out": "con_not_in_pair",
                        "max_trade_size": 1,
                        "total_spend_cap": 10,
                    },
                )


if __name__ == "__main__":
    unittest.main()
