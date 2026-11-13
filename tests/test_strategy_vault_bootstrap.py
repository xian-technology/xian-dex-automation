from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from xian_py import Wallet
from xian_py.xian_async import XianAsync

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "bootstrap_strategy_vault.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_strategy_vault", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


@pytest.mark.parametrize("execute", [False, True])
@pytest.mark.parametrize("contents", ["", " \n\t"])
def test_existing_empty_keeper_file_is_not_replaced(tmp_path, execute, contents):
    path = tmp_path / "keeper.key"
    path.write_text(contents)
    with pytest.raises(bootstrap.BootstrapError, match="empty.*restore"):
        bootstrap._keeper_wallet(path, execute=execute)
    assert path.read_text() == contents


def test_existing_keeper_key_is_preserved(tmp_path):
    expected = Wallet()
    path = tmp_path / "keeper.key"
    contents = expected.private_key + "\n"
    path.write_text(contents)
    wallet, generated = bootstrap._keeper_wallet(path, execute=True)
    assert wallet.public_key == expected.public_key
    assert not generated
    assert path.read_text() == contents


def test_default_bootstrap_is_a_read_only_plan(tmp_path, capsys) -> None:
    owner = Wallet()
    network_path = tmp_path / "network.json"
    network_path.write_text(
        json.dumps(
            {
                "chain_id": "xian-localnet-1",
                "founder_key": owner.private_key,
                "nodes": [{"host_rpc_port": 27657}],
            }
        ),
        encoding="utf-8",
    )
    keeper_path = tmp_path / "keeper.key"
    config_path = tmp_path / "config.yaml"

    assert (
        bootstrap.main(
            [
                "--network",
                str(network_path),
                "--keeper-private-key-file",
                str(keeper_path),
                "--config-out",
                str(config_path),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "plan"
    assert payload["writes"] == []
    assert payload["keeper_key_will_be_generated"] is True
    assert not keeper_path.exists()
    assert not config_path.exists()
    assert owner.private_key not in json.dumps(payload)


def test_vault_deployment_uses_automatic_chi_estimation() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.call: tuple[tuple, dict] | None = None

        def deploy_contract(self, *args, **kwargs):
            self.call = (args, kwargs)
            return object()

    args = bootstrap._build_parser().parse_args([])
    client = FakeClient()

    bootstrap._deploy_vault(client, args, "source", "a" * 64)

    assert client.call is not None
    positional, keyword = client.call
    assert positional == (args.contract_name, "source")
    assert "chi" not in keyword
    assert "deployment_artifacts" not in keyword
    assert keyword["wait_for_tx"] is True
    assert keyword["args"]["keeper"] == "a" * 64


def test_current_sdk_builds_source_backed_deployment_transaction() -> None:
    async def exercise() -> tuple[tuple, dict]:
        client = XianAsync("http://127.0.0.1:26657")
        client.send_tx = AsyncMock(return_value=object())
        await client.deploy_contract(
            "con_strategy",
            "contract source",
            args={"keeper": "a" * 64},
            wait_for_tx=True,
        )
        assert client.send_tx.await_args is not None
        return client.send_tx.await_args.args, client.send_tx.await_args.kwargs

    positional, keyword = asyncio.run(exercise())

    assert positional[0:2] == ("submission", "submit_contract")
    submission_payload = positional[2]
    assert submission_payload["name"] == "con_strategy"
    assert submission_payload["code"] == "contract source"
    assert submission_payload["constructor_args"] == {"keeper": "a" * 64}
    assert "deployment_artifacts" not in submission_payload
    assert keyword["chi"] is None


def test_keeper_gas_default_covers_standard_localnet_chi() -> None:
    args = bootstrap._build_parser().parse_args([])

    assert args.keeper_gas_funding == 100
    assert args.keeper_gas_funding * 20 >= 2_000


def test_dynamic_keeper_funding_covers_exactly_the_observed_estimate() -> None:
    estimated = 2_585

    required = bootstrap._required_keeper_currency(chi=estimated, chi_cost=20)

    assert required == 130
    assert required * 20 >= estimated
    assert (required - 1) * 20 < estimated


@pytest.mark.parametrize(
    ("chi_cost", "expected"),
    [(7, Decimal("370")), (20, Decimal("130")), (30, Decimal("87"))],
)
def test_dynamic_keeper_funding_tracks_changing_chi_costs(
    chi_cost: int,
    expected: Decimal,
) -> None:
    assert bootstrap._required_keeper_currency(chi=2_585, chi_cost=chi_cost) == expected


class _FakeDex:
    def __init__(self, estimate_calls: list) -> None:
        async def estimate_chi(contract, function, kwargs, **options):
            estimate_calls.append(options)
            return {"estimated": 2_585, "simulation": {"chi_used": 2_585}}

        chi_cost = SimpleNamespace(call=AsyncMock(return_value=20))
        self.client = SimpleNamespace(
            estimate_chi=estimate_chi,
            contract=lambda name: chi_cost,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_pair_snapshot(self, pair_id):
        return SimpleNamespace(pair_id=pair_id)

    async def quote_exact_in(self, snapshot, *, src, amount_in):
        return Decimal("4")

    async def build_swap_exact_in_call(self, snapshot, action, *, amount_out_min):
        return bootstrap.ContractCallPlan(
            "con_vault", "execute_swap", {"min": amount_out_min}
        )


def test_keeper_exercise_plans_exactly_the_simulated_chi(monkeypatch, tmp_path) -> None:
    estimate_calls: list = []
    monkeypatch.setattr(bootstrap, "DexClient", lambda config: _FakeDex(estimate_calls))
    args = bootstrap._build_parser().parse_args(
        ["--keeper-private-key-file", str(tmp_path / "keeper.key")]
    )
    config = bootstrap._service_config(
        args,
        rpc_url="http://127.0.0.1:26657",
        chain_id="xian-local",
        keeper_address="b" * 64,
    )

    plan = asyncio.run(bootstrap._prepare_exercise(config))

    assert estimate_calls == [{}]
    assert (plan.estimated_chi, plan.chi_cost, plan.required_currency) == (2_585, 20, 130)
    assert plan.details["chi"] == {
        "estimated": 2_585,
        "chi_cost": 20,
        "required_keeper_currency": "130",
    }


def test_rejected_keeper_submission_is_never_reported_as_success() -> None:
    rejected = SimpleNamespace(
        submitted=True,
        accepted=False,
        finalized=False,
        tx_hash="REJECTED_HASH",
        message="Transaction sender has too few chi",
        receipt=None,
    )

    with pytest.raises(bootstrap.BootstrapError, match="too few chi"):
        bootstrap._require_success("keeper vault swap", rejected)


def test_success_payload_includes_acceptance_finality_and_receipt() -> None:
    receipt = SimpleNamespace(
        success=True,
        tx_hash="TX123",
        chi_used=76,
        message="ok",
    )
    submission = SimpleNamespace(
        submitted=True,
        accepted=True,
        finalized=True,
        tx_hash="TX123",
        chi_supplied=80,
        chi_estimated=76,
        message="ok",
        receipt=receipt,
    )

    payload = bootstrap._require_success("keeper vault swap", submission)

    assert payload["accepted"] is True
    assert payload["finalized"] is True
    assert payload["receipt"] == {
        "success": True,
        "tx_hash": "TX123",
        "chi_used": 76,
        "message": "ok",
    }


def test_reuse_guard_accepts_formatting_only_source_differences() -> None:
    reviewed = """
value = Variable()

@export
def read():
    return value.get()
"""
    node_canonical_source = bootstrap.compile_contract_source(
        module_name="con_strategy",
        source="value=Variable()\n@export\ndef read():\n return value.get()\n",
    )["source"]

    bootstrap._verify_reusable_source(
        module_name="con_strategy",
        existing_source=node_canonical_source,
        reviewed_source=reviewed,
    )


def test_reuse_guard_rejects_semantic_source_differences() -> None:
    reviewed = """
@export
def read():
    return 1
"""
    existing = """
@export
def read():
    return 2
"""

    with pytest.raises(
        bootstrap.BootstrapError,
        match=r"existing_sha256=[0-9a-f]{64}.*reviewed_sha256=[0-9a-f]{64}",
    ):
        bootstrap._verify_reusable_source(
            module_name="con_strategy",
            existing_source=existing,
            reviewed_source=reviewed,
        )
