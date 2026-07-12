#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from contracting.compilation.artifacts import compile_contract_source
from xian_py import Wallet, Xian

from xian_dex_automation.config import (
    AutomationConfig,
    CustodyConfig,
    NetworkConfig,
    PriceMoveTriggerConfig,
    RuleConfig,
    StrategyVaultConfig,
    SwapExactInActionConfig,
    WalletConfig,
    save_config,
)
from xian_dex_automation.dex import BPS, ContractCallPlan, DexClient

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
DEFAULT_STACK_DIR = WORKSPACE_ROOT / "xian-stack"
DEFAULT_NETWORK_PATH = DEFAULT_STACK_DIR / ".localnet" / "network.json"
DEFAULT_CONTRACT_PATH = ROOT / "contracts" / "con_dex_strategy_vault.py"
DEFAULT_KEEPER_KEY_PATH = ROOT / "state" / "strategy-vault-keeper.key"
DEFAULT_CONFIG_PATH = ROOT / "state" / "strategy-vault.local.yaml"
KEEPER_MIN_CHI_HEADROOM = 250


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeeperExercisePlan:
    call: ContractCallPlan
    details: dict[str, Any]
    estimated_chi: int
    supplied_chi: int
    chi_cost: int
    required_currency: Decimal


def _decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def _read_network(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise BootstrapError(
            f"localnet metadata not found at {path}; start xian-stack localnet first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _rpc_url(network: dict[str, Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    nodes = network.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise BootstrapError("localnet metadata has no nodes")
    return f"http://127.0.0.1:{nodes[0]['host_rpc_port']}"


def _owner_key(network: dict[str, Any], explicit: str | None) -> str:
    value = explicit or os.environ.get("XIAN_DEX_VAULT_OWNER_PRIVATE_KEY")
    if value:
        return value.strip()
    founder = network.get("founder_key")
    if not isinstance(founder, str) or not founder:
        raise BootstrapError(
            "owner key unavailable; pass --owner-private-key or set "
            "XIAN_DEX_VAULT_OWNER_PRIVATE_KEY"
        )
    return founder


def _keeper_wallet(path: Path, *, execute: bool) -> tuple[Wallet, bool]:
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return Wallet(path.read_text(encoding="utf-8").strip()), False
    wallet = Wallet()
    if execute:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{wallet.private_key}\n", encoding="utf-8")
        path.chmod(0o600)
    return wallet, True


def _require_success(label: str, submission: Any) -> dict[str, Any]:
    receipt = getattr(submission, "receipt", None)
    if not getattr(submission, "submitted", False):
        raise BootstrapError(f"{label} was not submitted")
    if getattr(submission, "accepted", None) is False:
        raise BootstrapError(f"{label} was rejected: {submission.message}")
    if not getattr(submission, "finalized", False):
        raise BootstrapError(f"{label} was not finalized: {submission.message}")
    if receipt is None:
        raise BootstrapError(f"{label} finalized without a receipt")
    if not receipt.success:
        raise BootstrapError(f"{label} failed: {receipt.message}")
    return {
        "tx_hash": getattr(submission, "tx_hash", None),
        "submitted": bool(getattr(submission, "submitted", False)),
        "accepted": getattr(submission, "accepted", None),
        "finalized": bool(getattr(submission, "finalized", False)),
        "chi_supplied": getattr(submission, "chi_supplied", None),
        "chi_estimated": getattr(submission, "chi_estimated", None),
        "receipt": {
            "success": receipt.success,
            "tx_hash": getattr(receipt, "tx_hash", None),
            "chi_used": getattr(receipt, "chi_used", None),
            "message": receipt.message,
        },
    }


def _strategy_config(args: argparse.Namespace, keeper_address: str) -> StrategyVaultConfig:
    return StrategyVaultConfig(
        contract=args.contract_name,
        keeper_address=keeper_address,
        pair_id=args.pair_id,
        src=args.src,
        token_out=args.token_out,
        max_trade_size=args.max_trade_size,
        total_spend_cap=args.total_spend_cap,
        max_slippage_bps=args.max_slippage_bps,
        cooldown_seconds=args.cooldown_seconds,
        max_deadline_seconds=args.max_deadline_seconds,
    )


def _service_config(
    args: argparse.Namespace,
    *,
    rpc_url: str,
    chain_id: str | None,
    keeper_address: str,
) -> AutomationConfig:
    strategy = _strategy_config(args, keeper_address)
    return AutomationConfig(
        network=NetworkConfig(rpc_url=rpc_url, chain_id=chain_id),
        wallet=WalletConfig(
            private_key_file=args.keeper_private_key_file.resolve(),
            execute=False,
        ),
        custody=CustodyConfig(
            mode="strategy_vault",
            strategy_vault=strategy,
        ),
        database_path=ROOT / "state" / "strategy-vault.sqlite3",
        rules=[
            RuleConfig(
                id="local-strategy-vault",
                trigger=PriceMoveTriggerConfig(
                    pair_id=args.pair_id,
                    direction="either",
                    threshold_bps=100,
                    cooldown_seconds=args.cooldown_seconds,
                ),
                action=SwapExactInActionConfig(
                    src=args.src,
                    amount_in=args.exercise_amount,
                    max_slippage_bps=args.max_slippage_bps,
                    deadline_seconds=args.max_deadline_seconds,
                ),
            )
        ],
    )


def _assert_existing_strategy(existing: dict[str, Any], expected: StrategyVaultConfig) -> None:
    checks = {
        "keeper": expected.keeper_address,
        "pair": expected.pair_id,
        "src": expected.src,
        "token_out": expected.token_out,
        "max_trade_size": expected.max_trade_size,
        "total_spend_cap": expected.total_spend_cap,
        "max_slippage_bps": expected.max_slippage_bps,
        "cooldown_seconds": expected.cooldown_seconds,
        "max_deadline_seconds": expected.max_deadline_seconds,
    }
    for key, value in checks.items():
        actual = existing.get(key)
        if isinstance(value, Decimal):
            if Decimal(str(actual)) != value:
                raise BootstrapError(
                    f"existing vault {key} mismatch: expected {value}, got {actual}"
                )
        elif actual != value:
            raise BootstrapError(
                f"existing vault {key} mismatch: expected {value}, got {actual}"
            )


def _canonical_source_digest(*, module_name: str, source: str) -> str:
    canonical_source = compile_contract_source(
        module_name=module_name,
        source=source,
    )["source"]
    return hashlib.sha256(canonical_source.encode("utf-8")).hexdigest()


def _verify_reusable_source(
    *,
    module_name: str,
    existing_source: str,
    reviewed_source: str,
) -> None:
    existing_digest = _canonical_source_digest(
        module_name=module_name,
        source=existing_source,
    )
    reviewed_digest = _canonical_source_digest(
        module_name=module_name,
        source=reviewed_source,
    )
    if existing_digest != reviewed_digest:
        raise BootstrapError(
            "existing vault canonical source does not match the reviewed local "
            f"source (existing_sha256={existing_digest}, "
            f"reviewed_sha256={reviewed_digest})"
        )


def _deploy_vault(
    client: Any,
    args: argparse.Namespace,
    source: str,
    keeper_address: str,
) -> Any:
    return client.deploy_contract(
        args.contract_name,
        source,
        args={
            "keeper": keeper_address,
            "pair": args.pair_id,
            "src": args.src,
            "token_out": args.token_out,
            "max_trade_size": args.max_trade_size,
            "total_spend_cap": args.total_spend_cap,
            "max_slippage_bps": args.max_slippage_bps,
            "cooldown_seconds": args.cooldown_seconds,
            "max_deadline_seconds": args.max_deadline_seconds,
        },
        wait_for_tx=True,
    )


def _required_keeper_currency(*, supplied_chi: int, chi_cost: int) -> Decimal:
    if supplied_chi <= 0:
        raise BootstrapError("keeper chi estimate must be positive")
    if chi_cost <= 0:
        raise BootstrapError("chi_cost.current_value must be positive")
    return Decimal((supplied_chi + chi_cost - 1) // chi_cost)


async def _prepare_exercise(
    config: AutomationConfig,
) -> KeeperExercisePlan:
    runtime = config.model_copy(deep=True)
    runtime.wallet.execute = False
    action = runtime.rules[0].action
    async with DexClient(runtime) as dex:
        snapshot = await dex.get_pair_snapshot(runtime.rules[0].trigger.pair_id)
        quote = await dex.quote_exact_in(
            snapshot,
            src=action.src,
            amount_in=action.amount_in,
        )
        amount_out_min = quote * (
            (BPS - Decimal(action.max_slippage_bps)) / BPS
        )
        call = await dex.build_swap_exact_in_call(
            snapshot,
            action,
            amount_out_min=amount_out_min,
        )
        estimate = await dex.client.estimate_chi(
            call.contract,
            call.function,
            call.kwargs,
            min_chi_headroom=KEEPER_MIN_CHI_HEADROOM,
        )
        estimated_chi = int(estimate["estimated"])
        supplied_chi = int(estimate["suggested"])
        chi_cost = int(
            await dex.client.contract("chi_cost").call("current_value")
        )
        required_currency = _required_keeper_currency(
            supplied_chi=supplied_chi,
            chi_cost=chi_cost,
        )
        details: dict[str, Any] = {
            "mode": "dry_run",
            "pair_id": snapshot.pair_id,
            "amount_in": str(action.amount_in),
            "quoted_amount_out": str(quote),
            "amount_out_min": str(amount_out_min),
            "call": {
                "contract": call.contract,
                "function": call.function,
                "kwargs": call.kwargs,
            },
            "chi": {
                "estimated": estimated_chi,
                "headroom": KEEPER_MIN_CHI_HEADROOM,
                "supplied": supplied_chi,
                "chi_cost": chi_cost,
                "required_keeper_currency": str(required_currency),
            },
        }
        return KeeperExercisePlan(
            call=call,
            details=details,
            estimated_chi=estimated_chi,
            supplied_chi=supplied_chi,
            chi_cost=chi_cost,
            required_currency=required_currency,
        )


async def _exercise(
    config: AutomationConfig,
    plan: KeeperExercisePlan,
) -> dict[str, Any]:
    runtime = config.model_copy(deep=True)
    runtime.wallet.execute = True
    async with DexClient(runtime) as dex:
        submission = await dex.submit_call_plan(
            plan.call,
            chi=plan.supplied_chi,
        )
    result = dict(plan.details)
    result["mode"] = "execute"
    result["submission"] = _require_success(
        "keeper vault swap",
        submission,
    )
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute a localnet strategy-vault deployment. "
            "The default is read-only; pass --execute for chain/file writes."
        )
    )
    parser.add_argument("--network", type=Path, default=DEFAULT_NETWORK_PATH)
    parser.add_argument("--rpc-url")
    parser.add_argument("--owner-private-key")
    parser.add_argument(
        "--keeper-private-key-file",
        type=Path,
        default=DEFAULT_KEEPER_KEY_PATH,
    )
    parser.add_argument("--contract-name", default="con_dex_strategy_vault_demo")
    parser.add_argument("--pair-id", type=int, default=1)
    parser.add_argument("--src", default="currency")
    parser.add_argument("--token-out", default="con_dex_demo_token")
    parser.add_argument("--max-trade-size", type=_decimal, default=Decimal("1"))
    parser.add_argument("--total-spend-cap", type=_decimal, default=Decimal("10"))
    parser.add_argument("--deposit-amount", type=_decimal, default=Decimal("10"))
    parser.add_argument("--max-slippage-bps", type=int, default=100)
    parser.add_argument("--cooldown-seconds", type=int, default=0)
    parser.add_argument("--max-deadline-seconds", type=int, default=300)
    parser.add_argument("--exercise-amount", type=_decimal, default=Decimal("0.1"))
    parser.add_argument(
        "--keeper-gas-funding",
        type=_decimal,
        default=Decimal("100"),
        help=(
            "minimum keeper currency floor; actual target is raised to the "
            "ceil-divided live chi estimate plus explicit headroom"
        ),
    )
    parser.add_argument("--config-out", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="deploy/fund/unpause the vault, fund keeper gas, and write config",
    )
    parser.add_argument(
        "--execute-swap",
        action="store_true",
        help="also submit one keeper transaction through the service client",
    )
    parser.add_argument(
        "--unpause-existing",
        action="store_true",
        help="owner-authorize unpausing a reused vault after reviewing its state",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.execute_swap and not args.execute:
        parser.error("--execute-swap requires --execute")
    if args.exercise_amount > args.max_trade_size:
        parser.error("--exercise-amount must not exceed --max-trade-size")
    if args.deposit_amount < args.exercise_amount:
        parser.error("--deposit-amount must cover --exercise-amount")

    network = _read_network(args.network.expanduser().resolve())
    rpc_url = _rpc_url(network, args.rpc_url)
    chain_id = network.get("chain_id")
    owner_wallet = Wallet(_owner_key(network, args.owner_private_key))
    keeper_wallet, keeper_was_generated = _keeper_wallet(
        args.keeper_private_key_file.expanduser().resolve(),
        execute=args.execute,
    )
    strategy = _strategy_config(args, keeper_wallet.public_key)
    payload: dict[str, Any] = {
        "mode": "execute" if args.execute else "plan",
        "rpc_url": rpc_url,
        "chain_id": chain_id,
        "owner": owner_wallet.public_key,
        "keeper": keeper_wallet.public_key,
        "keeper_key_file": str(args.keeper_private_key_file.expanduser().resolve()),
        "keeper_key_will_be_generated": keeper_was_generated and not args.execute,
        "contract": args.contract_name,
        "strategy": strategy.model_dump(mode="json"),
        "deposit_amount": str(args.deposit_amount),
        "config_out": str(args.config_out.expanduser().resolve()),
        "writes": [],
    }
    if not args.execute:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    source = DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8")
    with Xian(
        rpc_url,
        chain_id=chain_id,
        wallet=owner_wallet,
    ) as owner_client:
        existing_source = owner_client.get_contract_source(args.contract_name)
        deployed_now = existing_source is None
        if existing_source is None:
            deployed = _deploy_vault(
                owner_client,
                args,
                source,
                keeper_wallet.public_key,
            )
            payload["writes"].append(
                {"deploy": _require_success("vault deployment", deployed)}
            )
        else:
            _verify_reusable_source(
                module_name=args.contract_name,
                existing_source=existing_source,
                reviewed_source=source,
            )
            existing = owner_client.contract(args.contract_name).call("get_strategy")
            _assert_existing_strategy(existing, strategy)
            payload["vault_reused"] = True

        vault_balance = Decimal(
            str(
                owner_client.contract(args.src).call(
                    "balance_of",
                    address=args.contract_name,
                )
                or 0
            )
        )
        if vault_balance < args.deposit_amount:
            deposit_delta = args.deposit_amount - vault_balance
            approved = owner_client.contract(args.src).send(
                "approve",
                amount=deposit_delta,
                to=args.contract_name,
                wait_for_tx=True,
            )
            payload["writes"].append(
                {"approve": _require_success("vault approval", approved)}
            )
            deposited = owner_client.contract(args.contract_name).send(
                "deposit",
                token=args.src,
                amount=deposit_delta,
                wait_for_tx=True,
            )
            payload["writes"].append(
                {"deposit": _require_success("vault deposit", deposited)}
            )

        status = owner_client.contract(args.contract_name).call("get_strategy")
        if status.get("paused") is not False:
            if deployed_now or args.unpause_existing:
                unpaused = owner_client.contract(args.contract_name).send(
                    "set_paused",
                    value=False,
                    wait_for_tx=True,
                )
                payload["writes"].append(
                    {"unpause": _require_success("vault unpause", unpaused)}
                )
            elif args.execute_swap:
                raise BootstrapError(
                    "reused vault is paused; review get_strategy() and pass "
                    "--unpause-existing to authorize owner unpause"
                )
            else:
                payload["vault_remains_paused"] = True

    service_config = _service_config(
        args,
        rpc_url=rpc_url,
        chain_id=chain_id,
        keeper_address=keeper_wallet.public_key,
    )
    config_out = args.config_out.expanduser().resolve()
    save_config(service_config, config_out)
    payload["writes"].append({"config": str(config_out)})
    exercise_plan = asyncio.run(_prepare_exercise(service_config))
    funding_target = max(
        args.keeper_gas_funding,
        exercise_plan.required_currency,
    )
    with Xian(rpc_url, chain_id=chain_id, wallet=owner_wallet) as owner_client:
        keeper_balance = Decimal(
            str(
                owner_client.contract("currency").call(
                    "balance_of",
                    address=keeper_wallet.public_key,
                )
                or 0
            )
        )
        top_up = max(Decimal(0), funding_target - keeper_balance)
        funding_details: dict[str, Any] = {
            "minimum_floor": str(args.keeper_gas_funding),
            "estimated_chi": exercise_plan.estimated_chi,
            "chi_headroom": KEEPER_MIN_CHI_HEADROOM,
            "supplied_chi": exercise_plan.supplied_chi,
            "chi_cost": exercise_plan.chi_cost,
            "required_from_estimate": str(
                exercise_plan.required_currency
            ),
            "target_balance": str(funding_target),
            "balance_before": str(keeper_balance),
            "top_up": str(top_up),
        }
        if top_up > 0:
            funded = owner_client.contract("currency").send(
                "transfer",
                amount=top_up,
                to=keeper_wallet.public_key,
                wait_for_tx=True,
            )
            funding_details["submission"] = _require_success(
                "keeper funding",
                funded,
            )
            payload["writes"].append(
                {"keeper_funding": funding_details["submission"]}
            )
    payload["keeper_funding"] = funding_details
    payload["dry_run"] = exercise_plan.details
    if args.execute_swap:
        payload["execution"] = asyncio.run(
            _exercise(service_config, exercise_plan)
        )

    with Xian(rpc_url, chain_id=chain_id, wallet=owner_wallet) as client:
        payload["on_chain_strategy"] = client.contract(args.contract_name).call(
            "get_strategy"
        )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
