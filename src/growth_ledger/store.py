"""SQLite 存储层。

所有事实表只追加（INSERT），不提供 UPDATE/DELETE 业务数据的方法；
状态机字段（event status、review status）是仅有的受控更新点，
且更新通过带条件的 UPDATE 语句完成（CAS 语义），历史行永不被覆盖。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    version INTEGER PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guides (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS point_events (
    id TEXT PRIMARY KEY,
    guide_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    occurred_on TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    reversed_by TEXT,
    note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(guide_id, source_ref)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_idem
    ON point_events(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS point_entries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    guide_id TEXT NOT NULL,
    account TEXT NOT NULL,
    amount INTEGER NOT NULL,
    occurred_on TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reversal_of TEXT,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_entries_reversal
    ON point_entries(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_entries_guide ON point_entries(guide_id);
CREATE TABLE IF NOT EXISTS level_movements (
    id TEXT PRIMARY KEY,
    guide_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    data TEXT NOT NULL,
    UNIQUE(guide_id, sequence)
);
CREATE TABLE IF NOT EXISTS benefit_grants (
    id TEXT PRIMARY KEY,
    guide_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_grants_guide ON benefit_grants(guide_id);
CREATE TABLE IF NOT EXISTS review_cases (
    id TEXT PRIMARY KEY,
    guide_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS level_snapshots (
    id TEXT PRIMARY KEY,
    guide_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE(guide_id, sequence)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots_hash
    ON level_snapshots(guide_id, content_hash);
CREATE TABLE IF NOT EXISTS promotion_runs (
    id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL UNIQUE,
    data TEXT NOT NULL
);
"""


def _default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"不可序列化的类型：{type(value)!r}")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_default)


def loads(value: str) -> Any:
    return json.loads(value)


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- 通用 ----------

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        return self.conn.execute(sql, tuple(params)).fetchone()[0]

    # ---------- 规则 ----------

    def insert_rule(self, version: int, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO rules(version, data) VALUES (?, ?)", (version, dumps(data))
        )

    def get_rule_row(self, version: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT data FROM rules WHERE version = ?", (version,)
        ).fetchone()

    def list_rule_versions(self) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT version FROM rules ORDER BY version")]

    def rule_version_for(self, on_date: date) -> int | None:
        row = self.conn.execute(
            "SELECT version FROM rules "
            "WHERE json_extract(data, '$.effective_from') <= ? "
            "ORDER BY version DESC LIMIT 1",
            (on_date.isoformat(),),
        ).fetchone()
        return row[0] if row else None

    # ---------- 讲解员 ----------

    def insert_guide(self, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO guides(id, data) VALUES (?, ?)", (data["id"], dumps(data))
        )

    def get_guide_row(self, guide_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT data FROM guides WHERE id = ?", (guide_id,)
        ).fetchone()

    def list_guide_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT id FROM guides ORDER BY id")]

    # ---------- 积分事件 / 分录 ----------

    def insert_event(self, e: dict) -> None:
        self.conn.execute(
            "INSERT INTO point_events"
            "(id, guide_id, event_type, amount, occurred_on, source_ref, status, "
            " reversed_by, note, idempotency_key, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (e["id"], e["guide_id"], e["event_type"], e["amount"],
             e["occurred_on"], e["source_ref"], e["status"], e.get("reversed_by"),
             e.get("note", ""), e.get("idempotency_key"), e["created_at"]),
        )

    def update_event_status(self, event_id: str, expected: str, new_status: str,
                            reversed_by: str | None) -> bool:
        cur = self.conn.execute(
            "UPDATE point_events SET status = ?, reversed_by = COALESCE(?, reversed_by) "
            "WHERE id = ? AND status = ?",
            (new_status, reversed_by, event_id, expected),
        )
        return cur.rowcount == 1

    def get_event_row(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM point_events WHERE id = ?", (event_id,)
        ).fetchone()

    def list_event_rows(self, guide_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM point_events WHERE guide_id = ? ORDER BY created_at, id",
            (guide_id,)))

    def find_event_by_idem(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM point_events WHERE idempotency_key = ?", (key,)
        ).fetchone()

    def insert_entry(self, e: dict) -> None:
        self.conn.execute(
            "INSERT INTO point_entries"
            "(id, event_id, guide_id, account, amount, occurred_on, created_at, "
            " reversal_of, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (e["id"], e["event_id"], e["guide_id"], e["account"], e["amount"],
             e["occurred_on"], e["created_at"], e.get("reversal_of"),
             e.get("reason", "")),
        )

    def list_entry_rows(self, guide_id: str | None = None) -> list[sqlite3.Row]:
        if guide_id is None:
            return list(self.conn.execute(
                "SELECT * FROM point_entries ORDER BY created_at, id"))
        return list(self.conn.execute(
            "SELECT * FROM point_entries WHERE guide_id = ? ORDER BY created_at, id",
            (guide_id,)))

    def list_entries_for_event(self, event_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM point_entries WHERE event_id = ? ORDER BY account", (event_id,)))

    # ---------- 等级变动 ----------

    def next_movement_sequence(self, guide_id: str) -> int:
        return int(self.scalar(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM level_movements WHERE guide_id = ?",
            (guide_id,))) or 1

    def insert_movement(self, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO level_movements(id, guide_id, sequence, data) VALUES (?,?,?,?)",
            (data["id"], data["guide_id"], data["sequence"], dumps(data)),
        )

    def list_movement_rows(self, guide_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT data FROM level_movements WHERE guide_id = ? ORDER BY sequence",
            (guide_id,)))

    # ---------- 权益 ----------

    def insert_grant(self, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO benefit_grants(id, guide_id, data) VALUES (?,?,?)",
            (data["id"], data["guide_id"], dumps(data)),
        )

    def list_grant_rows(self, guide_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT data FROM benefit_grants WHERE guide_id = ?"
            " ORDER BY json_extract(data, '$.granted_on'), id",
            (guide_id,)))

    # ---------- 复核 ----------

    def insert_review(self, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO review_cases(id, guide_id, data) VALUES (?,?,?)",
            (data["id"], data["guide_id"], dumps(data)),
        )

    def update_review(self, review_id: str, data: dict) -> bool:
        cur = self.conn.execute(
            "UPDATE review_cases SET data = ? WHERE id = ? "
            "AND json_extract(data, '$.status') = 'OPEN'",
            (dumps(data), review_id),
        )
        return cur.rowcount == 1

    def get_review_row(self, review_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT data FROM review_cases WHERE id = ?", (review_id,)
        ).fetchone()

    def list_review_rows(self, guide_id: str | None = None) -> list[sqlite3.Row]:
        if guide_id is None:
            return list(self.conn.execute(
                "SELECT data FROM review_cases"
                " ORDER BY json_extract(data, '$.opened_at')"))
        return list(self.conn.execute(
            "SELECT data FROM review_cases WHERE guide_id = ?"
            " ORDER BY json_extract(data, '$.opened_at')",
            (guide_id,)))

    # ---------- 快照 ----------

    def next_snapshot_sequence(self, guide_id: str) -> int:
        return int(self.scalar(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM level_snapshots WHERE guide_id = ?",
            (guide_id,))) or 1

    def insert_snapshot(self, data: dict, content_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO level_snapshots(id, guide_id, sequence, content_hash, data)"
            " VALUES (?,?,?,?,?)",
            (data["id"], data["guide_id"], data["sequence"], content_hash, dumps(data)),
        )

    def find_snapshot_by_content(self, guide_id: str, content_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT data FROM level_snapshots WHERE guide_id = ? AND content_hash = ?",
            (guide_id, content_hash),
        ).fetchone()

    def list_snapshot_rows(self, guide_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT data FROM level_snapshots WHERE guide_id = ? ORDER BY sequence",
            (guide_id,)))

    # ---------- 晋级运行 ----------

    def insert_run(self, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO promotion_runs(id, batch_key, data) VALUES (?,?,?)",
            (data["id"], data["batch_key"], dumps(data)),
        )

    def update_run(self, run_id: str, data: dict) -> None:
        self.conn.execute(
            "UPDATE promotion_runs SET data = ? WHERE id = ?", (dumps(data), run_id)
        )

    def get_run_by_batch(self, batch_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT data FROM promotion_runs WHERE batch_key = ?", (batch_key,)
        ).fetchone()
