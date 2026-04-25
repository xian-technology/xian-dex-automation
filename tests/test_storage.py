from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from xian_dex_automation.rules import RuleRuntimeState
from xian_dex_automation.storage import AutomationStore


def test_rule_state_round_trip(tmp_path) -> None:
    store = AutomationStore(tmp_path / "automation.sqlite3")
    now = datetime.now(UTC)

    store.save_rule_state(
        "r1",
        RuleRuntimeState(
            baseline_price=Decimal("1.23"),
            last_action_at=now,
        ),
    )

    state = store.get_rule_state("r1")
    assert state.baseline_price == Decimal("1.23")
    assert state.last_action_at == now


def test_runs_and_cursor_round_trip(tmp_path) -> None:
    store = AutomationStore(tmp_path / "automation.sqlite3")

    store.append_run(
        rule_id="r1",
        status="dry_run",
        reason="price_move_threshold_reached",
        tx_hash=None,
        details={"pair_id": 1},
    )
    store.save_cursor("con_pairs:Sync", 42)

    assert store.get_cursor("con_pairs:Sync") == 42
    runs = store.list_runs()
    assert runs[0]["status"] == "dry_run"
    assert runs[0]["details"] == {"pair_id": 1}

