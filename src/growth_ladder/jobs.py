"""定时晋级任务（可安全重跑）。

幂等机制有两层：
1. ``job_runs.run_id`` 唯一：批次登记使用 INSERT OR IGNORE，重跑同一批次不会
   重新初始化；
2. ``level_evaluations(run_id, guide_id)`` 唯一：同批次内每个讲解员至多一条评定，
   重跑时直接返回既有评定单，不再写分录、等级区间或权益。

因此任务在任意时刻崩溃后，用相同 ``run_id`` 重跑结果完全一致；
不同批次必须使用新的 ``run_id``（默认按 as_of 时间生成）。
"""
from __future__ import annotations

from .engine import Engine
from .store import Store
from .util import json_loads, now_iso, parse_iso, to_iso

JOB_NAME = "scheduled_level_evaluation"


class JobRunner:
    def __init__(self, store: Store, engine: Engine):
        self.store = store
        self.engine = engine

    def run_promotion_cycle(
        self,
        *,
        as_of: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        as_of = to_iso(as_of, default_now=True)
        rule = self.engine.rules.current()
        if run_id is None:
            stamp = parse_iso(as_of).strftime("%Y%m%dT%H%M%SZ")
            run_id = f"cycle-{stamp}"

        with self.store.tx():
            is_new = self.store.start_run(
                {
                    "run_id": run_id,
                    "job_name": JOB_NAME,
                    "as_of": as_of,
                    "rule_version": rule["version"],
                    "started_at": now_iso(),
                }
            )
            existing = self.store.get_run(run_id)
            if not is_new and existing["status"] == "completed":
                # 已完成批次的重跑：原样回放结果，不产生任何写入。
                return self._run_dto(existing, replayed=True)
            # 批次不存在 → 新建；批次停留在 running（上次崩溃）→ 从断点继续，
            # 已写入的评定由 (run_id, guide_id) 唯一约束保证不重复。

            guides = self.store.list_guides()
            evaluated = changed = held = 0
            per_guide: list[dict] = []
            for guide in guides:
                result = self.engine.evaluate(
                    guide["id"],
                    as_of=as_of,
                    run_id=run_id,
                    trigger_type="scheduled",
                )
                evaluated += 1
                if result["decision"] in ("promote", "demote"):
                    changed += 1
                if result["decision"] == "review":
                    held += 1
                per_guide.append(
                    {
                        "guide_id": guide["id"],
                        "decision": result["decision"],
                        "from": result["from_level_code"],
                        "to": result["to_level_code"],
                        "evaluation_id": result["evaluation_id"],
                        "effects": len(result["effects"]),
                    }
                )
            stats = {
                "evaluated_guides": evaluated,
                "changed_guides": changed,
                "held_guides": held,
                "detail": {"guides": per_guide},
            }
            self.store.finish_run(run_id, stats, now_iso())
            return self._run_dto(self.store.get_run(run_id), replayed=False)

    def list_runs(self) -> list[dict]:
        return [self._run_dto(r) for r in self.store.list_runs()]

    def get_run(self, run_id: str) -> dict | None:
        row = self.store.get_run(run_id)
        return self._run_dto(row) if row else None

    @staticmethod
    def _run_dto(row, *, replayed: bool | None = None) -> dict:
        data = {
            "run_id": row["run_id"],
            "job_name": row["job_name"],
            "as_of": row["as_of"],
            "rule_version": row["rule_version"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "evaluated_guides": row["evaluated_guides"],
            "changed_guides": row["changed_guides"],
            "held_guides": row["held_guides"],
            "detail": json_loads(row["detail_json"], {}),
        }
        if replayed is not None:
            data["replayed"] = replayed
        return data
