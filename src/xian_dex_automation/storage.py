from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from xian_py.transaction import prepare_transaction

from .config import AutomationConfig
from .rules import RuleRuntimeState
from .submission import PendingExecution, PreparedSwap


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class AutomationStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def execution_lock(self):
        """One evaluator per local database, including other service processes.

        Keep the lock file: unlinking it could create two lock authorities.
        The kernel releases the claim on process exit; the journal survives.
        """
        return self._file_lock("execution", shared=False)

    def configuration_lock(self, *, shared: bool = False):
        return self._file_lock("configuration", shared=shared)

    @contextmanager
    def _file_lock(self, name: str, *, shared: bool):
        with self.path.with_suffix(self.path.suffix + f".{name}.lock").open("a") as lock:
            try:
                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(lock, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def evaluation_lock(self):
        # Release the short admission lock before doing any async work. A
        # configuration writer can then close admission while an evaluation
        # finishes under the execution lock, without starving behind readers.
        with ExitStack() as execution:
            with self.configuration_lock(shared=True) as admitted:
                reason = "configuration_draining"
                if admitted:
                    acquired = execution.enter_context(self.execution_lock())
                    reason = None if acquired else "execution_busy"
            yield reason

    def active_config(self, default: AutomationConfig) -> AutomationConfig:
        with self.connect() as connection:
            row = connection.execute("select config_json from runtime_config where slot = 1").fetchone()
        return default if row is None else AutomationConfig.model_validate_json(row["config_json"])

    def save_active_config(self, config: AutomationConfig, *, previous: AutomationConfig) -> None:
        with self.connect() as connection:
            connection.execute(
                """insert into runtime_config (slot, config_json, generation) values (1, ?, 1)
                    on conflict(slot) do update set config_json = excluded.config_json,
                        generation = runtime_config.generation + 1""",
                (config.model_dump_json(),),
            )
            self._reset_changed_markets(connection, previous, config)

    @staticmethod
    def _reset_changed_markets(connection, previous, target):
        market_changed = (
            previous.network.rpc_url != target.network.rpc_url
            or previous.network.chain_id != target.network.chain_id
            or previous.dex != target.dex
        )
        if market_changed:
            connection.execute("update rule_state set baseline_price = null")
            connection.execute("delete from cursors")
            return
        previous_pairs = {rule.id: rule.trigger.pair_id for rule in previous.rules}
        target_pairs = {rule.id: rule.trigger.pair_id for rule in target.rules}
        for rule_id in previous_pairs.keys() | target_pairs.keys():
            if previous_pairs.get(rule_id) != target_pairs.get(rule_id):
                connection.execute("update rule_state set baseline_price = null where rule_id = ?", (rule_id,))

    def config_generation(self) -> int:
        with self.connect() as connection:
            row = connection.execute("select generation from runtime_config where slot = 1").fetchone()
        return 0 if row is None else row["generation"]

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self.connect() as connection:
            connection.execute("""create table if not exists uncertain_executions (
                run_id integer primary key references runs(id),
                rpc_url text not null, chain_id text not null,
                tx_json blob not null, tx_hash text not null
            )""")
            connection.execute("""create table if not exists runtime_config (
                slot integer primary key check (slot = 1), config_json text not null,
                generation integer not null)""")
            connection.execute(
                """
                create table if not exists rule_state (
                    rule_id text primary key,
                    baseline_price text,
                    last_action_at text,
                    updated_at text not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists runs (
                    id integer primary key autoincrement,
                    rule_id text not null,
                    status text not null,
                    reason text not null,
                    tx_hash text,
                    details_json text not null,
                    created_at text not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists cursors (
                    name text primary key,
                    cursor integer,
                    updated_at text not null
                )
                """
            )
            connection.execute(
                """create table if not exists pending_execution (
                    slot integer primary key check (slot = 1),
                    run_id integer not null unique references runs(id),
                    rpc_url text not null,
                    chain_id text not null,
                    tx_json blob not null,
                    tx_hash text not null,
                    next_baseline_price text not null,
                    broadcast_attempted integer not null default 0
                        check (broadcast_attempted in (0, 1))
                )"""
            )

    def begin_execution(
        self,
        *,
        rule_id: str,
        prepared: PreparedSwap,
        next_baseline_price: Decimal,
        reason: str,
        details: dict[str, Any],
    ) -> PendingExecution:
        with self.connect() as connection:
            run_id = self._append_run(
                connection,
                rule_id=rule_id,
                status="pending",
                reason=reason,
                tx_hash=prepared.transaction.tx_hash,
                details=details,
            )
            connection.execute(
                """insert into pending_execution
                    (slot, run_id, rpc_url, chain_id, tx_json, tx_hash, next_baseline_price)
                    values (1, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    prepared.rpc_url,
                    prepared.chain_id,
                    prepared.transaction.json_bytes,
                    prepared.transaction.tx_hash,
                    str(next_baseline_price),
                ),
            )
        return PendingExecution(run_id, rule_id, prepared, False, reason, dict(details))

    def pending_execution(self) -> PendingExecution | None:
        with self.connect() as connection:
            row = connection.execute(
                """select p.*, r.rule_id, r.reason, r.details_json
                    from pending_execution p join runs r on r.id = p.run_id"""
            ).fetchone()
        if row is None:
            return None
        return self._execution_from_row(row)

    @staticmethod
    def _execution_from_row(row) -> PendingExecution:
        transaction = prepare_transaction(row["tx_json"])
        if transaction.tx_hash != row["tx_hash"]:
            raise ValueError("persisted transaction hash mismatch")
        return PendingExecution(
            row["run_id"],
            row["rule_id"],
            PreparedSwap(row["rpc_url"], row["chain_id"], transaction),
            bool(row["broadcast_attempted"]),
            row["reason"],
            json.loads(row["details_json"]),
        )

    def uncertain_execution(self, rule_id: str) -> PendingExecution | None:
        with self.connect() as connection:
            row = connection.execute(
                """select u.*, r.rule_id, r.reason, r.details_json, 1 as broadcast_attempted
                    from uncertain_executions u join runs r on r.id = u.run_id
                    where r.rule_id = ?""", (rule_id,),
            ).fetchone()
        return None if row is None else self._execution_from_row(row)

    def release_consumed_execution(self, run_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """select p.*, r.rule_id, r.reason, r.details_json
                    from pending_execution p join runs r on r.id = p.run_id
                    where p.run_id = ?""", (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("no matching active execution")
            connection.execute(
                """insert into uncertain_executions (run_id, rpc_url, chain_id, tx_json, tx_hash)
                    select run_id, rpc_url, chain_id, tx_json, tx_hash
                    from pending_execution where run_id = ?""", (run_id,),
            )
            details = json.loads(row["details_json"])
            details.pop("recovery_error", None)
            details["nonce_consumed"] = True
            result = dict(rule_id=row["rule_id"], tx_hash=row["tx_hash"], details=details,
                          status="outcome_unknown", reason="nonce_consumed_receipt_missing")
            connection.execute(
                "update runs set status = ?, reason = ?, details_json = ? where id = ?",
                (result["status"], result["reason"], json.dumps(details), run_id),
            )
            connection.execute("delete from pending_execution where run_id = ?", (run_id,))
        return result

    def resolve_uncertain_execution(self, run_id: int, *, success: bool, error: str | None,
                                    now: datetime) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """select r.* from uncertain_executions u join runs r on r.id = u.run_id
                    where u.run_id = ?""", (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("no matching uncertain execution")
            result = self._execution_result(row, success, error)
            connection.execute(
                "update runs set status = ?, reason = ?, details_json = ? where id = ?",
                (result["status"], "receipt_recovered", json.dumps(result["details"]), run_id),
            )
            # Independent trades/configuration may have changed this market.
            # Start from a fresh baseline even when the old trade reverted.
            self._save_rule_state(connection, row["rule_id"], RuleRuntimeState(last_action_at=now))
            connection.execute("delete from uncertain_executions where run_id = ?", (run_id,))

    def mark_broadcast_attempted(self, run_id: int) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                "update pending_execution set broadcast_attempted = 1 where run_id = ?",
                (run_id,),
            ).rowcount
            if changed != 1:
                raise ValueError("no matching active execution")

    def note_execution_error(self, run_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """update runs set details_json = json_set(details_json, '$.recovery_error', ?)
                    where id = ? and id in (select run_id from pending_execution)""",
                (error, run_id),
            )

    def finish_execution(
        self,
        run_id: int,
        *,
        success: bool,
        now: datetime,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """select p.*, r.rule_id, r.reason, r.details_json
                    from pending_execution p join runs r on r.id = p.run_id
                    where p.run_id = ?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("no matching active execution")
            result = self._execution_result(row, success, error)
            if success:
                self._save_rule_state(
                    connection,
                    row["rule_id"],
                    RuleRuntimeState(
                        baseline_price=Decimal(row["next_baseline_price"]),
                        last_action_at=now,
                    ),
                )
            connection.execute(
                "update runs set status = ?, reason = ?, details_json = ? where id = ?",
                (result["status"], result["reason"], json.dumps(result["details"]), run_id),
            )
            connection.execute("delete from pending_execution where run_id = ?", (run_id,))
        return result

    @staticmethod
    def _execution_result(row, success: bool, error: str | None) -> dict[str, Any]:
        details = json.loads(row["details_json"])
        details.pop("recovery_error", None)
        if not success:
            details["error"] = error or "swap execution failed"
        return {
            "rule_id": row["rule_id"],
            "tx_hash": row["tx_hash"],
            "details": details,
            "status": "submitted" if success else "failed",
            "reason": row["reason"] if success else "submission_failed",
        }

    def get_rule_state(self, rule_id: str) -> RuleRuntimeState:
        with self.connect() as connection:
            row = connection.execute(
                "select baseline_price, last_action_at from rule_state where rule_id = ?",
                (rule_id,),
            ).fetchone()
        if row is None:
            return RuleRuntimeState()
        baseline = row["baseline_price"]
        return RuleRuntimeState(
            baseline_price=Decimal(baseline) if baseline else None,
            last_action_at=_parse_datetime(row["last_action_at"]),
        )

    def save_rule_state(self, rule_id: str, state: RuleRuntimeState) -> None:
        with self.connect() as connection:
            self._save_rule_state(connection, rule_id, state)

    @staticmethod
    def _save_rule_state(connection, rule_id: str, state: RuleRuntimeState) -> None:
        connection.execute(
            """
                insert into rule_state (
                    rule_id, baseline_price, last_action_at, updated_at
                )
                values (?, ?, ?, ?)
                on conflict(rule_id) do update set
                    baseline_price = excluded.baseline_price,
                    last_action_at = excluded.last_action_at,
                    updated_at = excluded.updated_at
                """,
            (
                rule_id,
                str(state.baseline_price) if state.baseline_price is not None else None,
                state.last_action_at.isoformat() if state.last_action_at is not None else None,
                _utc_now_iso(),
            ),
        )

    def append_run(
        self,
        *,
        rule_id: str,
        status: str,
        reason: str,
        tx_hash: str | None,
        details: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            self._append_run(
                connection,
                rule_id=rule_id,
                status=status,
                reason=reason,
                tx_hash=tx_hash,
                details=details,
            )

    @staticmethod
    def _append_run(connection, *, rule_id, status, reason, tx_hash, details) -> int:
        cursor = connection.execute(
            """
                insert into runs (
                    rule_id, status, reason, tx_hash, details_json, created_at
                )
                values (?, ?, ?, ?, ?, ?)
                """,
            (
                rule_id,
                status,
                reason,
                tx_hash,
                json.dumps(details, sort_keys=True, default=str),
                _utc_now_iso(),
            ),
        )
        return cursor.lastrowid

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                select id, rule_id, status, reason, tx_hash, details_json, created_at
                from runs
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "rule_id": row["rule_id"],
                "status": row["status"],
                "reason": row["reason"],
                "tx_hash": row["tx_hash"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_cursor(self, name: str) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "select cursor from cursors where name = ?",
                (name,),
            ).fetchone()
        return int(row["cursor"]) if row and row["cursor"] is not None else None

    def save_cursor(self, name: str, cursor: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into cursors (name, cursor, updated_at)
                values (?, ?, ?)
                on conflict(name) do update set
                    cursor = excluded.cursor,
                    updated_at = excluded.updated_at
                where cursors.cursor is null or excluded.cursor > cursors.cursor
                """,
                (name, cursor, _utc_now_iso()),
            )
