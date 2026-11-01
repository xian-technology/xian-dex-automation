#!/usr/bin/env python3
"""Exercise automation recovery against the stack's disposable five-node chain.

The stack supplies funded test wallets and deployed DEX contracts in a temporary
0600 JSON file. Never point this harness at a production wallet or database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from xian_py import XianAsync
from xian_py import transaction as tr
from xian_py.wallet import Wallet

from xian_dex_automation.config import AutomationConfig, render_config, save_config
from xian_dex_automation.configuration import ConfigurationManager
from xian_dex_automation.dex import DexClient
from xian_dex_automation.rules import RuleRuntimeState
from xian_dex_automation.service import create_app
from xian_dex_automation.storage import AutomationStore
from xian_dex_automation.submission import SwapTransport
from xian_dex_automation.worker import AutomationWorker

CRASH_EXIT = 73


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def require_submitted(run, message):
    if run.get("status") == "submitted":
        return
    # Report the outcome, never the journal's signed transaction or other details.
    details = run.get("details") or {}
    fields = {
        "status": run.get("status"),
        "reason": run.get("reason"),
        "error": details.get("error") or details.get("recovery_error"),
    }
    summary = {
        key: re.sub(r"[0-9a-fA-F]{64,}", "<redacted-hex>", value)[:300]
        for key, value in fields.items()
        if isinstance(value, str)
    }
    raise RuntimeError(f"{message}: {json.dumps(summary, sort_keys=True)}")


def make_config(settings):
    os.environ["XIAN_DEX_RECOVERY_SMOKE_KEY"] = settings["private_key"]
    return AutomationConfig.model_validate(
        {
            "network": {
                "rpc_url": settings["rpc_urls"][0],
                "chain_id": settings["chain_id"],
                "poll_interval_seconds": 0.1,
            },
            "dex": {"router_contract": settings["router"], "pairs_contract": settings["pairs"]},
            "wallet": {
                "execute": True,
                "private_key_env": "XIAN_DEX_RECOVERY_SMOKE_KEY",
                "private_key_file_env": "XIAN_DEX_RECOVERY_UNUSED_FILE",
            },
            "database_path": str(Path(settings["output_dir"]) / "execution.sqlite3"),
            "rules": [
                {
                    "id": "recovery-smoke",
                    "trigger": {
                        "pair_id": settings["pair_id"],
                        "threshold_bps": 1,
                        "cooldown_seconds": 300,
                    },
                    "action": {
                        "src": settings["token_in"],
                        "amount_in": "0.01",
                        "deadline_seconds": 120,
                    },
                }
            ],
        }
    )


async def arm_rule(config, store):
    async with DexClient(config) as dex:
        snapshot = await dex.get_pair_snapshot(config.rules[0].trigger.pair_id)
    store.save_rule_state(
        config.rules[0].id, RuleRuntimeState(baseline_price=snapshot.price_token1_per_token0 / 2)
    )


async def settle(worker):
    async with asyncio.timeout(60):
        while worker.store.pending_execution() is not None:
            await worker.recover_once()
            await asyncio.sleep(0.1)
    return worker.store.list_runs()[0]


async def balance_and_nonce(config):
    async with DexClient(config) as dex:
        balance = await dex.client.get_state(
            config.rules[0].action.src, "balances", dex.wallet_address
        )
        nonce = await tr.get_nonce_async(
            config.network.rpc_url, dex.wallet_address, session=dex.client.session
        )
    return Decimal(str(balance)), nonce


async def verify_nodes(settings, tx_hash, success):
    async def verify(url):
        async with XianAsync(url, chain_id=settings["chain_id"]) as client:
            result = await client.wait_for_tx(tx_hash, timeout_seconds=30)
            require(
                result.tx_hash == tx_hash and result.success is success,
                "validators disagree on transaction outcome",
            )
            balance = await client.get_state(
                settings["token_in"], "balances", Wallet(settings["private_key"]).public_key
            )
            return str(balance)

    balances = await asyncio.gather(*(verify(url) for url in settings["rpc_urls"]))
    require(len(set(balances)) == 1, "validator token balances diverged")


async def assert_one_swap(config, before):
    balance, nonce = await balance_and_nonce(config)
    require(nonce == before[1] + 1, "recovery consumed more than one nonce")
    require(
        balance == before[0] - config.rules[0].action.amount_in,
        "swap input was debited more than once",
    )


async def concurrent_evaluations(config, store):
    await arm_rule(config, store)
    before = await balance_and_nonce(config)
    workers = [AutomationWorker(config, AutomationStore(store.path)) for _ in range(2)]
    results = await asyncio.gather(
        *(worker.evaluate_pair_once(config.rules[0].trigger.pair_id) for worker in workers)
    )
    require(
        any(item[0]["reason"] == "execution_busy" for item in results),
        "overlap did not exercise the execution lock",
    )
    run = await settle(workers[0])
    require(
        run["status"] == "submitted" and len(store.list_runs()) == 1,
        "overlap produced multiple runs",
    )
    await assert_one_swap(config, before)
    return run["tx_hash"]


async def crash_after_broadcast(config):
    import xian_dex_automation.execution as execution

    class CrashTransport(SwapTransport):
        async def broadcast(self):
            rejection = await super().broadcast()
            require(rejection is None, "crash probe was rejected before broadcast")
            os._exit(CRASH_EXIT)

    execution.SwapTransport = CrashTransport
    worker = AutomationWorker(config, AutomationStore(config.database_path))
    await worker.evaluate_pair_once(config.rules[0].trigger.pair_id)
    raise RuntimeError("crash probe never broadcast")


async def unrelated_transfer(settings):
    wallet = Wallet(settings["unrelated_private_key"])
    async with XianAsync(
        settings["rpc_urls"][1], wallet=wallet, chain_id=settings["chain_id"]
    ) as client:
        submission = await client.contract("currency").send(
            "transfer",
            amount=Decimal("0.01"),
            to=Wallet(settings["private_key"]).public_key,
            wait_for_tx=True,
        )
    require(
        submission.receipt is not None and submission.receipt.success,
        "unrelated wallet was unable to transact",
    )
    return submission.tx_hash


async def crash_and_reload(settings, config, store, settings_path):
    await arm_rule(config, store)
    before = await balance_and_nonce(config)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--config",
        str(settings_path),
        "--crash",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(60):
            _stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    require(
        process.returncode == CRASH_EXIT,
        f"crash probe exited {process.returncode}: {stderr.decode()[-500:]}",
    )
    pending = store.pending_execution()
    require(
        pending is not None and pending.broadcast_attempted, "process exit lost durable submission"
    )
    unrelated_hash = await unrelated_transfer(settings)
    require(store.pending_execution() is not None, "unrelated transaction touched automation state")
    target = config.model_copy(deep=True)
    target.network.rpc_url = settings["rpc_urls"][1]
    config_path = Path(settings["output_dir"]) / "config.yaml"
    save_config(config, config_path)
    app = create_app(config, config_path=config_path, admin_token="localnet-recovery-test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local.test"
    ) as client:
        async with asyncio.timeout(60):
            response = await client.put(
                "/config.yaml",
                content=render_config(target),
                headers={
                    "Authorization": "Bearer localnet-recovery-test",
                    "Content-Type": "text/plain",
                },
            )
    require(response.status_code == 200, "live config failed to drain via another validator")
    require(store.pending_execution() is None, "reload abandoned pending execution")
    require(
        store.list_runs()[0]["tx_hash"] == pending.prepared.transaction.tx_hash,
        "reload replaced signed transaction",
    )
    require(store.active_config(config) == target, "reload did not publish target configuration")
    await assert_one_swap(target, before)
    return target, pending.prepared.transaction.tx_hash, unrelated_hash


async def prepare_probe(config, *, revert):
    async with DexClient(config) as dex:
        snapshot = await dex.get_pair_snapshot(config.rules[0].trigger.pair_id)
        prepared = await dex.prepare_swap(
            snapshot, config.rules[0].action, amount_out_min=Decimal("0")
        )
        if revert:
            payload = json.loads(prepared.transaction.json_bytes)["payload"]
            payload["kwargs"]["amountOutMin"] = {"__fixed__": "1000000000000"}
            prepared = replace(
                prepared,
                transaction=tr.prepare_transaction(tr.create_tx(payload, dex.client.wallet)),
            )
    return snapshot, prepared


async def reverted_then_followup(config, store):
    await arm_rule(config, store)
    state = store.get_rule_state(config.rules[0].id)
    before = await balance_and_nonce(config)
    snapshot, rejected = await prepare_probe(config, revert=True)
    with store.execution_lock() as claimed:
        require(claimed, "test execution is busy")
        pending = store.begin_execution(
            rule_id=config.rules[0].id,
            prepared=rejected,
            next_baseline_price=snapshot.price_token1_per_token0,
            reason="e2e_forced_slippage",
            details={},
        )
        store.mark_broadcast_attempted(pending.run_id)
        async with SwapTransport(rejected) as transport:
            require(await transport.broadcast() is None, "slippage probe rejected before execution")
    worker = AutomationWorker(config, store)
    run = await settle(worker)
    require(run["status"] == "failed", "reverted swap recorded as successful")
    require(store.get_rule_state(config.rules[0].id) == state, "reverted swap advanced rule state")
    after = await balance_and_nonce(config)
    require(after[0] == before[0], "reverted swap spent input tokens")
    require(after[1] == before[1] + 1, "failed execution did not consume exactly one nonce")
    await worker.evaluate_pair_once(config.rules[0].trigger.pair_id)
    followup = await settle(worker)
    require_submitted(followup, "follow-up swap did not submit")
    await assert_one_swap(config, after)
    return rejected.transaction.tx_hash, followup["tx_hash"]


async def missing_receipt_then_independent(settings, config, store, *, success):
    target = config.model_copy(deep=True)
    original = target.rules[0]
    original.id = f"missing-receipt-{success}"
    independent = original.model_copy(deep=True)
    independent.id = f"independent-{success}"
    target.rules = [original, independent]
    await ConfigurationManager(AutomationWorker(config, store)).update(target, lambda: None)
    await arm_rule(target, store)
    snapshot, prepared = await prepare_probe(target, revert=not success)
    with store.execution_lock() as claimed:
        require(claimed, "test execution is busy")
        pending = store.begin_execution(
            rule_id=original.id,
            prepared=prepared,
            next_baseline_price=snapshot.price_token1_per_token0,
            reason="e2e_missing_receipt",
            details={},
        )
        store.mark_broadcast_attempted(pending.run_id)
        async with SwapTransport(prepared) as transport:
            require(await transport.broadcast() is None, "missing receipt probe rejected")
    # The test oracle knows the result on all validators. Hide ONLY this hash's
    # receipt from automation, leaving real committed-state reads and signing.
    await verify_nodes(settings, prepared.transaction.tx_hash, success)
    before = await balance_and_nonce(target)
    run_count = len(store.list_runs())

    class HiddenReceiptTransport(SwapTransport):
        async def lookup(self):
            if self.prepared.transaction.tx_hash == prepared.transaction.tx_hash:
                return None
            return await super().lookup()

        async def broadcast(self):
            require(
                self.prepared.transaction.tx_hash != prepared.transaction.tx_hash,
                "consumed transaction was rebroadcast",
            )
            return await super().broadcast()

    with patch("xian_dex_automation.execution.SwapTransport", HiddenReceiptTransport):
        worker = AutomationWorker(target, store)
        result = await worker.recover_once()
        require(result["status"] == "outcome_unknown", "consumed nonce did not release queue")
        require(store.pending_execution() is None, "consumed nonce retained shared queue")
        store.save_rule_state(
            independent.id,
            RuleRuntimeState(
                baseline_price=snapshot.price_token1_per_token0 / 2,
            ),
        )
        for attempt in range(3):
            # Reopen SQLite and recreate the worker to test durable suppression.
            worker = AutomationWorker(target, AutomationStore(store.path))
            results = await worker.evaluate_pair_once(original.trigger.pair_id)
            require(results[0]["reason"] == "rule_outcome_unknown", "original decision repeated")
            if attempt == 0:
                followup = await settle(worker)
                require_submitted(followup, "independent rule did not submit")
                await assert_one_swap(target, before)
                changed = target.model_copy(deep=True)
                changed.network.rpc_url = settings["rpc_urls"][2]
                await ConfigurationManager(worker).update(changed, lambda: None)
                target = changed
        require(len(store.list_runs()) == run_count + 1, "missing receipt repeated a decision")
        await assert_one_swap(target, before)
        require(store.uncertain_execution(original.id) is not None, "unknown outcome lost")

    # Restore receipt visibility: success AND revert must rebaseline the held
    # rule without resubmitting its old decision or changing the run count.
    worker = AutomationWorker(target, store)
    results = await worker.evaluate_pair_once(original.trigger.pair_id)
    require(results[0]["reason"] == "baseline_initialized", "receipt recovery replayed decision")
    require(store.uncertain_execution(original.id) is None, "restored receipt did not release rule")
    require(len(store.list_runs()) == run_count + 1, "receipt restoration created another trade")
    await assert_one_swap(target, before)
    await verify_nodes(settings, followup["tx_hash"], True)
    return target, {
        "original": prepared.transaction.tx_hash,
        "independent": followup["tx_hash"],
        "original_success": success,
    }


async def run(settings, settings_path):
    require(len(settings["rpc_urls"]) == 5, "recovery phase requires five validators")
    config = make_config(settings)
    require(not config.database_path.exists(), "use a fresh test artifact directory")
    store = AutomationStore(config.database_path)
    async with DexClient(config) as dex:
        approved = await dex.client.contract(settings["token_in"]).send(
            "approve",
            amount=Decimal("10"),
            to=settings["router"],
            wait_for_tx=True,
        )
        require(
            approved.receipt is not None and approved.receipt.success, "test token approval failed"
        )
    concurrent_hash = await concurrent_evaluations(config, store)
    await verify_nodes(settings, concurrent_hash, True)
    config, crash_hash, unrelated_hash = await crash_and_reload(
        settings, config, store, settings_path
    )
    await verify_nodes(settings, crash_hash, True)
    reverted_hash, followup_hash = await reverted_then_followup(config, store)
    await verify_nodes(settings, reverted_hash, False)
    await verify_nodes(settings, followup_hash, True)
    missing_receipts = []
    for success in (True, False):
        config, result = await missing_receipt_then_independent(
            settings, config, store, success=success
        )
        missing_receipts.append(result)
    require(len(store.list_runs()) == 8, "recovery duplicated run records")
    return {
        "ok": True,
        "nodes_verified": 5,
        "concurrent_swap": concurrent_hash,
        "recovered_after_process_exit": crash_hash,
        "reverted_swap": reverted_hash,
        "followup_swap": followup_hash,
        "unrelated_wallet_transfer": unrelated_hash,
        "consumed_nonce_missing_receipts": missing_receipts,
        "runs": len(store.list_runs()),
        "active_rpc": config.network.rpc_url,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--crash", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    settings = json.loads(args.config.read_text())
    if args.crash:
        asyncio.run(crash_after_broadcast(make_config(settings)))
    else:
        print(json.dumps(asyncio.run(run(settings, args.config)), sort_keys=True))


if __name__ == "__main__":
    main()
