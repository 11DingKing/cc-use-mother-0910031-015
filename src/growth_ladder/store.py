"""SQLite 存储层。

设计要点：
- 积分采用复式记账：每事件必为一借一贷，导览员账户 ``POINTS:G{id}`` 与
  系统发行账户 ``FUND:POINTS_ISSUED`` 始终配平，全体账户余额之和恒为 0。
- 评价/服务事实只追加、不原地改写；撤回与更正用状态列 + ``changed_at`` 表达，
  因此可以按任意历史时点 ``as_of`` 重放指标。
- 晋级评定带 ``(run_id, guide_id)`` 唯一约束，定时任务重跑不会产生重复分录、
  重复等级行或重复权益。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .util import canonical_json, json_dumps, json_loads, now_iso, sha256_text

SCHEMA_VERSION = 1

FUND_ACCOUNT = "FUND:POINTS_ISSUED"


def guide_account(guide_id: int) -> str:
    return f"POINTS:G{guide_id}"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    version INTEGER PRIMARY KEY,
    published_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'published',
    params_json TEXT NOT NULL,
    points_policy_json TEXT NOT NULL,
    levels_json TEXT NOT NULL,
    benefits_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS guides (
    id INTEGER PRIMARY KEY,
    guide_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_events (
    id INTEGER PRIMARY KEY,
    event_uid TEXT NOT NULL UNIQUE,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_ref TEXT,
    status TEXT NOT NULL DEFAULT 'posted'
        CHECK (status IN ('posted','reversed','superseded')),
    reversal_of INTEGER REFERENCES point_events(id),
    memo TEXT,
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_guide_time ON point_events(guide_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_events_source ON point_events(guide_id, event_type, source_ref);

CREATE TABLE IF NOT EXISTS ledger_postings (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES point_events(id),
    entry_no INTEGER NOT NULL,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    account TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('debit','credit')),
    points INTEGER NOT NULL CHECK (points > 0),
    topic_code TEXT,
    reason TEXT NOT NULL,
    UNIQUE(event_id, entry_no)
);
CREATE INDEX IF NOT EXISTS ix_postings_guide_account ON ledger_postings(guide_id, account);

CREATE TABLE IF NOT EXISTS service_sessions (
    id INTEGER PRIMARY KEY,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    session_uid TEXT NOT NULL UNIQUE,
    topic_code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    minutes INTEGER NOT NULL CHECK (minutes > 0),
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('confirmed','cancelled')),
    changed_at TEXT,
    event_id INTEGER REFERENCES point_events(id),
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    review_uid TEXT NOT NULL UNIQUE,
    session_ref TEXT,
    stars INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    published_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','withdrawn','corrected')),
    changed_at TEXT,
    superseded_by INTEGER REFERENCES reviews(id),
    event_id INTEGER REFERENCES point_events(id),
    memo TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_guide_pub ON reviews(guide_id, published_at);

CREATE TABLE IF NOT EXISTS level_evaluations (
    id INTEGER PRIMARY KEY,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    rule_version INTEGER NOT NULL REFERENCES rule_versions(version),
    run_id TEXT,
    trigger_type TEXT NOT NULL
        CHECK (trigger_type IN ('scheduled','manual','manual_review')),
    as_of TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    decision TEXT NOT NULL
        CHECK (decision IN ('promote','hold','demote','warn','review')),
    from_level_code TEXT,
    to_level_code TEXT,
    warning_until TEXT,
    reasons_json TEXT NOT NULL,
    effects_json TEXT NOT NULL DEFAULT '[]',
    hold_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, guide_id)
);
CREATE INDEX IF NOT EXISTS ix_eval_guide_time ON level_evaluations(guide_id, as_of);

CREATE TABLE IF NOT EXISTS level_assignments (
    id INTEGER PRIMARY KEY,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    level_code TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    reason TEXT NOT NULL,
    evaluation_id INTEGER REFERENCES level_evaluations(id)
);
CREATE INDEX IF NOT EXISTS ix_assign_guide_current
    ON level_assignments(guide_id, effective_from);

CREATE TABLE IF NOT EXISTS review_holds (
    id INTEGER PRIMARY KEY,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','resolved')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    resolution TEXT CHECK (resolution IS NULL OR resolution IN ('confirm_demote','keep_level')),
    resolved_by TEXT,
    resolved_at TEXT,
    comment TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hold_pending
    ON review_holds(guide_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS benefit_grants (
    id INTEGER PRIMARY KEY,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    benefit_code TEXT NOT NULL,
    benefit_name TEXT NOT NULL,
    level_code TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','revoked')),
    granted_at TEXT NOT NULL,
    granted_evaluation_id INTEGER REFERENCES level_evaluations(id),
    revoked_at TEXT,
    revoked_evaluation_id INTEGER REFERENCES level_evaluations(id),
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_grants_guide_status ON benefit_grants(guide_id, status);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY,
    guide_id INTEGER NOT NULL REFERENCES guides(id),
    as_of TEXT NOT NULL,
    points_balance INTEGER NOT NULL,
    level_code TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    benefits_json TEXT NOT NULL,
    posting_last_id INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    evaluation_id INTEGER,
    prev_digest TEXT,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_guide_asof
    ON account_snapshots(guide_id, as_of);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    job_name TEXT NOT NULL,
    as_of TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    evaluated_guides INTEGER NOT NULL DEFAULT 0,
    changed_guides INTEGER NOT NULL DEFAULT 0,
    held_guides INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT
);
"""


class Store:
    """薄数据访问层；所有跨表写操作应在单个事务中完成。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        # HTTP 工作线程共用连接：关闭同线程限制，用可重入锁把每个请求/事务整体串行化。
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self._in_tx = False
        self._savepoint_seq = 0
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        """串行写事务；嵌套调用退化为 SAVEPOINT，保证整体原子性。"""
        with self.lock:
            if self._in_tx:
                self._savepoint_seq += 1
                sp = f"sp_{self._savepoint_seq}"
                self.conn.execute(f"SAVEPOINT {sp}")
                try:
                    yield self.conn
                    self.conn.execute(f"RELEASE SAVEPOINT {sp}")
                except Exception:
                    self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    self.conn.execute(f"RELEASE SAVEPOINT {sp}")
                    raise
                return
            self._in_tx = True
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    # ------------------------------------------------------------------ rules
    def insert_rule_version(
        self,
        *,
        version: int,
        params: dict,
        points_policy: dict,
        levels: list[dict],
        benefits: list[dict],
        published_at: str,
        note: str | None = None,
    ) -> str:
        content = canonical_json(
            {
                "params": params,
                "points_policy": points_policy,
                "levels": levels,
                "benefits": benefits,
            }
        )
        digest = sha256_text(content)
        self.conn.execute(
            """INSERT INTO rule_versions
               (version, published_at, params_json, points_policy_json,
                levels_json, benefits_json, content_hash, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                version,
                published_at,
                json_dumps(params),
                json_dumps(points_policy),
                json_dumps(levels),
                json_dumps(benefits),
                digest,
                note,
            ),
        )
        return digest

    def get_rule_version(self, version: int | None = None) -> dict | None:
        if version is None:
            row = self.conn.execute(
                "SELECT * FROM rule_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM rule_versions WHERE version=?", (version,)
            ).fetchone()
        return self._rule_row(row) if row else None

    def list_rule_versions(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM rule_versions ORDER BY version").fetchall()
        return [self._rule_row(r) for r in rows]

    @staticmethod
    def _rule_row(row: sqlite3.Row) -> dict:
        return {
            "version": row["version"],
            "published_at": row["published_at"],
            "status": row["status"],
            "params": json_loads(row["params_json"], {}),
            "points_policy": json_loads(row["points_policy_json"], {}),
            "levels": json_loads(row["levels_json"], []),
            "benefits": json_loads(row["benefits_json"], []),
            "content_hash": row["content_hash"],
            "note": row["note"],
        }

    # ----------------------------------------------------------------- guides
    def create_guide(self, guide_no: str, name: str, created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO guides(guide_no, name, created_at) VALUES (?,?,?)",
            (guide_no, name, created_at),
        )
        return cur.lastrowid

    def get_guide(self, guide_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM guides WHERE id=?", (guide_id,)).fetchone()

    def get_guide_by_no(self, guide_no: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM guides WHERE guide_no=?", (guide_no,)).fetchone()

    def list_guides(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM guides ORDER BY id").fetchall()

    # ----------------------------------------------------------------- events
    def insert_event(
        self,
        *,
        event_uid: str,
        guide_id: int,
        event_type: str,
        occurred_at: str,
        recorded_at: str,
        source_ref: str | None,
        status: str = "posted",
        reversal_of: int | None = None,
        memo: str | None = None,
        created_by: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO point_events
               (event_uid, guide_id, event_type, occurred_at, recorded_at,
                source_ref, status, reversal_of, memo, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                event_uid,
                guide_id,
                event_type,
                occurred_at,
                recorded_at,
                source_ref,
                status,
                reversal_of,
                memo,
                created_by,
            ),
        )
        return cur.lastrowid

    def get_event(self, event_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM point_events WHERE id=?", (event_id,)
        ).fetchone()

    def get_event_by_uid(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM point_events WHERE event_uid=?", (uid,)
        ).fetchone()

    def find_event(self, guide_id: int, event_type: str, source_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM point_events WHERE guide_id=? AND event_type=? AND source_ref=?",
            (guide_id, event_type, source_ref),
        ).fetchone()

    def update_event_status(self, event_id: int, status: str) -> None:
        self.conn.execute("UPDATE point_events SET status=? WHERE id=?", (status, event_id))

    def list_events(self, guide_id: int, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM point_events WHERE guide_id=? ORDER BY occurred_at, id LIMIT ?",
            (guide_id, limit),
        ).fetchall()

    def insert_postings(self, event_id: int, guide_id: int, lines: list[dict]) -> None:
        """lines: [{account,direction,points,topic_code,reason}, ...]，要求借贷配平。"""
        debit = sum(l["points"] for l in lines if l["direction"] == "debit")
        credit = sum(l["points"] for l in lines if l["direction"] == "credit")
        if not lines or debit != credit or debit == 0:
            raise ValueError("分录必须借贷相等且金额为正")
        self.conn.executemany(
            """INSERT INTO ledger_postings
               (event_id, entry_no, guide_id, account, direction, points, topic_code, reason)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    event_id,
                    idx + 1,
                    guide_id,
                    l["account"],
                    l["direction"],
                    l["points"],
                    l.get("topic_code"),
                    l["reason"],
                )
                for idx, l in enumerate(lines)
            ],
        )

    def list_postings(self, event_ids: Iterable[int]) -> list[sqlite3.Row]:
        ids = list(event_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return self.conn.execute(
            f"SELECT * FROM ledger_postings WHERE event_id IN ({placeholders}) ORDER BY event_id, entry_no",
            ids,
        ).fetchall()

    # --------------------------------------------------------------- sessions
    def insert_session(
        self,
        *,
        guide_id: int,
        uid: str,
        topic_code: str,
        started_at: str,
        minutes: int,
        event_id: int | None,
        recorded_at: str,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO service_sessions
               (guide_id, session_uid, topic_code, started_at, minutes, event_id, recorded_at)
               VALUES (?,?,?,?,?,?,?)""",
            (guide_id, uid, topic_code, started_at, minutes, event_id, recorded_at),
        )
        return cur.lastrowid

    def get_session_by_uid(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM service_sessions WHERE session_uid=?", (uid,)
        ).fetchone()

    def point_in_time_sessions(self, guide_id: int, as_of: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM service_sessions
               WHERE guide_id=? AND started_at<=?
                 AND (status='confirmed' OR changed_at IS NULL OR changed_at>?)""",
            (guide_id, as_of, as_of),
        ).fetchall()

    # ---------------------------------------------------------------- reviews
    def insert_review(
        self,
        *,
        guide_id: int,
        uid: str,
        stars: int,
        published_at: str,
        session_ref: str | None,
        event_id: int | None,
        memo: str | None,
        recorded_at: str,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO reviews
               (guide_id, review_uid, session_ref, stars, published_at,
                event_id, memo, recorded_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (guide_id, uid, session_ref, stars, published_at, event_id, memo, recorded_at),
        )
        return cur.lastrowid

    def get_review_by_uid(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM reviews WHERE review_uid=?", (uid,)
        ).fetchone()

    def mark_review_changed(
        self, review_id: int, status: str, changed_at: str, superseded_by: int | None
    ) -> None:
        self.conn.execute(
            "UPDATE reviews SET status=?, changed_at=?, superseded_by=? WHERE id=?",
            (status, changed_at, superseded_by, review_id),
        )

    def point_in_time_reviews(self, guide_id: int, as_of: str) -> list[sqlite3.Row]:
        """as_of 时点仍然有效的评价（含之后才被撤回/更正的旧评价）。"""
        return self.conn.execute(
            """SELECT * FROM reviews
               WHERE guide_id=? AND published_at<=?
                 AND (status='active' OR changed_at IS NULL OR changed_at>?)""",
            (guide_id, as_of, as_of),
        ).fetchall()

    # ------------------------------------------------------------ evaluations
    def insert_evaluation(self, data: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO level_evaluations
               (guide_id, rule_version, run_id, trigger_type, as_of, metrics_json,
                decision, from_level_code, to_level_code, warning_until,
                reasons_json, effects_json, hold_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["guide_id"],
                data["rule_version"],
                data.get("run_id"),
                data["trigger_type"],
                data["as_of"],
                json_dumps(data["metrics"]),
                data["decision"],
                data.get("from_level_code"),
                data.get("to_level_code"),
                data.get("warning_until"),
                json_dumps(data.get("reasons", [])),
                json_dumps(data.get("effects", [])),
                data.get("hold_id"),
                data.get("created_at", now_iso()),
            ),
        )
        return cur.lastrowid

    def get_evaluation(self, evaluation_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM level_evaluations WHERE id=?", (evaluation_id,)
        ).fetchone()

    def latest_evaluation(self, guide_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM level_evaluations WHERE guide_id=? ORDER BY as_of DESC, id DESC LIMIT 1",
            (guide_id,),
        ).fetchone()

    def list_evaluations(self, guide_id: int, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM level_evaluations WHERE guide_id=? ORDER BY as_of, id LIMIT ?",
            (guide_id, limit),
        ).fetchall()

    # ------------------------------------------------------------- assignments
    def insert_assignment(self, data: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO level_assignments
               (guide_id, level_code, rule_version, effective_from, reason, evaluation_id)
               VALUES (?,?,?,?,?,?)""",
            (
                data["guide_id"],
                data["level_code"],
                data["rule_version"],
                data["effective_from"],
                data["reason"],
                data.get("evaluation_id"),
            ),
        )
        return cur.lastrowid

    def close_open_assignments(self, guide_id: int, effective_to: str) -> None:
        self.conn.execute(
            "UPDATE level_assignments SET effective_to=? WHERE guide_id=? AND effective_to IS NULL",
            (effective_to, guide_id),
        )

    def current_assignment(self, guide_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM level_assignments WHERE guide_id=? AND effective_to IS NULL",
            (guide_id,),
        ).fetchone()

    def list_assignments(self, guide_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM level_assignments WHERE guide_id=? ORDER BY effective_from, id",
            (guide_id,),
        ).fetchall()

    # ------------------------------------------------------------------- holds
    def insert_hold(self, data: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO review_holds
               (guide_id, reason_code, reason, evidence_json, status,
                requested_by, requested_at)
               VALUES (?,?,?,?,'pending',?,?)""",
            (
                data["guide_id"],
                data["reason_code"],
                data["reason"],
                json_dumps(data.get("evidence", {})),
                data.get("requested_by", "system"),
                data["requested_at"],
            ),
        )
        return cur.lastrowid

    def pending_hold(self, guide_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM review_holds WHERE guide_id=? AND status='pending'",
            (guide_id,),
        ).fetchone()

    def list_holds(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.conn.execute(
                "SELECT * FROM review_holds WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM review_holds ORDER BY id").fetchall()

    def get_hold(self, hold_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM review_holds WHERE id=?", (hold_id,)
        ).fetchone()

    def resolve_hold(
        self, hold_id: int, resolution: str, by: str, at: str, comment: str | None
    ) -> None:
        self.conn.execute(
            """UPDATE review_holds
               SET status='resolved', resolution=?, resolved_by=?, resolved_at=?, comment=?
               WHERE id=?""",
            (resolution, by, at, comment, hold_id),
        )

    # --------------------------------------------------------------- benefits
    def grant_benefit(self, data: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO benefit_grants
               (guide_id, benefit_code, benefit_name, level_code, rule_version,
                status, granted_at, granted_evaluation_id)
               VALUES (?,?,?,?,?, 'active', ?,?)""",
            (
                data["guide_id"],
                data["benefit_code"],
                data["benefit_name"],
                data["level_code"],
                data["rule_version"],
                data["granted_at"],
                data.get("granted_evaluation_id"),
            ),
        )
        return cur.lastrowid

    def revoke_benefit(self, grant_id: int, at: str, evaluation_id: int, reason: str) -> None:
        self.conn.execute(
            """UPDATE benefit_grants
               SET status='revoked', revoked_at=?, revoked_evaluation_id=?, revoke_reason=?
               WHERE id=?""",
            (at, evaluation_id, reason, grant_id),
        )

    def active_grants(self, guide_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM benefit_grants WHERE guide_id=? AND status='active' ORDER BY id",
            (guide_id,),
        ).fetchall()

    def list_grants(self, guide_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM benefit_grants WHERE guide_id=? ORDER BY id", (guide_id,)
        ).fetchall()

    # -------------------------------------------------------------- snapshots
    def latest_snapshot(self, guide_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM account_snapshots WHERE guide_id=? ORDER BY as_of DESC, id DESC LIMIT 1",
            (guide_id,),
        ).fetchone()

    def insert_snapshot(self, data: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO account_snapshots
               (guide_id, as_of, points_balance, level_code, rule_version,
                benefits_json, posting_last_id, event_count, evaluation_id,
                prev_digest, digest, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["guide_id"],
                data["as_of"],
                data["points_balance"],
                data["level_code"],
                data["rule_version"],
                json_dumps(data["benefits"]),
                data["posting_last_id"],
                data["event_count"],
                data.get("evaluation_id"),
                data.get("prev_digest"),
                data["digest"],
                data.get("created_at", now_iso()),
            ),
        )
        return cur.lastrowid

    def list_snapshots(self, guide_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM account_snapshots WHERE guide_id=? ORDER BY as_of, id",
            (guide_id,),
        ).fetchall()

    def verify_snapshot_chain(self, guide_id: int) -> dict:
        """重算每个快照的摘要哈希并校验 prev 链；任何历史篡改都会被报出。"""
        rows = self.list_snapshots(guide_id)
        prev_digest = None
        broken: list[dict] = []
        for row in rows:
            body = canonical_json(
                {
                    "guide_id": row["guide_id"],
                    "as_of": row["as_of"],
                    "points_balance": row["points_balance"],
                    "level_code": row["level_code"],
                    "rule_version": row["rule_version"],
                    "benefits": json_loads(row["benefits_json"], []),
                    "posting_last_id": row["posting_last_id"],
                    "event_count": row["event_count"],
                    "evaluation_id": row["evaluation_id"],
                    "prev_digest": prev_digest,
                }
            )
            expected = sha256_text(body)
            if row["prev_digest"] != prev_digest or row["digest"] != expected:
                broken.append({"snapshot_id": row["id"], "expected_digest": expected})
            prev_digest = row["digest"]
        return {"guide_id": guide_id, "snapshot_count": len(rows),
                "intact": not broken, "broken": broken}

    # -------------------------------------------------------------------- runs
    def start_run(self, run: dict) -> bool:
        """登记任务批次。返回 True 表示新批次，False 表示批次已存在（重跑）。"""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO job_runs
               (run_id, job_name, as_of, rule_version, status, started_at)
               VALUES (?,?,?,?,'running',?)""",
            (
                run["run_id"],
                run["job_name"],
                run["as_of"],
                run["rule_version"],
                run["started_at"],
            ),
        )
        return cur.rowcount == 1

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM job_runs WHERE run_id=?", (run_id,)
        ).fetchone()

    def finish_run(self, run_id: str, stats: dict, finished_at: str) -> None:
        self.conn.execute(
            """UPDATE job_runs
               SET status='completed', finished_at=?, evaluated_guides=?,
                   changed_guides=?, held_guides=?, detail_json=?
               WHERE run_id=?""",
            (
                finished_at,
                stats.get("evaluated_guides", 0),
                stats.get("changed_guides", 0),
                stats.get("held_guides", 0),
                json_dumps(stats.get("detail", {})),
                run_id,
            ),
        )

    def list_runs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT 50"
        ).fetchall()

    # -------------------------------------------------------------- aggregates
    def balance_at(self, guide_id: int, as_of: str | None = None) -> int:
        sql = [
            "SELECT COALESCE(SUM(CASE WHEN p.direction='credit' THEN p.points ELSE -p.points END),0) AS bal",
            "FROM ledger_postings p JOIN point_events e ON e.id=p.event_id",
            "WHERE p.guide_id=? AND p.account=?",
        ]
        params: list[Any] = [guide_id, guide_account(guide_id)]
        if as_of is not None:
            sql.append("AND e.occurred_at<=?")
            params.append(as_of)
        return self.conn.execute(" ".join(sql), params).fetchone()["bal"]

    def posting_cursor(self, guide_id: int) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(id),0) AS last_id, COUNT(*) AS cnt "
            "FROM ledger_postings WHERE guide_id=?",
            (guide_id,),
        ).fetchone()
        return row["last_id"], row["cnt"]

    def integrity_report(self) -> dict:
        """全局守恒自检：事件内借贷配平 + 系统账户与全体讲解员账户轧差为 0。"""
        per_event = self.conn.execute(
            """SELECT event_id,
                      SUM(CASE WHEN direction='debit' THEN points ELSE 0 END) AS debit,
                      SUM(CASE WHEN direction='credit' THEN points ELSE 0 END) AS credit
               FROM ledger_postings GROUP BY event_id
               HAVING debit<>credit"""
        ).fetchall()
        # 仅讲解员账户（POINTS:G 前缀）。
        guide_rows = self.conn.execute(
            """SELECT guide_id,
                      SUM(CASE WHEN direction='credit' THEN points ELSE -points END) AS bal
               FROM ledger_postings
               WHERE account LIKE 'POINTS:G%'
               GROUP BY guide_id"""
        ).fetchall()
        fund = self.conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN direction='debit' THEN points ELSE -points END),0) AS bal
               FROM ledger_postings WHERE account=?""",
            (FUND_ACCOUNT,),
        ).fetchone()["bal"]
        guide_total = sum(r["bal"] for r in guide_rows)
        negatives = [
            {"guide_id": r["guide_id"], "balance": r["bal"]}
            for r in guide_rows if r["bal"] < 0
        ]
        # 负余额是“积分欠账”的信息项，不属于配平被破坏：balanced 只看借贷守恒。
        return {
            "balanced": not per_event and guide_total == fund,
            "unbalanced_events": [
                {"event_id": r["event_id"], "debit": r["debit"], "credit": r["credit"]}
                for r in per_event
            ],
            "guide_points_total": guide_total,
            "fund_balance": fund,
            "negative_balances": negatives,
            "checked_at": now_iso(),
        }
