from __future__ import annotations

import asyncio
import os

import pytest

from xian_dex_automation.config import load_config
from xian_dex_automation.storage import AutomationStore
from xian_dex_automation.worker import AutomationWorker


@pytest.mark.live
def test_live_node_evaluates_configured_dex_pair(tmp_path) -> None:
    rpc_url = os.environ.get("XIAN_DEX_AUTOMATION_LIVE_RPC_URL")
    if not rpc_url:
        pytest.skip("set XIAN_DEX_AUTOMATION_LIVE_RPC_URL to run live test")

    pair_id = int(os.environ.get("XIAN_DEX_AUTOMATION_LIVE_PAIR_ID", "1"))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
network:
  rpc_url: "{rpc_url}"
wallet:
  execute: false
database_path: "automation.sqlite3"
rules:
  - id: "live-price-move"
    enabled: true
    trigger:
      type: "price_move"
      pair_id: {pair_id}
      direction: "either"
      threshold_bps: 100
      cooldown_seconds: 300
    action:
      type: "swap_exact_in"
      src: "currency"
      amount_in: "1"
      max_slippage_bps: 100
      deadline_seconds: 300
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    worker = AutomationWorker(config, AutomationStore(config.database_path))

    first_result = asyncio.run(worker.evaluate_pair_once(pair_id))
    second_result = asyncio.run(worker.evaluate_pair_once(pair_id))

    assert len(first_result) == 1
    assert len(second_result) == 1
    assert first_result[0]["details"]["pair_id"] == pair_id
    assert second_result[0]["details"]["pair_id"] == pair_id
    assert first_result[0]["details"]["token0"]
    assert first_result[0]["details"]["token1"]
    assert first_result[0]["details"]["current_price"]
    assert second_result[0]["status"] in {"skipped", "dry_run"}
