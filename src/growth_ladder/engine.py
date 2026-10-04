"""等级评定引擎与权益对账。

评定只读取指标与历史评定记录，**绝不改写任何积分分录**；每次评定产出一条
不可变的 ``level_evaluations``（含决策依据 reasons 与权益效果 effects），
等级变化另开一条 ``level_assignments`` 区间。保护期与预警规则：

- 晋级后 ``protection_days_after_promotion`` 天内免于降级；
- 降级后 ``protection_days_after_demotion`` 天冷静期，不连续降级；
- 不达标先给 ``demote_grace_warnings`` 次降级预警，再逐级降级；
- 存在待处理人工复核（hold）时不自动变动等级。

每次评定后写余额/等级/权益快照，快照以 prev_digest 串联，任何历史篡改
都会破坏哈希链。
"""
from __future__ import annotations

from .errors import ConflictError, NotFoundError, ValidationError
from .ledger import Ledger
from .rules import RuleBook
from .store import Store
from .util import add_days, canonical_json, json_loads, now_iso, parse_iso, sha256_text, to_iso


class Engine:
    def __init__(self, store: Store, rules: RuleBook, ledger: Ledger):
        self.store = store
        self.rules = rules
        self.ledger = ledger

    # ------------------------------------------------------------- evaluation
    def evaluate(
        self,
        guide_id: int,
        *,
        as_of: str | None = None,
        run_id: str | None = None,
        trigger_type: str = "manual",
        force: bool = False,
        manual_override: str | None = None,
        actor: str | None = None,
    ) -> dict:
        as_of = to_iso(as_of, default_now=True)
        if trigger_type not in ("scheduled", "manual", "manual_review"):
            raise ValidationError(f"未知触发类型：{trigger_type}")

        with self.store.tx():
            guide = self.store.get_guide(guide_id)
            if guide is None:
                raise NotFoundError(f"讲解员不存在：{guide_id}", code="guide_not_found")

            # 重跑保护：同一批次同一讲解员只评定一次。
            if run_id is not None:
                existing = self.store.conn.execute(
                    "SELECT id FROM level_evaluations WHERE run_id=? AND guide_id=?",
                    (run_id, guide_id),
                ).fetchone()
                if existing:
                    return self.evaluation_dto(self.store.get_evaluation(existing["id"]))

            rule = self.rules.current()
            metrics = self.ledger.metrics(guide_id, as_of)
            assignment = self.store.current_assignment(guide_id)
            from_code = assignment["level_code"] if assignment else rule["levels"][0]["code"]
            from_idx = self.rules.level_index(rule, from_code)
            eligible_idx = self._eligible_level_index(rule, metrics)
            eligible_code = rule["levels"][eligible_idx]["code"]

            reasons: list[str] = []
            warning_until = None
            hold_id = None
            decision: str
            to_code = from_code

            hold = self.store.pending_hold(guide_id)
            history = self.store.list_evaluations(guide_id, limit=10_000)

            if eligible_idx > from_idx:
                decision, to_code = "promote", eligible_code
                self._explain_promotion(rule, metrics, from_code, eligible_code, reasons)
            elif eligible_idx == from_idx:
                decision = "hold"
                reasons.append(
                    f"指标维持 {from_code}：积分 {metrics['points_balance']}、"
                    f"场次 {metrics['session_count']}、均分 {metrics['avg_stars']}、"
                    f"主题能力 {metrics['topic_capabilities']} 均满足当前等级要求"
                )
            else:
                # 可能降级：依次检查人工复核、保护期、预警次数。
                self._explain_shortfall(rule, metrics, from_idx, eligible_idx, reasons)
                if hold is not None and not force:
                    decision = "review"
                    hold_id = hold["id"]
                    reasons.append(
                        f"存在待处理人工复核 #{hold['id']}（{hold['reason_code']}），自动评定挂起"
                    )
                elif not force and self._in_promotion_protection(rule, assignment, history, as_of):
                    decision = "hold"
                    until = add_days(assignment["effective_from"],
                                     int(rule["params"]["protection_days_after_promotion"]))
                    reasons.append(f"处于晋级保护期（至 {until}），本次免于降级，等级维持 {from_code}")
                elif not force and self._in_demotion_cooldown(rule, history, as_of):
                    decision = "hold"
                    last_demote = self._last_demote(history)
                    until = add_days(last_demote["as_of"],
                                     int(rule["params"]["protection_days_after_demotion"]))
                    reasons.append(f"处于降级冷静期（至 {until}），不连续降级，等级维持 {from_code}")
                elif not force:
                    used_warnings = self._trailing_warnings(history)
                    grace = int(rule["params"]["demote_grace_warnings"])
                    if used_warnings < grace:
                        decision = "warn"
                        warning_until = add_days(as_of, 30)
                        reasons.append(
                            f"降级预警 {used_warnings + 1}/{grace}：暂维持 {from_code}，"
                            f"若下一周期仍不满足将逐级降级（目标合格等级 {eligible_code}）"
                        )
                    else:
                        decision, to_code = "demote", rule["levels"][from_idx - 1]["code"]
                        reasons.append(
                            f"预警次数已用尽（{grace} 次），逐级降级 {from_code}→{to_code}；"
                            f"按当前指标最终合格等级为 {eligible_code}"
                        )
                else:
                    # 人工复核结论：允许立即逐级降或明确维持，仍然只依据已有指标，
                    # 且不改动任何积分分录。
                    if manual_override == "keep_level":
                        decision = "hold"
                        reasons.append(
                            f"人工复核结论：维持 {from_code}（操作人 {actor or 'unknown'}），"
                            "本次豁免降级；预警计数在此中断，积分分录与权益不做改动"
                        )
                    else:
                        decision, to_code = "demote", rule["levels"][from_idx - 1]["code"]
                        reasons.append(
                            f"人工复核确认降级（操作人 {actor or 'unknown'}）：{from_code}→{to_code}，"
                            "跳过保护期与预警；积分分录不做任何改动"
                        )

            effects: list[dict] = []
            if to_code != from_code and decision in ("promote", "demote"):
                effects = self._reconcile_benefits(
                    rule, guide_id, from_code, to_code, decision, as_of
                )
                self.store.close_open_assignments(guide_id, as_of)
                assignment_id = self.store.insert_assignment(
                    {
                        "guide_id": guide_id,
                        "level_code": to_code,
                        "rule_version": rule["version"],
                        "effective_from": as_of,
                        "reason": ("晋级：" if decision == "promote" else "降级：")
                        + "；".join(reasons)[:400],
                    }
                )
            else:
                assignment_id = assignment["id"] if assignment else None

            eval_id = self.store.insert_evaluation(
                {
                    "guide_id": guide_id,
                    "rule_version": rule["version"],
                    "run_id": run_id,
                    "trigger_type": trigger_type,
                    "as_of": as_of,
                    "metrics": metrics,
                    "decision": decision,
                    "from_level_code": from_code,
                    "to_level_code": to_code,
                    "warning_until": warning_until,
                    "reasons": reasons,
                    "effects": effects,
                    "hold_id": hold_id,
                }
            )
            # 回填权益效果到评定单，保证每条授予/收回都能反查来源。
            for effect in effects:
                if effect["action"] == "granted":
                    self.store.conn.execute(
                        "UPDATE benefit_grants SET granted_evaluation_id=? WHERE id=?",
                        (eval_id, effect["grant_id"]),
                    )
                elif effect["action"] == "revoked":
                    self.store.conn.execute(
                        "UPDATE benefit_grants SET revoked_evaluation_id=? WHERE id=?",
                        (eval_id, effect["grant_id"]),
                    )
            if assignment_id is not None and to_code != from_code:
                self.store.conn.execute(
                    "UPDATE level_assignments SET evaluation_id=? WHERE id=?",
                    (eval_id, assignment_id),
                )
            snapshot = self._write_snapshot(guide_id, as_of, rule, to_code, eval_id)
            result = self.evaluation_dto(self.store.get_evaluation(eval_id))
            result["snapshot"] = snapshot
            return result

    # -------------------------------------------------------------- snapshot
    def take_snapshot(self, guide_id: int, *, as_of: str | None = None) -> dict:
        as_of = to_iso(as_of, default_now=True)
        with self.store.tx():
            if self.store.get_guide(guide_id) is None:
                raise NotFoundError(f"讲解员不存在：{guide_id}", code="guide_not_found")
            rule = self.rules.current()
            assignment = self.store.current_assignment(guide_id)
            level = assignment["level_code"] if assignment else rule["levels"][0]["code"]
            return self._write_snapshot(guide_id, as_of, rule, level, None)

    def _write_snapshot(self, guide_id: int, as_of: str, rule: dict, level_code: str,
                        evaluation_id: int | None) -> dict:
        balance = self.store.balance_at(guide_id, as_of)
        grants = self.store.active_grants(guide_id)
        benefits = [
            {
                "benefit_code": g["benefit_code"],
                "benefit_name": g["benefit_name"],
                "level_code": g["level_code"],
                "granted_at": g["granted_at"],
            }
            for g in grants
        ]
        last_id, event_count = self.store.posting_cursor(guide_id)
        prev = self.store.latest_snapshot(guide_id)
        prev_digest = prev["digest"] if prev else None
        body = canonical_json(
            {
                "guide_id": guide_id,
                "as_of": as_of,
                "points_balance": balance,
                "level_code": level_code,
                "rule_version": rule["version"],
                "benefits": benefits,
                "posting_last_id": last_id,
                "event_count": event_count,
                "evaluation_id": evaluation_id,
                "prev_digest": prev_digest,
            }
        )
        digest = sha256_text(body)
        snap_id = self.store.insert_snapshot(
            {
                "guide_id": guide_id,
                "as_of": as_of,
                "points_balance": balance,
                "level_code": level_code,
                "rule_version": rule["version"],
                "benefits": benefits,
                "posting_last_id": last_id,
                "event_count": event_count,
                "evaluation_id": evaluation_id,
                "prev_digest": prev_digest,
                "digest": digest,
            }
        )
        return {
            "snapshot_id": snap_id,
            "guide_id": guide_id,
            "as_of": as_of,
            "points_balance": balance,
            "level_code": level_code,
            "rule_version": rule["version"],
            "benefits": benefits,
            "posting_last_id": last_id,
            "event_count": event_count,
            "evaluation_id": evaluation_id,
            "prev_digest": prev_digest,
            "digest": digest,
        }

    # -------------------------------------------------------- benefit reconcile
    def _reconcile_benefits(self, rule: dict, guide_id: int, from_code: str, to_code: str,
                            decision: str, at: str) -> list[dict]:
        from_idx = self.rules.level_index(rule, from_code)
        to_idx = self.rules.level_index(rule, to_code)
        active = {g["benefit_code"]: g for g in self.store.active_grants(guide_id)}
        effects: list[dict] = []

        if to_idx > from_idx:  # 晋级：补发新等级（含跨越等级）的全部权益
            for level in rule["levels"][: to_idx + 1]:
                for benefit in self.rules.benefits_for(rule, level["code"]):
                    if benefit["code"] in active:
                        continue
                    grant_id = self.store.grant_benefit(
                        {
                            "guide_id": guide_id,
                            "benefit_code": benefit["code"],
                            "benefit_name": benefit["name"],
                            "level_code": level["code"],
                            "rule_version": rule["version"],
                            "granted_at": at,
                        }
                    )
                    effects.append(
                        {
                            "action": "granted",
                            "benefit_code": benefit["code"],
                            "benefit_name": benefit["name"],
                            "grant_id": grant_id,
                            "reason": f"晋级 {from_code}→{to_code}，授予 {level['code']} 权益",
                        }
                    )
        else:  # 降级：按权益定义决定收回或保留
            for code, grant in list(active.items()):
                benefit = next((b for b in rule["benefits"] if b["code"] == code), None)
                if benefit is None:
                    continue
                granted_idx = self.rules.level_index(rule, benefit["level_code"])
                if granted_idx <= to_idx:
                    continue  # 新等级仍享有的权益不动
                if not benefit.get("revoke_on_demotion", True):
                    effects.append(
                        {
                            "action": "retained",
                            "benefit_code": code,
                            "benefit_name": benefit["name"],
                            "grant_id": grant["id"],
                            "reason": (
                                f"降级 {from_code}→{to_code}，权益 {code} 标记为不自动收回，"
                                "继续保留（人工决定是否追回）"
                            ),
                        }
                    )
                    continue
                self.store.revoke_benefit(
                    grant["id"], at, None,
                    f"降级 {from_code}→{to_code}，按规则版本 {rule['version']} 收回",
                )
                effects.append(
                    {
                        "action": "revoked",
                        "benefit_code": code,
                        "benefit_name": benefit["name"],
                        "grant_id": grant["id"],
                        "reason": f"降级 {from_code}→{to_code}，不再满足 {benefit['level_code']}，按规则收回",
                    }
                )
        return effects

    # ----------------------------------------------------------------- rules
    @staticmethod
    def _eligible_level_index(rule: dict, metrics: dict) -> int:
        """返回满足全部门槛的最高等级下标。"""
        result = 0
        caps = set(metrics["topic_capabilities"])
        for idx, level in enumerate(rule["levels"]):
            ok = (
                metrics["points_balance"] >= int(level["min_points"])
                and metrics["session_count"] >= int(level["min_sessions"])
                and metrics["avg_stars"] >= float(level["min_avg_stars"])
                and set(level["topic_capabilities"]).issubset(caps)
            )
            if ok:
                result = idx
        return result

    @staticmethod
    def _explain_promotion(rule, metrics, from_code, to_code, reasons) -> None:
        for level in rule["levels"]:
            if level["code"] == to_code:
                reasons.append(
                    f"达到 {to_code}（{level['name']}）门槛：积分 "
                    f"{metrics['points_balance']}>={level['min_points']}、"
                    f"场次 {metrics['session_count']}>={level['min_sessions']}、"
                    f"均分 {metrics['avg_stars']}>={level['min_avg_stars']}、"
                    f"主题能力 {sorted(metrics['topic_capabilities'])} 覆盖 "
                    f"{level['topic_capabilities']}；自 {from_code} 晋级"
                )
                return

    @staticmethod
    def _explain_shortfall(rule, metrics, from_idx, eligible_idx, reasons) -> None:
        level = rule["levels"][from_idx]
        gaps = []
        if metrics["points_balance"] < level["min_points"]:
            gaps.append(f"积分 {metrics['points_balance']}<{level['min_points']}")
        if metrics["session_count"] < level["min_sessions"]:
            gaps.append(f"场次 {metrics['session_count']}<{level['min_sessions']}")
        if metrics["avg_stars"] < level["min_avg_stars"]:
            gaps.append(f"均分 {metrics['avg_stars']}<{level['min_avg_stars']}")
        missing = sorted(set(level["topic_capabilities"]) - set(metrics["topic_capabilities"]))
        if missing:
            gaps.append(f"缺主题能力 {missing}")
        reasons.append(
            f"当前等级 {level['code']} 不再达标（{('；'.join(gaps)) or '综合指标不足'}），"
            f"按指标仅满足 {rule['levels'][eligible_idx]['code']}"
        )

    @staticmethod
    def _trailing_warnings(history) -> int:
        count = 0
        for row in reversed(history):
            if row["decision"] == "warn":
                count += 1
            else:
                break
        return count

    @staticmethod
    def _last_demote(history):
        for row in reversed(history):
            if row["decision"] == "demote":
                return row
        return None

    def _in_demotion_cooldown(self, rule, history, as_of) -> bool:
        last = self._last_demote(history)
        if last is None:
            return False
        days = int(rule["params"]["protection_days_after_demotion"])
        return parse_iso(as_of) < parse_iso(add_days(last["as_of"], days))

    def _in_promotion_protection(self, rule, assignment, history, as_of) -> bool:
        if assignment is None or not assignment["evaluation_id"]:
            return False
        eval_row = self.store.get_evaluation(assignment["evaluation_id"])
        if eval_row is None or eval_row["decision"] != "promote":
            return False
        days = int(rule["params"]["protection_days_after_promotion"])
        return parse_iso(as_of) < parse_iso(add_days(assignment["effective_from"], days))

    # ------------------------------------------------------------- manual hold
    def request_hold(self, guide_id: int, *, reason_code: str, reason: str,
                     evidence: dict | None = None, requested_by: str = "operator",
                     requested_at: str | None = None) -> dict:
        with self.store.tx():
            if self.store.get_guide(guide_id) is None:
                raise NotFoundError(f"讲解员不存在：{guide_id}", code="guide_not_found")
            if self.store.pending_hold(guide_id) is not None:
                raise ConflictError("该讲解员已有待处理的人工复核", code="hold_exists")
            hold_id = self.store.insert_hold(
                {
                    "guide_id": guide_id,
                    "reason_code": reason_code,
                    "reason": reason,
                    "evidence": evidence or {},
                    "requested_by": requested_by,
                    "requested_at": to_iso(requested_at, default_now=True),
                }
            )
            return self.hold_dto(self.store.get_hold(hold_id))

    def resolve_hold(self, hold_id: int, *, resolution: str, comment: str | None = None,
                     resolved_by: str = "operator", at: str | None = None) -> dict:
        if resolution not in ("confirm_demote", "keep_level"):
            raise ValidationError("resolution 必须是 confirm_demote 或 keep_level")
        at = to_iso(at, default_now=True)
        with self.store.tx():
            hold = self.store.get_hold(hold_id)
            if hold is None:
                raise NotFoundError(f"人工复核单不存在：{hold_id}", code="hold_not_found")
            if hold["status"] != "pending":
                raise ConflictError("该复核单已处理", code="hold_resolved")
            self.store.resolve_hold(hold_id, resolution, resolved_by, at, comment)
            if resolution == "confirm_demote":
                evaluation = self.evaluate(
                    hold["guide_id"],
                    as_of=at,
                    trigger_type="manual_review",
                    force=True,
                    actor=resolved_by,
                )
            else:
                # 人工维持同样走强制分支，否则保护期/预警逻辑会覆盖人工结论。
                evaluation = self.evaluate(
                    hold["guide_id"],
                    as_of=at,
                    trigger_type="manual_review",
                    force=True,
                    manual_override="keep_level",
                    actor=resolved_by,
                )
            return {
                "hold": self.hold_dto(self.store.get_hold(hold_id)),
                "evaluation": evaluation,
            }

    # ------------------------------------------------------------------ dtos
    def evaluation_dto(self, row) -> dict:
        metrics = json_loads(row["metrics_json"], {})
        return {
            "evaluation_id": row["id"],
            "guide_id": row["guide_id"],
            "rule_version": row["rule_version"],
            "run_id": row["run_id"],
            "trigger_type": row["trigger_type"],
            "as_of": row["as_of"],
            "decision": row["decision"],
            "from_level_code": row["from_level_code"],
            "to_level_code": row["to_level_code"],
            "warning_until": row["warning_until"],
            "hold_id": row["hold_id"],
            "metrics": metrics,
            "reasons": json_loads(row["reasons_json"], []),
            "effects": json_loads(row["effects_json"], []),
            "created_at": row["created_at"],
        }

    @staticmethod
    def hold_dto(row) -> dict:
        return {
            "hold_id": row["id"],
            "guide_id": row["guide_id"],
            "reason_code": row["reason_code"],
            "reason": row["reason"],
            "evidence": json_loads(row["evidence_json"], {}),
            "status": row["status"],
            "requested_by": row["requested_by"],
            "requested_at": row["requested_at"],
            "resolution": row["resolution"],
            "resolved_by": row["resolved_by"],
            "resolved_at": row["resolved_at"],
            "comment": row["comment"],
        }
