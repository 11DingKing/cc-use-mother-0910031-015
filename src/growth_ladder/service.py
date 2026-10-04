"""应用服务门面：聚合规则、台账、评定引擎与定时任务。"""
from __future__ import annotations

from pathlib import Path

from .engine import Engine
from .jobs import JobRunner
from .ledger import Ledger
from .rules import RuleBook
from .store import Store
from .util import now_iso, to_iso


class GrowthService:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.store = Store(db_path)
        self.rules = RuleBook(self.store)
        self.ledger = Ledger(self.store, self.rules)
        self.engine = Engine(self.store, self.rules, self.ledger)
        self.jobs = JobRunner(self.store, self.engine)
        self.rules.ensure_default()

    def close(self) -> None:
        self.store.close()

    # ---------------------------------------------------------------- guides
    def create_guide(self, guide_no: str, name: str) -> dict:
        if not guide_no or not name:
            from .errors import ValidationError

            raise ValidationError("guide_no 与 name 不能为空")
        with self.store.tx():
            if self.store.get_guide_by_no(guide_no):
                from .errors import ConflictError

                raise ConflictError(f"讲解员编号已存在：{guide_no}", code="duplicate_guide")
            gid = self.store.create_guide(guide_no, name, now_iso())
            row = self.store.get_guide(gid)
        return dict(row)

    def list_guides(self) -> list[dict]:
        return [dict(r) for r in self.store.list_guides()]

    def get_guide(self, guide_id: int) -> dict:
        from .errors import NotFoundError

        row = self.store.get_guide(guide_id)
        if row is None:
            raise NotFoundError(f"讲解员不存在：{guide_id}", code="guide_not_found")
        return dict(row)

    # ------------------------------------------------------------------ rules
    def publish_rule(self, payload: dict) -> dict:
        return self.rules.publish(
            params=payload.get("params"),
            points_policy=payload.get("points_policy"),
            levels=payload.get("levels"),
            benefits=payload.get("benefits"),
            note=payload.get("note"),
            published_at=payload.get("published_at"),
        )

    def list_rule_versions(self) -> list[dict]:
        return self.rules.list_versions()

    # ----------------------------------------------------------------- ledger
    def record_service(self, guide_id: int, payload: dict) -> dict:
        return self.ledger.record_service(
            guide_id,
            minutes=int(payload["minutes"]),
            topic_code=payload["topic_code"],
            session_uid=payload.get("session_uid"),
            started_at=payload.get("started_at"),
            created_by=payload.get("created_by"),
        )

    def record_review(self, guide_id: int, payload: dict) -> dict:
        return self.ledger.record_review(
            guide_id,
            stars=int(payload["stars"]),
            review_uid=payload.get("review_uid"),
            session_ref=payload.get("session_ref"),
            published_at=payload.get("published_at"),
            memo=payload.get("memo"),
            created_by=payload.get("created_by"),
        )

    def withdraw_review(self, review_uid: str, payload: dict | None = None) -> dict:
        return self.ledger.withdraw_review(
            review_uid, at=(payload or {}).get("at")
        )

    def correct_review(self, review_uid: str, payload: dict) -> dict:
        return self.ledger.correct_review(
            review_uid,
            new_stars=int(payload["new_stars"]),
            at=payload.get("at"),
            memo=payload.get("memo"),
        )

    def cancel_session(self, session_uid: str, payload: dict | None = None) -> dict:
        return self.ledger.cancel_session(
            session_uid, at=(payload or {}).get("at")
        )

    def manual_adjust(self, guide_id: int, payload: dict) -> dict:
        return self.ledger.manual_adjust(
            guide_id,
            points=int(payload["points"]),
            reason=payload["reason"],
            topic_code=payload.get("topic_code"),
            occurred_at=payload.get("occurred_at"),
            created_by=payload.get("created_by"),
            idempotency_key=payload.get("idempotency_key"),
        )

    def balance(self, guide_id: int, as_of: str | None = None) -> dict:
        return {
            "guide_id": guide_id,
            "as_of": to_iso(as_of) if as_of else None,
            "points_balance": self.ledger.balance(guide_id, as_of),
        }

    def metrics(self, guide_id: int, as_of: str | None = None) -> dict:
        return self.ledger.metrics(guide_id, to_iso(as_of, default_now=True))

    def statement(self, guide_id: int, as_of: str | None = None) -> dict:
        return self.ledger.statement(guide_id, as_of)

    # --------------------------------------------------------------- evaluate
    def evaluate(self, guide_id: int, payload: dict | None = None) -> dict:
        payload = payload or {}
        return self.engine.evaluate(
            guide_id,
            as_of=payload.get("as_of"),
            trigger_type=payload.get("trigger_type", "manual"),
            actor=payload.get("actor"),
        )

    def take_snapshot(self, guide_id: int, payload: dict | None = None) -> dict:
        return self.engine.take_snapshot(
            guide_id, as_of=(payload or {}).get("as_of")
        )

    def request_hold(self, guide_id: int, payload: dict) -> dict:
        return self.engine.request_hold(
            guide_id,
            reason_code=payload["reason_code"],
            reason=payload["reason"],
            evidence=payload.get("evidence"),
            requested_by=payload.get("requested_by", "operator"),
            requested_at=payload.get("requested_at"),
        )

    def resolve_hold(self, hold_id: int, payload: dict) -> dict:
        return self.engine.resolve_hold(
            hold_id,
            resolution=payload["resolution"],
            comment=payload.get("comment"),
            resolved_by=payload.get("resolved_by", "operator"),
            at=payload.get("at"),
        )

    def list_holds(self, status: str | None = None) -> list[dict]:
        return [self.engine.hold_dto(r) for r in self.store.list_holds(status)]

    def run_cycle(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        return self.jobs.run_promotion_cycle(
            as_of=payload.get("as_of"), run_id=payload.get("run_id")
        )

    # --------------------------------------------------------------- history
    def evaluations(self, guide_id: int) -> list[dict]:
        return [self.engine.evaluation_dto(r) for r in self.store.list_evaluations(guide_id)]

    def evaluation_detail(self, evaluation_id: int) -> dict:
        from .errors import NotFoundError

        row = self.store.get_evaluation(evaluation_id)
        if row is None:
            raise NotFoundError(f"评定单不存在：{evaluation_id}", code="evaluation_not_found")
        return self.engine.evaluation_dto(row)

    def assignments(self, guide_id: int) -> list[dict]:
        return [dict(r) for r in self.store.list_assignments(guide_id)]

    def grants(self, guide_id: int) -> list[dict]:
        return [dict(r) for r in self.store.list_grants(guide_id)]

    def snapshots(self, guide_id: int) -> list[dict]:
        return [dict(r) for r in self.store.list_snapshots(guide_id)]

    def verify_snapshot_chain(self, guide_id: int) -> dict:
        self.get_guide(guide_id)
        return self.store.verify_snapshot_chain(guide_id)

    def runs(self) -> list[dict]:
        return self.jobs.list_runs()

    def integrity(self) -> dict:
        return self.store.integrity_report()

    def explain(self, guide_id: int) -> dict:
        """讲解员当前状态的完整解释：余额、指标、等级、最近评定、权益来源。"""
        from .errors import NotFoundError

        guide = self.get_guide(guide_id)
        rule = self.rules.current()
        assignment = self.store.current_assignment(guide_id)
        latest = self.store.latest_evaluation(guide_id)
        grants = self.store.list_grants(guide_id)
        return {
            "guide": guide,
            "rule_version": rule["version"],
            "current_level": assignment["level_code"] if assignment else rule["levels"][0]["code"],
            "level_since": assignment["effective_from"] if assignment else None,
            "points_balance": self.store.balance_at(guide_id),
            "latest_evaluation": self.engine.evaluation_dto(latest) if latest else None,
            "benefit_grants": [
                {
                    "benefit_code": g["benefit_code"],
                    "benefit_name": g["benefit_name"],
                    "status": g["status"],
                    "level_code": g["level_code"],
                    "granted_at": g["granted_at"],
                    "granted_evaluation_id": g["granted_evaluation_id"],
                    "revoked_at": g["revoked_at"],
                    "revoked_evaluation_id": g["revoked_evaluation_id"],
                    "revoke_reason": g["revoke_reason"],
                }
                for g in grants
            ],
        }
