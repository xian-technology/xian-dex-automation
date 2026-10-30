import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from xian_py.transaction import create_tx, prepare_transaction
from xian_py.wallet import Wallet

from xian_dex_automation.rules import RuleRuntimeState
from xian_dex_automation.storage import AutomationStore


def prepared_swap():
    from xian_dex_automation.submission import PreparedSwap

    wallet = Wallet("1" * 64)
    tx = prepare_transaction(
        create_tx(
            {
                "chain_id": "xian-local",
                "sender": wallet.public_key,
                "nonce": 0,
                "contract": "con_dex",
                "function": "swapExactTokenForToken",
                "kwargs": {"amountIn": Decimal("5.25")},
                "chi_supplied": 1000,
            },
            wallet,
        )
    )
    return PreparedSwap("http://127.0.0.1:26657", "xian-local", tx)


def begin(store):
    return store.begin_execution(
        rule_id="r1",
        prepared=prepared_swap(),
        next_baseline_price=Decimal("1.02"),
        reason="price_move_threshold_reached",
        details={"pair_id": 1},
    )


def test_journal_survives_restart_with_exact_signed_bytes(tmp_path):
    store = AutomationStore(tmp_path / "state.sqlite3")
    pending = begin(store)
    restored = AutomationStore(store.path).pending_execution()
    assert restored.run_id == pending.run_id
    assert restored.prepared.transaction.json_bytes == prepared_swap().transaction.json_bytes
    assert restored.prepared.transaction.tx_hash == pending.prepared.transaction.tx_hash
    assert not restored.broadcast_attempted
    with pytest.raises(sqlite3.IntegrityError):
        begin(store)
    assert len(store.list_runs()) == 1


@pytest.mark.parametrize("success", [True, False])
def test_completion_atomically_records_outcome_and_rule_state(tmp_path, success):
    store = AutomationStore(tmp_path / "state.sqlite3")
    store.save_rule_state("r1", RuleRuntimeState(baseline_price=Decimal("1")))
    pending = begin(store)
    store.mark_broadcast_attempted(pending.run_id)
    assert AutomationStore(store.path).pending_execution().broadcast_attempted
    now = datetime(2026, 10, 30, tzinfo=UTC)
    result = store.finish_execution(pending.run_id, success=success, error="reverted", now=now)
    assert result["status"] == ("submitted" if success else "failed")
    assert store.pending_execution() is None
    assert len(store.list_runs()) == 1
    state = store.get_rule_state("r1")
    assert state.baseline_price == (Decimal("1.02") if success else Decimal("1"))
    assert state.last_action_at == (now if success else None)
    with pytest.raises(ValueError, match="active execution"):
        store.finish_execution(pending.run_id, success=True, now=now)


def test_failed_completion_commit_keeps_pending_transaction_and_baseline(tmp_path):
    store = AutomationStore(tmp_path / "state.sqlite3")
    store.save_rule_state("r1", RuleRuntimeState(baseline_price=Decimal("1")))
    pending = begin(store)
    with store.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_completion BEFORE DELETE ON pending_execution
            BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
        store.finish_execution(pending.run_id, success=True, now=datetime.now(UTC))
    assert store.pending_execution() is not None
    assert store.list_runs()[0]["status"] == "pending"
    assert store.get_rule_state("r1").baseline_price == Decimal("1")


def test_corrupt_signed_payload_is_not_recovered(tmp_path):
    store = AutomationStore(tmp_path / "state.sqlite3")
    begin(store)
    with store.connect() as connection:
        connection.execute("UPDATE pending_execution SET tx_hash = 'wrong'")
    with pytest.raises(ValueError, match="hash"):
        store.pending_execution()


def test_corrupt_saved_chain_is_not_recovered(tmp_path):
    store = AutomationStore(tmp_path / "state.sqlite3")
    begin(store)
    with store.connect() as connection:
        connection.execute("UPDATE pending_execution SET chain_id = 'wrong'")
    with pytest.raises(ValueError, match="chain"):
        store.pending_execution()
