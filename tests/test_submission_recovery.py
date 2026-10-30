import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_execution_journal import prepared_swap
from xian_py.exception import TxTimeoutError
from xian_py.models import TransactionReceipt


def receipt(prepared, success=True):
    return TransactionReceipt.from_lookup(
        {
            "success": success,
            "tx_hash": prepared.transaction.tx_hash,
            "message": None if success else "reverted",
        }
    )


def transport():
    from xian_dex_automation.submission import SwapTransport

    prepared = prepared_swap()
    result = SwapTransport(prepared)
    result.client = SimpleNamespace(
        get_chain_id=AsyncMock(return_value=prepared.chain_id),
        wait_for_tx=AsyncMock(return_value=receipt(prepared)),
        node_url=prepared.rpc_url,
        session=object(),
    )
    return result


def test_broadcast_sends_exact_persisted_transaction(monkeypatch):
    t = transport()
    send = AsyncMock(return_value={"result": {"code": 0, "hash": t.prepared.transaction.tx_hash}})
    monkeypatch.setattr("xian_dex_automation.submission.tr.broadcast_tx_wait_async", send)
    assert asyncio.run(t.broadcast()) is None
    assert send.call_args.args[1].json_bytes == t.prepared.transaction.json_bytes


def test_changed_chain_cannot_receive_a_recovered_broadcast(monkeypatch):
    t = transport()
    t.client.get_chain_id.return_value = "different-chain"
    send = AsyncMock()
    monkeypatch.setattr("xian_dex_automation.submission.tr.broadcast_tx_wait_async", send)
    with pytest.raises(ValueError, match="chain"):
        asyncio.run(t.broadcast())
    send.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        {"error": {"message": "timeout"}},
        {},
        {"result": {"code": 0, "hash": "different"}},
        {"result": {"code": 1, "log": "tx already exists in cache"}},
    ],
)
def test_ambiguous_broadcast_responses_are_not_definitive_rejections(monkeypatch, response):
    t = transport()
    monkeypatch.setattr(
        "xian_dex_automation.submission.tr.broadcast_tx_wait_async",
        AsyncMock(return_value=response),
    )
    with pytest.raises(ValueError):
        asyncio.run(t.broadcast())


def test_checktx_rejection_is_explicit(monkeypatch):
    t = transport()
    monkeypatch.setattr(
        "xian_dex_automation.submission.tr.broadcast_tx_wait_async",
        AsyncMock(return_value={"result": {"code": 1, "log": "insufficient funds"}}),
    )
    assert asyncio.run(t.broadcast()) == "insufficient funds"


def test_lookup_timeout_and_wrong_hash_cannot_complete_a_trade():
    t = transport()
    t.client.wait_for_tx.side_effect = TxTimeoutError("not found")
    assert asyncio.run(t.lookup()) is None
    t.client.wait_for_tx.side_effect = None
    t.client.wait_for_tx.return_value = TransactionReceipt.from_lookup(
        {"success": True, "tx_hash": "other"}
    )
    with pytest.raises(ValueError, match="hash"):
        asyncio.run(t.lookup())


@pytest.mark.parametrize("success", [True, False])
def test_lookup_preserves_confirmed_execution_outcome(success):
    t = transport()
    t.client.wait_for_tx.return_value = receipt(t.prepared, success)
    assert asyncio.run(t.lookup()).success is success


def test_lookup_can_observe_a_receipt_arriving_on_the_next_poll(monkeypatch):
    from xian_py import RetryPolicy, XianClientConfig
    from xian_py.exception import TransportError

    from xian_dex_automation.submission import SwapTransport

    prepared = prepared_swap()
    t = SwapTransport(prepared)
    t.client.config = XianClientConfig(retry=RetryPolicy(max_attempts=1))
    monkeypatch.setattr(t.client, "get_chain_id", AsyncMock(return_value=prepared.chain_id))
    monkeypatch.setattr("xian_py.transaction.get_tx_async", AsyncMock(side_effect=[
        TransportError("index temporarily unavailable"),
        {"result": {"hash": prepared.transaction.tx_hash, "tx_result": {"code": 0}}},
    ]))

    async def scenario():
        async with t:
            result = await t.lookup()
            assert result is not None
            assert result.success
            assert result.tx_hash == prepared.transaction.tx_hash

    asyncio.run(scenario())
