"""Drain shared execution before publishing a new worker configuration."""

import asyncio
from collections.abc import Callable

from .config import AutomationConfig
from .execution import reconcile_execution
from .worker import AutomationWorker


class ConfigurationBusy(RuntimeError):
    pass


class ConfigurationManager:
    def __init__(self, worker: AutomationWorker):
        self.worker = worker
        self.store = worker.store

    async def update(
        self,
        target: AutomationConfig,
        commit: Callable[[], None],
        *,
        after_commit: Callable[[], None] | None = None,
    ) -> None:
        if target.database_path.resolve() != self.store.path:
            raise ValueError("database_path changes require an offline database migration")
        with self.store.configuration_lock() as claimed:
            if not claimed:
                raise ConfigurationBusy("another configuration change is draining execution")
            while True:
                with self.store.execution_lock() as idle:
                    if idle:
                        await self._drain(target)
                        if self.store.pending_execution() is None:
                            self._publish(target, commit, after_commit)
                            return
                delay = (
                    target.network.poll_interval_seconds if self.store.pending_execution() else 0.05
                )
                await asyncio.sleep(delay)

    def _publish(self, target, commit, after_commit) -> None:
        previous = self.store.active_config(self.worker.config)
        commit()
        self.store.save_active_config(target, previous=previous)
        if after_commit is not None:
            after_commit()

    async def _drain(self, target: AutomationConfig) -> None:
        # A request to disable execution never broadcasts during its drain.
        # A chain switch must finish the old transaction on the old network.
        pending = self.store.pending_execution()
        if pending is None:
            return
        rpc_urls = [pending.prepared.rpc_url]
        if target.network.chain_id in (None, pending.prepared.chain_id):
            rpc_urls = list(dict.fromkeys([target.network.rpc_url, *rpc_urls]))
        for rpc_url in rpc_urls:
            await reconcile_execution(
                self.store,
                allow_broadcast=target.wallet.execute,
                rpc_url=rpc_url,
            )
            if self.store.pending_execution() is None:
                return
