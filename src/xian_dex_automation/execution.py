"""Recover a journaled swap under the database's execution lock."""

from datetime import UTC, datetime

from .storage import AutomationStore
from .submission import PendingExecution, SwapTransport


async def reconcile_execution(store: AutomationStore, *, allow_broadcast: bool):
    pending = store.pending_execution()
    if pending is None:
        return None
    try:
        async with SwapTransport(pending.prepared) as transport:
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
        receipt = await transport.lookup()
        if receipt is not None:
            return _finish_receipt(store, pending, receipt)
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
