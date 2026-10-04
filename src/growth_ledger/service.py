"""应用服务：用事务把存储层与纯计算引擎串起来。

全部写操作在单个 SQLite 事务内完成，提交前强制守恒校验；
任何不平衡都会抛 LedgerUnbalancedError 并整体回滚。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from . import engine
from .errors import ConflictError, DomainError, LedgerUnbalancedError, NotFoundError
from .models import (
    BenefitGrant,
    GrowthRule,
    Guide,
    LevelBand,
    LevelMovement,
    LevelSnapshot,
    PointEntry,
    PointEvent,
    PromotionRun,
    ReviewCase,
    asdict,
)
from .store import Store, loads


def _now() -> datetime:
    return datetime.now().astimezone()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class GrowthService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ========== 规则版本 ==========

    def publish_rule(self, bands: list[dict], *, effective_from: date,
                     published_by: str, note: str = "",
                     version: int | None = None) -> dict:
        """发布成长规则新版本。已生效版本不可修改，只能发新版本。"""
        existing = self.store.list_rule_versions()
        if version is None:
            version = (max(existing) + 1) if existing else 1
        if version in existing:
            raise ConflictError(f"规则版本 v{version} 已存在", code="rule_version_exists")
        rule = GrowthRule(
            version=version,
            published_at=_now(),
            effective_from=effective_from,
            published_by=published_by,
            bands=tuple(
                LevelBand(
                    level=b["level"], name=b["name"], min_points=b["min_points"],
                    protection_days=b.get("protection_days", 0),
                    benefits=tuple(b.get("benefits", [])),
                ) for b in bands
            ),
            note=note,
        )
        rule.validate()
        if existing:
            prev = self._load_rule(max(existing))
            if rule.effective_from < prev.effective_from:
                raise DomainError("新版本生效日不得早于已发布版本")
        data = asdict(rule)
        self.store.begin()
        try:
            self.store.insert_rule(version, data)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return data

    def _load_rule(self, version: int) -> GrowthRule:
        row = self.store.get_rule_row(version)
        if row is None:
            raise NotFoundError(f"规则版本 v{version} 不存在", code="rule_not_found")
        d = loads(row["data"])
        return GrowthRule(
            version=d["version"],
            published_at=datetime.fromisoformat(d["published_at"]),
            effective_from=date.fromisoformat(d["effective_from"]),
            published_by=d["published_by"],
            bands=tuple(LevelBand(**b) for b in d["bands"]),
            note=d.get("note", ""),
        )

    def _rule_on(self, on_date: date) -> GrowthRule | None:
        version = self.store.rule_version_for(on_date)
        return self._load_rule(version) if version else None

    def list_rules(self) -> list[dict]:
        return [asdict(self._load_rule(v)) for v in self.store.list_rule_versions()]

    # ========== 讲解员 ==========

    def register_guide(self, guide_id: str, name: str) -> dict:
        if self.store.get_guide_row(guide_id) is not None:
            raise ConflictError("讲解员已存在", code="guide_exists")
        today = _now().date()
        rule = self._rule_on(today)
        if rule is None:
            raise DomainError("尚未发布生效的成长规则，无法建档", code="no_rule")
        guide = Guide(id=guide_id, name=name, created_at=_now(),
                      initial_level=rule.bands[0].level)
        self.store.begin()
        try:
            self.store.insert_guide(asdict(guide))
            # 建账分录取最小区间，授予初始权益，历史从第 1 条流水开始完整可溯。
            self._write_movement(
                guide_id, rule, today,
                from_level=rule.bands[0].level, to_level=rule.bands[0].level,
                points_before=0, points_after=0,
                reason=engine.REASON_INITIAL, triggered_by="system",
                trigger_event_id=None, protection_until=None,
                explanation=f"建档定级：{rule.bands[0].name}（规则 v{rule.version}）",
                grant_target_level=rule.bands[0].level,
            )
            self._write_snapshot(guide_id, rule, today)
            self._assert_conservation(guide_id)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return asdict(guide)

    def _get_guide(self, guide_id: str) -> dict:
        row = self.store.get_guide_row(guide_id)
        if row is None:
            raise NotFoundError("讲解员不存在", code="guide_not_found")
        return loads(row["data"])

    def list_guides(self) -> list[str]:
        return self.store.list_guide_ids()

    # ========== 积分事件 ==========

    def post_event(self, guide_id: str, event_type: str, amount: int, *,
                   occurred_on: date | None = None, source_ref: str | None = None,
                   note: str = "", idempotency_key: str | None = None,
                   _status: str = "POSTED") -> dict:
        """
        登记积分事件并写平衡分录：EARNED -n / AVAILABLE +n。
        source_ref 为外部单据号（如评价单），同一讲解员不可重复。
        """
        self._get_guide(guide_id)
        if idempotency_key:
            existed = self.store.find_event_by_idem(idempotency_key)
            if existed is not None:
                return self._event_row_to_dict(existed)
        self.store.begin()
        try:
            result = self._post_event_locked(
                guide_id, event_type, amount,
                occurred_on=occurred_on, source_ref=source_ref, note=note,
                idempotency_key=idempotency_key, _status=_status)
            self._assert_conservation(guide_id)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return result

    def _post_event_locked(self, guide_id: str, event_type: str, amount: int, *,
                           occurred_on: date | None = None, source_ref: str | None = None,
                           note: str = "", idempotency_key: str | None = None,
                           _status: str = "POSTED") -> dict:
        """在已打开的事务内登记事件（供复核流程复用）。"""
        if amount <= 0:
            raise DomainError("正向事件积分必须大于 0；冲回请走冲回流程")
        occurred_on = occurred_on or _now().date()
        source_ref = source_ref or f"{event_type}-{uuid.uuid4().hex[:12]}"
        event = PointEvent(
            id=_new_id("EVT"), guide_id=guide_id, event_type=event_type,
            amount=amount, occurred_on=occurred_on.isoformat(),
            created_at=_now().isoformat(), source_ref=source_ref,
            status=_status, note=note, idempotency_key=idempotency_key,
        )
        event_dict = asdict(event)
        self.store.insert_event(event_dict)
        self._insert_earning_entries(event_dict, amount, occurred_on,
                                     reversal_of=None, reason=note)
        self._assert_event_balanced(event.id)
        return event_dict

    def _insert_earning_entries(self, event_dict: dict, signed_amount: int,
                                on_date: date, *, reversal_of: str | None,
                                reason: str) -> None:
        """写一对平衡分录；冲回时 signed_amount 为负、reversal_of 指向原事件。"""
        eid = event_dict["id"]
        gid = event_dict["guide_id"]
        ts = _now().isoformat()
        on = on_date.isoformat()
        if reversal_of is None:
            # 正向事件：收入贷方 -n，可用借方 +n。
            self.store.insert_entry({
                "id": _new_id("ENT"), "event_id": eid, "guide_id": gid,
                "account": engine.ACCOUNT_EARNED, "amount": -signed_amount,
                "occurred_on": on, "created_at": ts,
                "reversal_of": None, "reason": reason,
            })
            self.store.insert_entry({
                "id": _new_id("ENT"), "event_id": eid, "guide_id": gid,
                "account": engine.ACCOUNT_AVAILABLE, "amount": signed_amount,
                "occurred_on": on, "created_at": ts,
                "reversal_of": None, "reason": reason,
            })
        else:
            # 红冲冲回：可用借方 -n，冲回抵减科目（REVERSED）借方 +n。
            # 毛收入 EARNED 保持不变，历史可核对；
            # 恒等式 AVAILABLE + EARNED + REVERSED == 0 仍然成立。
            originals = self.store.list_entries_for_event(reversal_of)
            by_account = {e["account"]: e for e in originals}
            self.store.insert_entry({
                "id": _new_id("ENT"), "event_id": eid, "guide_id": gid,
                "account": engine.ACCOUNT_AVAILABLE, "amount": -signed_amount,
                "occurred_on": on, "created_at": ts,
                "reversal_of": by_account[engine.ACCOUNT_AVAILABLE]["id"],
                "reason": reason,
            })
            self.store.insert_entry({
                "id": _new_id("ENT"), "event_id": eid, "guide_id": gid,
                "account": engine.ACCOUNT_REVERSED, "amount": signed_amount,
                "occurred_on": on, "created_at": ts,
                "reversal_of": by_account[engine.ACCOUNT_EARNED]["id"],
                "reason": reason,
            })

    def list_events(self, guide_id: str) -> list[dict]:
        self._get_guide(guide_id)
        return [self._event_row_to_dict(r) for r in self.store.list_event_rows(guide_id)]

    def list_entries(self, guide_id: str) -> list[dict]:
        self._get_guide(guide_id)
        return [dict(r) for r in self.store.list_entry_rows(guide_id)]

    @staticmethod
    def _event_row_to_dict(row: Any) -> dict:
        return {k: row[k] for k in row.keys()}

    # ========== 冲回（内部，复核通过后调用；冲回发生在“当日”，不回改历史） ==========

    def _reverse_event(self, guide_id: str, original_event_id: str, *,
                       triggered_by: str, note: str, on_date: date | None = None) -> dict:
        original = self.store.get_event_row(original_event_id)
        if original is None or original["guide_id"] != guide_id:
            raise NotFoundError("被冲回事件不存在", code="event_not_found")
        if original["status"] != "POSTED":
            raise ConflictError(f"事件状态为 {original['status']}，不可冲回",
                                code="event_not_reversible")
        on_date = on_date or _now().date()
        reversal = PointEvent(
            id=_new_id("EVT"), guide_id=guide_id,
            event_type=f"{original['event_type']}_REVERSAL",
            amount=-original["amount"],
            occurred_on=on_date.isoformat(),
            created_at=_now().isoformat(),
            source_ref=f"{original['source_ref']}#RV",
            status="POSTED",
            note=note or f"冲回 {original['source_ref']}",
        )
        reversal_dict = asdict(reversal)
        if not self.store.update_event_status(original_event_id, "POSTED", "REVERSED",
                                              reversal.id):
            raise ConflictError("事件状态已被其他操作变更", code="event_status_race")
        self.store.insert_event(reversal_dict)
        self._insert_earning_entries(reversal_dict, original["amount"], on_date,
                                     reversal_of=original_event_id, reason=note)
        self._assert_event_balanced(reversal.id)
        return reversal_dict

    # ========== 人工复核 ==========

    def open_review(self, guide_id: str, kind: str, reason: str, *,
                    opened_by: str, event_id: str | None = None,
                    payload: dict | None = None) -> dict:
        self._get_guide(guide_id)
        if kind not in {"RATING_CORRECTION", "POINT_REVERSAL",
                        "LEVEL_OVERRIDE", "PROTECTION_DISPUTE"}:
            raise DomainError("未知复核类型")
        if kind in {"RATING_CORRECTION", "POINT_REVERSAL"} and not event_id:
            raise DomainError(f"{kind} 必须指定 event_id")
        case = ReviewCase(
            id=_new_id("REV"), guide_id=guide_id, event_id=event_id, kind=kind,
            status="OPEN", reason=reason, opened_by=opened_by,
            opened_at=_now().isoformat(), payload=payload or {},
        )
        data = asdict(case)
        self.store.begin()
        try:
            self.store.insert_review(data)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return data

    def decide_review(self, review_id: str, *, approved: bool, decided_by: str,
                      decision_note: str = "") -> dict:
        row = self.store.get_review_row(review_id)
        if row is None:
            raise NotFoundError("复核单不存在", code="review_not_found")
        case = ReviewCase(**loads(row["data"]))
        if case.status != "OPEN":
            raise ConflictError("复核单已结案", code="review_closed")
        decided = ReviewCase(
            **{**asdict(case),
               "status": "APPROVED" if approved else "REJECTED",
               "decided_by": decided_by, "decided_at": _now().isoformat(),
               "decision_note": decision_note}
        )
        result = asdict(decided)
        self.store.begin()
        try:
            if not self.store.update_review(review_id, result):
                raise ConflictError("复核单并发结案冲突", code="review_status_race")
            if approved:
                effective_on_value = case.payload.get("effective_on")
                today = (date.fromisoformat(effective_on_value)
                         if effective_on_value else _now().date())
                trigger_event = None
                if case.kind == "POINT_REVERSAL":
                    trigger_event = self._reverse_event(
                        case.guide_id, case.event_id, triggered_by=decided_by,
                        note=f"复核 {review_id} 通过：{decision_note or case.reason}",
                        on_date=today)
                elif case.kind == "RATING_CORRECTION":
                    corrected = int(case.payload.get("corrected_amount", 0))
                    if corrected < 0:
                        raise DomainError("更正后积分不能为负")
                    trigger_event = self._reverse_event(
                        case.guide_id, case.event_id, triggered_by=decided_by,
                        note=f"评价更正冲回（复核 {review_id}）", on_date=today)
                    if corrected > 0:
                        original = self.store.get_event_row(case.event_id)
                        repost = self._post_event_locked(
                            case.guide_id, original["event_type"], corrected,
                            occurred_on=today,
                            source_ref=f"{original['source_ref']}#C2",
                            note=f"评价更正后重记（复核 {review_id}）")
                        trigger_event["repost_event_id"] = repost["id"]
                # 复核引发即时重算：保护期仍生效时只会 HELD，不会被冲回直接降级。
                force_level = None
                if case.kind == "LEVEL_OVERRIDE":
                    force_level = int(case.payload.get("to_level"))
                self._evaluate_and_apply(
                    case.guide_id, today, reason=engine.REASON_REVIEW,
                    triggered_by=decided_by,
                    trigger_event_id=trigger_event["id"] if trigger_event else case.event_id,
                    force_level=force_level)
            self._assert_conservation(case.guide_id)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return result

    def list_reviews(self, guide_id: str | None = None) -> list[dict]:
        return [loads(r["data"]) for r in self.store.list_review_rows(guide_id)]

    # ========== 等级评估（写流水 + 权益 + 快照） ==========

    def _load_facts(self, guide_id: str):
        entries = [dict(r) for r in self.store.list_entry_rows(guide_id)]
        movements = [loads(r["data"]) for r in self.store.list_movement_rows(guide_id)]
        grants = [loads(r["data"]) for r in self.store.list_grant_rows(guide_id)]
        events = [self._event_row_to_dict(r) for r in self.store.list_event_rows(guide_id)]
        return entries, movements, grants, events

    def _evaluate_and_apply(self, guide_id: str, on_date: date, *, reason: str,
                            triggered_by: str, trigger_event_id: str | None,
                            force_level: int | None = None) -> dict | None:
        rule = self._rule_on(on_date)
        if rule is None:
            raise DomainError("当日无生效规则", code="no_rule")
        entries, movements, grants, events = self._load_facts(guide_id)
        projected = engine.project_state(
            entries, movements, grants, asdict(rule), on_date.isoformat())
        current = projected["level"]
        candidate = projected["candidate_level"]
        target = force_level if force_level is not None else candidate
        if target is None:
            return None

        if force_level is not None and not any(
                b.level == force_level for b in rule.bands):
            raise DomainError(f"规则 v{rule.version} 中不存在等级 {force_level}")
        held_by_protection = (
            force_level is None
            and target < current and projected["protection_active"]
        )
        # 同日已做过评估且等级未变（如复核评估后定时任务同日重跑），
        # 不再重复写 HOLD/定级分录，只核对快照；返回已有分录供调用方统计。
        if (force_level is None and movements
                and movements[-1]["effective_on"] == on_date.isoformat()
                and movements[-1]["to_level"] == current):
            self._write_snapshot(guide_id, rule, on_date)
            return {"movement": movements[-1], "duplicate": True}
        if target == current and not held_by_protection:
            self._write_snapshot(guide_id, rule, on_date)
            return None

        if held_by_protection:
            # 保护期内的“暂不降级”也是一条可核对的决策分录。
            movement = self._write_movement(
                guide_id, rule, on_date,
                from_level=current, to_level=current,
                points_before=projected["points_available"],
                points_after=projected["points_available"],
                reason=engine.REASON_HOLD, triggered_by=triggered_by,
                trigger_event_id=trigger_event_id,
                protection_until=projected["protection_until"],
                explanation=engine.explain_movement(
                    rule=asdict(rule), from_level=current, to_level=current,
                    points_before=projected["points_available"],
                    points_after=projected["points_available"],
                    as_of=on_date.isoformat(), events=events, entries=entries,
                    reason=engine.REASON_HOLD, trigger=triggered_by,
                    protection_until=projected["protection_until"]),
                grant_target_level=None)
        else:
            target_band = next(b for b in rule.bands if b.level == target)
            direction_reason = reason
            if reason == engine.REASON_SCHEDULED:
                direction_reason = (engine.REASON_DEMOTION if target < current
                                    else engine.REASON_SCHEDULED)
            protection_until = (engine.protection_end(on_date, target_band.protection_days)
                                if target != current else projected["protection_until"])
            movement = self._write_movement(
                guide_id, rule, on_date,
                from_level=current, to_level=target,
                points_before=projected["points_available"],
                points_after=projected["points_available"],
                reason=direction_reason, triggered_by=triggered_by,
                trigger_event_id=trigger_event_id,
                protection_until=protection_until,
                explanation=engine.explain_movement(
                    rule=asdict(rule), from_level=current, to_level=target,
                    points_before=projected["points_available"],
                    points_after=projected["points_available"],
                    as_of=on_date.isoformat(), events=events, entries=entries,
                    reason=direction_reason, trigger=triggered_by,
                    protection_until=protection_until),
                grant_target_level=target)
        self._write_snapshot(guide_id, rule, on_date)
        return {"movement": movement, "duplicate": False}

    def _write_movement(self, guide_id: str, rule: GrowthRule, on_date: date, *,
                        from_level: int, to_level: int, points_before: int,
                        points_after: int, reason: str, triggered_by: str,
                        trigger_event_id: str | None, protection_until: date | None,
                        explanation: str, grant_target_level: int | None) -> dict:
        if isinstance(protection_until, str):
            protection_until = date.fromisoformat(protection_until)
        sequence = self.store.next_movement_sequence(guide_id)
        movement = LevelMovement(
            id=_new_id("MOV"), guide_id=guide_id, sequence=sequence,
            rule_version=rule.version, from_level=from_level, to_level=to_level,
            points_before=points_before, points_after=points_after, reason=reason,
            triggered_by=triggered_by, trigger_event_id=trigger_event_id,
            effective_on=on_date.isoformat(), created_at=_now().isoformat(),
            protection_until=protection_until.isoformat() if protection_until else None,
            explanation=explanation,
        )
        data = asdict(movement)
        self.store.insert_movement(data)

        if grant_target_level is not None:
            grants = [loads(r["data"]) for r in self.store.list_grant_rows(guide_id)]
            active = engine.active_benefit_set(grants, on_date.isoformat())
            band = next(b for b in rule.bands if b.level == grant_target_level)
            to_grant, to_revoke = engine.reconcile_benefits(active, set(band.benefits))
            for benefit in to_grant:
                self.store.insert_grant(asdict(BenefitGrant(
                    id=_new_id("BNF"), guide_id=guide_id, benefit=benefit,
                    action="GRANT", level=to_level, movement_id=movement.id,
                    granted_on=on_date.isoformat(), created_at=_now().isoformat(),
                    reason=reason,
                    explanation=(f"等级 {from_level}→{to_level}（{reason}），"
                                 f"按规则 v{rule.version} 授予权益 {benefit}；"
                                 f"来源分录 {movement.id}"))))
            for benefit in to_revoke:
                self.store.insert_grant(asdict(BenefitGrant(
                    id=_new_id("BNF"), guide_id=guide_id, benefit=benefit,
                    action="REVOKE", level=to_level, movement_id=movement.id,
                    granted_on=on_date.isoformat(), created_at=_now().isoformat(),
                    reason=reason,
                    explanation=(f"等级 {from_level}→{to_level}（{reason}），"
                                 f"按规则 v{rule.version} 撤销权益 {benefit}；"
                                 f"来源分录 {movement.id}"))))
        return data

    def _write_snapshot(self, guide_id: str, rule: GrowthRule, on_date: date) -> dict:
        entries, movements, grants, _ = self._load_facts(guide_id)
        projected = engine.project_state(
            entries, movements, grants, asdict(rule), on_date.isoformat())
        prev_rows = self.store.list_snapshot_rows(guide_id)
        prev_data = loads(prev_rows[-1]["data"]) if prev_rows else None
        payload = {
            "guide_id": guide_id,
            "rule_version": rule.version,
            "as_of_date": on_date.isoformat(),
            "points_earned": projected["points_earned_gross"],
            "points_available": projected["points_available"],
            "level": projected["level"],
            "protection_until": projected["protection_until"],
            "benefits": projected["benefits"],
            "entry_count": projected["entry_count"],
        }
        content_hash = engine.snapshot_content_hash(payload)
        existed = self.store.find_snapshot_by_content(guide_id, content_hash)
        if existed is not None:
            return loads(existed["data"])
        sequence = self.store.next_snapshot_sequence(guide_id)
        prev_hash = prev_data["hash"] if prev_data else ""
        snap = LevelSnapshot(
            id=_new_id("SNP"), guide_id=guide_id, sequence=sequence,
            rule_version=rule.version, as_of_date=on_date,
            points_earned=payload["points_earned"],
            points_available=payload["points_available"],
            level=payload["level"],
            protection_until=(date.fromisoformat(payload["protection_until"])
                              if payload["protection_until"] else None),
            benefits=tuple(payload["benefits"]),
            movement_ids=tuple(m["id"] for m in movements),
            entry_count=payload["entry_count"],
            created_at=_now(),
            content_hash=content_hash,
            prev_hash=prev_hash,
            hash=engine.chain_hash(prev_hash, content_hash, sequence),
        )
        data = asdict(snap)
        self.store.insert_snapshot(data, content_hash)
        return data

    # ========== 定时晋级（幂等、可安全重跑） ==========

    def run_promotions(self, run_date: date | None = None,
                       rule_version: int | None = None) -> dict:
        run_date = run_date or _now().date()
        rule = (self._load_rule(rule_version) if rule_version
                else self._rule_on(run_date))
        if rule is None:
            raise DomainError("当日无生效规则", code="no_rule")
        batch_key = f"{run_date.isoformat()}|v{rule.version}"
        existed = self.store.get_run_by_batch(batch_key)
        if existed is not None:
            # 同一天同一规则的重跑直接返回首次结果，绝不产生重复分录。
            return {"rerun": True, **loads(existed["data"])}

        run = PromotionRun(
            id=_new_id("RUN"), run_date=run_date, rule_version=rule.version,
            status="STARTED", started_at=_now(), committed_at=None,
            processed=0, promoted=0, demoted=0, held=0, batch_key=batch_key,
        )
        run_data = asdict(run)
        self.store.begin()
        try:
            self.store.insert_run(run_data)
            guide_ids = self.store.list_guide_ids()
            details: list[dict] = []
            for gid in guide_ids:
                outcome = self._evaluate_and_apply(
                    gid, run_date, reason=engine.REASON_SCHEDULED,
                    triggered_by=f"scheduled:{run_data['id']}",
                    trigger_event_id=None)
                run_data["processed"] += 1
                if outcome is None:
                    continue
                movement = outcome["movement"]
                if movement["reason"] == engine.REASON_HOLD:
                    run_data["held"] += 1
                elif movement["to_level"] > movement["from_level"]:
                    run_data["promoted"] += 1
                elif movement["to_level"] < movement["from_level"]:
                    run_data["demoted"] += 1
                details.append({"guide_id": gid, "movement_id": movement["id"],
                                "reason": movement["reason"],
                                "from_level": movement["from_level"],
                                "to_level": movement["to_level"]})
            run_data["status"] = "COMMITTED"
            run_data["committed_at"] = _now()
            run_data["details"] = details
            self.store.update_run(run_data["id"], run_data)
            for gid in guide_ids:
                self._assert_conservation(gid)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return {"rerun": False, **run_data}

    # ========== 查询 / 解释 / 核对 ==========

    def evaluate(self, guide_id: str, as_of: date | None = None) -> dict:
        """只读：返回当日重放状态与候选等级，不写任何数据。"""
        self._get_guide(guide_id)
        as_of = as_of or _now().date()
        rule = self._rule_on(as_of)
        entries, movements, grants, events = self._load_facts(guide_id)
        projected = engine.project_state(
            entries, movements, grants, asdict(rule) if rule else None,
            as_of.isoformat())
        projected["points_breakdown"] = engine.explain_points(
            events, entries, as_of.isoformat())
        return projected

    def timeline(self, guide_id: str) -> dict:
        """讲解员完整成长档案：事件、分录、等级流水、权益、快照、守恒报告。"""
        self._get_guide(guide_id)
        entries, movements, grants, events = self._load_facts(guide_id)
        snapshots = [loads(r["data"]) for r in self.store.list_snapshot_rows(guide_id)]
        return {
            "guide_id": guide_id,
            "events": events,
            "entries": entries,
            "movements": movements,
            "benefit_grants": grants,
            "snapshots": snapshots,
            "balance": engine.balance_report(entries),
            "snapshot_violations": engine.verify_snapshots(snapshots),
        }

    def list_snapshots(self, guide_id: str) -> list[dict]:
        self._get_guide(guide_id)
        return [loads(r["data"]) for r in self.store.list_snapshot_rows(guide_id)]

    def conservation_report(self) -> dict:
        """全局余额守恒与快照哈希链核对，供巡检接口使用。"""
        entries = [dict(r) for r in self.store.list_entry_rows()]
        report = engine.balance_report(entries)
        violations: list[str] = []
        for gid in self.store.list_guide_ids():
            snaps = [loads(r["data"]) for r in self.store.list_snapshot_rows(gid)]
            violations.extend(f"{gid}: {v}" for v in engine.verify_snapshots(snaps))
            guide_entries = [e for e in entries if e["guide_id"] == gid]
            by_event: dict[str, int] = {}
            for e in guide_entries:
                by_event[e["event_id"]] = by_event.get(e["event_id"], 0) + e["amount"]
            for eid, total in by_event.items():
                if total != 0:
                    violations.append(f"{gid}: 事件 {eid} 分录合计 {total} != 0")
        report["snapshot_violations"] = violations
        report["balanced"] = report["balanced"] and not violations
        return report

    # ========== 守恒守卫 ==========

    def _assert_event_balanced(self, event_id: str) -> None:
        entries = self.store.list_entries_for_event(event_id)
        engine.check_event_balanced([dict(e) for e in entries])

    def _assert_conservation(self, guide_id: str) -> None:
        entries = [dict(r) for r in self.store.list_entry_rows(guide_id)]
        report = engine.balance_report(entries)
        if not report["balanced"]:
            raise LedgerUnbalancedError(
                f"余额守恒被破坏：{report['unbalanced_events']} / "
                f"identity={report['identity_sum']}")
        snapshots = [loads(r["data"]) for r in self.store.list_snapshot_rows(guide_id)]
        violations = engine.verify_snapshots(snapshots)
        if violations:
            raise LedgerUnbalancedError("快照哈希链异常：" + "；".join(violations))
