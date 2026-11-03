import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/recovery_smoke.py"
SPEC = importlib.util.spec_from_file_location("recovery_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_submission_diagnostic_distinguishes_rejection_without_dumping_journal():
    run = {
        "status": "failed",
        "reason": "submission_failed",
        "tx_hash": "not-needed",
        "details": {
            "error": "TransactionException: Transaction sender has too few chi for this transaction",
            "private_key": "secret-wallet-key",
            "tx_json": '{"payload":"secret-signed-payload"}',
        },
    }
    with pytest.raises(RuntimeError) as failure:
        smoke.require_submitted(run, "independent rule did not submit")
    message = str(failure.value)
    summary = json.loads(message.split(": ", 1)[1])
    assert summary == {
        "status": "failed",
        "reason": "submission_failed",
        "error": run["details"]["error"],
    }
    assert "secret-wallet-key" not in message
    assert "secret-signed-payload" not in message


def test_submission_diagnostic_redacts_long_hex_and_accepts_success():
    key = "ab" * 32
    with pytest.raises(RuntimeError) as failure:
        smoke.require_submitted(
            {"status": "failed", "reason": "submission_failed", "details": {"error": key}},
            "follow-up failed",
        )
    assert key not in str(failure.value)
    assert "redacted" in str(failure.value)
    smoke.require_submitted({"status": "submitted"}, "must not fail")


@pytest.fixture
def validators(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    state = SimpleNamespace(balances=["10"] * 5, calls=[], wrong_receipt=None, divergent=False)

    class Client:
        def __init__(self, url, **kwargs):
            self.node = int(url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def wait_for_tx(self, tx_hash, **kwargs):
            await asyncio.sleep(0)
            state.calls.append(("receipt", self.node, tx_hash))
            if tx_hash == "followup":
                state.balances[self.node] = "8" if state.divergent and self.node == 4 else "9"
            result = SimpleNamespace(tx_hash=tx_hash, success=tx_hash != "revert")
            if state.wrong_receipt is not None and self.node == 4 and tx_hash == "followup":
                setattr(result, *state.wrong_receipt)
            return result

        async def get_state(self, *args):
            state.calls.append(("balance", self.node))
            return state.balances[self.node]

    monkeypatch.setattr(smoke, "XianAsync", Client)
    monkeypatch.setattr(smoke, "Wallet", lambda key: SimpleNamespace(public_key="public-test-key"))
    settings = {
        "rpc_urls": list(map(str, range(5))),
        "chain_id": "local-test",
        "token_in": "con_token",
        "private_key": "never-used",
    }
    return settings, state


def test_run_waits_for_revert_and_followup_on_every_validator(monkeypatch, tmp_path, validators):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    settings, state = validators
    config = SimpleNamespace(database_path=tmp_path / "fresh", network=SimpleNamespace(rpc_url="0"))
    store = SimpleNamespace(list_runs=lambda: [None] * 8)
    monkeypatch.setattr(smoke, "make_config", lambda settings: config)
    monkeypatch.setattr(smoke, "AutomationStore", lambda path: store)

    class Dex:
        def __init__(self, config):
            self.client = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def contract(self, name):
            return self

        async def send(self, *args, **kwargs):
            return SimpleNamespace(receipt=SimpleNamespace(success=True))

    settings["router"] = "con_router"
    monkeypatch.setattr(smoke, "DexClient", Dex)
    monkeypatch.setattr(smoke, "concurrent_evaluations", AsyncMock(return_value="concurrent"))
    monkeypatch.setattr(
        smoke, "crash_and_reload", AsyncMock(return_value=(config, "crash", "other"))
    )
    monkeypatch.setattr(
        smoke, "missing_receipt_then_independent", AsyncMock(return_value=(config, {}))
    )

    async def submit_pair(*args):
        # Both transactions exist, but only the first validator has applied the
        # followup so far. Reading after just the revert yields a false mismatch.
        state.calls.clear()
        state.balances[0] = "9"
        return "revert", "followup"

    monkeypatch.setattr(smoke, "reverted_then_followup", submit_pair)
    result = asyncio.run(smoke.run(settings, tmp_path / "settings.json"))
    assert result["ok"] is True
    first_balance = next(i for i, call in enumerate(state.calls) if call[0] == "balance")
    assert set(state.calls[:first_balance]) == {
        ("receipt", node, tx_hash) for node in range(5) for tx_hash in ("revert", "followup")
    }
    assert len([call for call in state.calls if call[0] == "balance"]) == 5


@pytest.mark.parametrize("wrong_receipt", [("tx_hash", "wrong-hash"), ("success", False)])
def test_receipt_mismatch_fails_before_any_balance_read(validators, wrong_receipt):
    import asyncio

    settings, state = validators
    state.wrong_receipt = wrong_receipt
    with pytest.raises(RuntimeError, match="validators disagree on transaction outcome"):
        asyncio.run(smoke.verify_nodes(settings, [("revert", False), ("followup", True)]))
    assert not any(call[0] == "balance" for call in state.calls)


def test_completed_batch_still_rejects_real_balance_mismatch(validators):
    import asyncio

    settings, state = validators
    state.divergent = True
    with pytest.raises(RuntimeError, match="validator token balances diverged"):
        asyncio.run(smoke.verify_nodes(settings, [("revert", False), ("followup", True)]))
    assert len([call for call in state.calls if call[0] == "balance"]) == 5
