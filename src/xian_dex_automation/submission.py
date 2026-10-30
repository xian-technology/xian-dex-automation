"""Signed submission identity and transport; no replacement signing on recovery."""

import json
from dataclasses import dataclass
from typing import Any

from xian_py import XianAsync
from xian_py import transaction as tr
from xian_py.exception import TxTimeoutError
from xian_py.models import TransactionReceipt
from xian_py.transaction import PreparedTransaction


@dataclass(frozen=True)
class PreparedSwap:
    rpc_url: str
    chain_id: str
    transaction: PreparedTransaction

    def __post_init__(self):
        payload = json.loads(self.transaction.json_bytes)["payload"]
        if payload["chain_id"] != self.chain_id:
            raise ValueError("persisted transaction chain ID mismatch")


@dataclass(frozen=True)
class PendingExecution:
    run_id: int
    rule_id: str
    prepared: PreparedSwap
    broadcast_attempted: bool
    reason: str
    details: dict[str, Any]

    def result(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "status": "pending",
            "reason": "submission_pending",
            "tx_hash": self.prepared.transaction.tx_hash,
            "details": self.details,
        }


class SwapTransport:
    """Read and rebroadcast on the original network without loading a signing key."""

    def __init__(self, prepared: PreparedSwap):
        self.prepared = prepared
        self.client = XianAsync(prepared.rpc_url, chain_id=prepared.chain_id)

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)

    async def _check_chain(self):
        if await self.client.get_chain_id() != self.prepared.chain_id:
            raise ValueError("RPC chain does not match the persisted transaction chain")

    async def broadcast(self) -> str | None:
        await self._check_chain()
        data = await tr.broadcast_tx_wait_async(
            self.prepared.rpc_url,
            self.prepared.transaction,
            session=self.client.session,
        )
        if "error" in data:
            raise ValueError("RPC did not return a definitive CheckTx result")
        result = data.get("result", {})
        code = result.get("code")
        if type(code) is not int:
            raise ValueError("CheckTx result is missing a valid status code")
        returned_hash = result.get("hash")
        if returned_hash is not None and returned_hash.upper() != self.prepared.transaction.tx_hash:
            raise ValueError("RPC transaction hash mismatch")
        if code == 0:
            return None
        message = str(result.get("log") or "transaction rejected")
        if "tx already exists" in message.lower():
            raise ValueError("transaction may already be pending or committed")
        return message

    async def lookup(self) -> TransactionReceipt | None:
        await self._check_chain()
        try:
            receipt = await self.client.wait_for_tx(
                self.prepared.transaction.tx_hash,
                timeout_seconds=1,
                poll_interval_seconds=0.25,
            )
        except TxTimeoutError:
            return None
        if receipt.tx_hash != self.prepared.transaction.tx_hash:
            raise ValueError("receipt transaction hash mismatch")
        return receipt
