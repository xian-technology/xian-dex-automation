from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .rules import RuleRuntimeState


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class AutomationStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self.connect() as connection:
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
                    str(state.baseline_price)
                    if state.baseline_price is not None
                    else None,
                    state.last_action_at.isoformat()
                    if state.last_action_at is not None
                    else None,
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
            connection.execute(
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
                """,
                (name, cursor, _utc_now_iso()),
            )
