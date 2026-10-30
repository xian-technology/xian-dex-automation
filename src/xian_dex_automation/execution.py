"""Recover a journaled swap under the database's execution lock."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from .storage import AutomationStore
from .submission import PendingExecution, SwapTransport


async def reconcile_execution(
    store: AutomationStore, *, allow_broadcast: bool, rpc_url: str | None = None
):
    pending = store.pending_execution()
    if pending is None:
        return None
    prepared = pending.prepared
    if rpc_url is not None:
        # Only the endpoint changes. Transport still verifies the ORIGINAL
        # chain and hash before lookup or rebroadcast; never re-sign here.
        prepared = replace(prepared, rpc_url=rpc_url)
    try:
        async with SwapTransport(prepared) as transport:
            return await _reconcile(store, pending, transport, allow_broadcast)
    except Exception as exc:
        # Cancellation/process exit must propagate, leaving the durable claim.
        error = f"{type(exc).__name__}: {exc}"
        store.note_execution_error(pending.run_id, error)
        result = pending.result()
        result["details"] = {**pending.details, "recovery_error": error}
        return result


async def _reconcile(store, pending, transport, allow_broadcast):
    if pending.broadcast_attempted:
        receipt = await _lookup_receipt(transport)
        if receipt is not None:
            return _finish_receipt(store, pending, receipt)
        if await transport.nonce_consumed():
            return store.release_consumed_execution(pending.run_id)
    if not allow_broadcast:
        return pending.result()

    # Persist before touching the network, even if this process dies on the
    # next instruction. Future rejections cannot disprove an earlier broadcast.
    store.mark_broadcast_attempted(pending.run_id)
    rejection = await transport.broadcast()
    if rejection is not None and not pending.broadcast_attempted:
        return store.finish_execution(
            pending.run_id,
            success=False,
            error=rejection,
            now=datetime.now(UTC),
        )
    receipt = await transport.lookup()
    if receipt is not None:
        return _finish_receipt(store, pending, receipt)
    return pending.result()


def _finish_receipt(store: AutomationStore, pending: PendingExecution, receipt):
    return store.finish_execution(
        pending.run_id,
        success=receipt.success,
        error=receipt.message,
        now=datetime.now(UTC),
    )


async def reconcile_uncertain_rule(store: AutomationStore, rule_id: str) -> bool:
    """Return whether a rule remains held; never rebroadcast consumed nonces."""
    pending = store.uncertain_execution(rule_id)
    if pending is None:
        return False
    try:
        async with asyncio.timeout(2):
            async with SwapTransport(pending.prepared) as transport:
                receipt = await transport.lookup()
        if receipt is None:
            return True
        store.resolve_uncertain_execution(
            pending.run_id,
            success=receipt.success,
            error=receipt.message,
            now=datetime.now(UTC),
        )
        return False
    except Exception:
        # Failure to read this rule's receipt must not block independent rules.
        return True


async def _lookup_receipt(transport):
    try:
        async with asyncio.timeout(2):
            return await transport.lookup()
    except Exception:
        # The receipt index can fail independently of committed state queries.
        # Only the nonce check may release the queue after a failed lookup.
        return None
