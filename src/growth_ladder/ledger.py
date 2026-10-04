"""积分事件与复式分录。

守恒规则
--------
每个事件写入一组借贷配平的分录：

- 讲解员积分账户 ``POINTS:G{id}``：贷记增加、借记减少；
- 系统发行账户 ``FUND:POINTS_ISSUED``：方向相反。

全体账户余额之和恒为 0（讲解员总积分 = 发行账户借方余额）。讲解员余额允许为负：
评价是必须如实登记的外部事实，撤回已消费积分形成的追扣会产生“积分欠账”，
在对账单与 ``/integrity`` 的 ``negative_balances`` 中显式暴露，由人工调整结清。

撤回与更正不删除、不覆盖任何旧分录：

- 撤回（评价撤回、服务取消）→ 追加一组方向相反的冲回分录；
- 更正评价 → 追加一组“新旧积分差额”分录，旧评价标记为 superseded。

因此任意历史时点都可用 ``occurred_at <= as_of`` 重放出当时的真实余额，
已据此发放的权益永远可以核对。
"""
from __future__ import annotations

import uuid
from collections import defaultdict

from .errors import ConflictError, NotFoundError, RuleError, ValidationError
from .rules import RuleBook
from .store import FUND_ACCOUNT, Store, guide_account
from .util import now_iso, parse_iso, to_iso


def _uid(prefix: str, value: str | None) -> str:
    return value or f"{prefix}_{uuid.uuid4().hex[:16]}"


class Ledger:
    def __init__(self, store: Store, rules: RuleBook):
        self.store = store
        self.rules = rules

    # ------------------------------------------------------------- recording
    def record_service(
        self,
        guide_id: int,
        *,
        minutes: int,
        topic_code: str,
        session_uid: str | None = None,
        started_at: str | None = None,
        created_by: str | None = None,
    ) -> dict:
        if not isinstance(minutes, int) or minutes <= 0:
            raise ValidationError("服务时长（分钟）必须是正整数")
        if not topic_code:
            raise ValidationError("topic_code 不能为空")
        uid = _uid("SES", session_uid)
        started_at = to_iso(started_at, default_now=True)
        recorded_at = now_iso()

        with self.store.tx():
            self._require_guide(guide_id)
            if self.store.get_session_by_uid(uid):
                raise ConflictError(f"服务场次已存在：{uid}", code="duplicate_session")
            rule = self.rules.current()
            policy = rule["points_policy"]
            raw = minutes // int(policy["service_minutes_per_point"])
            points = min(raw, int(policy["service_points_cap_per_session"]))
            if points <= 0:
                raise ValidationError(
                    f"服务时长 {minutes} 分钟不足 {policy['service_minutes_per_point']} 分钟，不产生积分"
                )
            event_id = self.store.insert_event(
                event_uid=_uid("EVT", None),
                guide_id=guide_id,
                event_type="service",
                occurred_at=started_at,
                recorded_at=recorded_at,
                source_ref=uid,
                memo=f"服务场次 {uid} / 主题 {topic_code} / {minutes} 分钟",
                created_by=created_by,
            )
            lines = [
                {
                    "account": FUND_ACCOUNT,
                    "direction": "debit",
                    "points": points,
                    "topic_code": topic_code,
                    "reason": f"服务发行积分：{minutes} 分钟 / 主题 {topic_code}",
                },
                {
                    "account": guide_account(guide_id),
                    "direction": "credit",
                    "points": points,
                    "topic_code": topic_code,
                    "reason": f"服务场次 {uid} 积分入账",
                },
            ]
            self.store.insert_postings(event_id, guide_id, lines)
            self.store.insert_session(
                guide_id=guide_id,
                uid=uid,
                topic_code=topic_code,
                started_at=started_at,
                minutes=minutes,
                event_id=event_id,
                recorded_at=recorded_at,
            )
            event = self.store.get_event(event_id)
        return self.event_dto(event, include_postings=True)

    def record_review(
        self,
        guide_id: int,
        *,
        stars: int,
        review_uid: str | None = None,
        session_ref: str | None = None,
        published_at: str | None = None,
        memo: str | None = None,
        created_by: str | None = None,
    ) -> dict:
        if not isinstance(stars, int) or not 1 <= stars <= 5:
            raise ValidationError("星级必须是 1~5 的整数")
        uid = _uid("REV", review_uid)
        published_at = to_iso(published_at, default_now=True)
        recorded_at = now_iso()

        with self.store.tx():
            self._require_guide(guide_id)
            if self.store.get_review_by_uid(uid):
                raise ConflictError(f"评价已存在：{uid}", code="duplicate_review")
            rule = self.rules.current()
            points = int(rule["points_policy"]["review_stars"][str(stars)])
            event_id = self.store.insert_event(
                event_uid=_uid("EVT", None),
                guide_id=guide_id,
                event_type="review",
                occurred_at=published_at,
                recorded_at=recorded_at,
                source_ref=uid,
                memo=f"评价 {uid}：{stars} 星，积分 {points:+d}",
                created_by=created_by,
            )
            # 0 分评价（如 3 星）只登记事实，不产生分录。
            if points != 0:
                self.store.insert_postings(
                    event_id, guide_id,
                    self._signed_lines(
                        guide_id, points, topic_code=None,
                        credit_reason=f"评价 {uid} 好评积分（{stars} 星）",
                        debit_reason=f"评价 {uid} 差评扣减（{stars} 星）",
                    ),
                )
            self.store.insert_review(
                guide_id=guide_id,
                uid=uid,
                stars=stars,
                published_at=published_at,
                session_ref=session_ref,
                event_id=event_id,
                memo=memo,
                recorded_at=recorded_at,
            )
            event = self.store.get_event(event_id)
        return self.event_dto(event, include_postings=True)

    def withdraw_review(self, review_uid: str, *, at: str | None = None) -> dict:
        """评价撤回：冲回该评价全部积分，旧事件保留、状态置 reversed。"""
        at = to_iso(at, default_now=True)
        with self.store.tx():
            review = self.store.get_review_by_uid(review_uid)
            if review is None:
                raise NotFoundError(f"评价不存在：{review_uid}", code="review_not_found")
            if review["status"] != "active":
                raise ConflictError(
                    f"评价 {review_uid} 当前状态 {review['status']}，不能撤回",
                    code="review_not_active",
                )
            if parse_iso(at) < parse_iso(review["published_at"]):
                raise ValidationError("撤回时间不能早于评价发布时间")
            reversal_id = self._post_reversal(
                guide_id=review["guide_id"],
                original_event_id=review["event_id"],
                occurred_at=at,
                kind="review",
                source_ref=review_uid,
                memo=f"评价 {review_uid} 撤回，积分冲回",
            )
            self.store.mark_review_changed(review["id"], "withdrawn", at, None)
            event = self.store.get_event(reversal_id)
        return self.event_dto(event, include_postings=True)

    def correct_review(
        self,
        review_uid: str,
        *,
        new_stars: int,
        at: str | None = None,
        memo: str | None = None,
    ) -> dict:
        """评价更正：旧评价置 superseded，仅就积分差额追加更正事件。"""
        if not isinstance(new_stars, int) or not 1 <= new_stars <= 5:
            raise ValidationError("新星级必须是 1~5 的整数")
        at = to_iso(at, default_now=True)
        with self.store.tx():
            review = self.store.get_review_by_uid(review_uid)
            if review is None:
                raise NotFoundError(f"评价不存在：{review_uid}", code="review_not_found")
            if review["status"] != "active":
                raise ConflictError(
                    f"评价 {review_uid} 当前状态 {review['status']}，不能更正",
                    code="review_not_active",
                )
            if new_stars == review["stars"]:
                raise ConflictError("新星级与原星级相同，无需更正", code="review_unchanged")
            if parse_iso(at) < parse_iso(review["published_at"]):
                raise ValidationError("更正时间不能早于评价发布时间")
            rule = self.rules.current()
            old_points = int(rule["points_policy"]["review_stars"][str(review["stars"])])
            new_points = int(rule["points_policy"]["review_stars"][str(new_stars)])
            delta = new_points - old_points

            corrected_uid = _uid("REV", None)
            recorded_at = now_iso()
            event_id = self.store.insert_event(
                event_uid=_uid("EVT", None),
                guide_id=review["guide_id"],
                event_type="review",
                occurred_at=at,
                recorded_at=recorded_at,
                source_ref=corrected_uid,
                reversal_of=review["event_id"],
                memo=(
                    f"评价更正：{review_uid}（{review['stars']}星/{old_points:+d}）"
                    f"→ {new_stars}星/{new_points:+d}，差额 {delta:+d}"
                ),
                created_by=None,
            )
            lines = self._signed_lines(
                review["guide_id"],
                delta,
                topic_code=None,
                credit_reason=f"评价 {review_uid} 更正补记 {delta:+d}",
                debit_reason=f"评价 {review_uid} 更正追扣 {delta:+d}",
            )
            # 不同星级但积分相同（差额为 0）时只更正事实，不产生分录。
            if delta != 0:
                self.store.insert_postings(event_id, review["guide_id"], lines)
            new_id = self.store.insert_review(
                guide_id=review["guide_id"],
                uid=corrected_uid,
                stars=new_stars,
                published_at=at,
                session_ref=review["session_ref"],
                event_id=event_id,
                memo=memo or f"由 {review_uid} 更正而来",
                recorded_at=recorded_at,
            )
            self.store.mark_review_changed(review["id"], "corrected", at, new_id)
            event = self.store.get_event(event_id)
        return self.event_dto(event, include_postings=True)

    def cancel_session(self, session_uid: str, *, at: str | None = None) -> dict:
        """服务场次取消（如登记错误、活动撤销）：冲回服务积分。"""
        at = to_iso(at, default_now=True)
        with self.store.tx():
            session = self.store.get_session_by_uid(session_uid)
            if session is None:
                raise NotFoundError(f"服务场次不存在：{session_uid}", code="session_not_found")
            if session["status"] != "confirmed":
                raise ConflictError(f"服务场次 {session_uid} 已取消", code="session_cancelled")
            if parse_iso(at) < parse_iso(session["started_at"]):
                raise ValidationError("取消时间不能早于服务开始时间")
            reversal_id = self._post_reversal(
                guide_id=session["guide_id"],
                original_event_id=session["event_id"],
                occurred_at=at,
                kind="service",
                source_ref=session_uid,
                memo=f"服务场次 {session_uid} 取消，积分冲回",
            )
            self.store.conn.execute(
                "UPDATE service_sessions SET status='cancelled', changed_at=? WHERE id=?",
                (at, session["id"]),
            )
            event = self.store.get_event(reversal_id)
        return self.event_dto(event, include_postings=True)

    def manual_adjust(
        self,
        guide_id: int,
        *,
        points: int,
        reason: str,
        topic_code: str | None = None,
        occurred_at: str | None = None,
        created_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """人工积分调整（可正可负），必须给出理由；同样借贷配平、余额不得为负。"""
        if not isinstance(points, int) or points == 0:
            raise ValidationError("调整积分必须是非零整数")
        if not reason:
            raise ValidationError("人工调整必须填写理由")
        occurred_at = to_iso(occurred_at, default_now=True)
        key = idempotency_key or f"ADJ:{uuid.uuid4().hex[:16]}"
        with self.store.tx():
            self._require_guide(guide_id)
            existing = self.store.find_event(guide_id, "manual_adjust", key)
            if existing:
                return self.event_dto(existing, include_postings=True)
            event_id = self.store.insert_event(
                event_uid=_uid("EVT", None),
                guide_id=guide_id,
                event_type="manual_adjust",
                occurred_at=occurred_at,
                recorded_at=now_iso(),
                source_ref=key,
                memo=f"人工调整 {points:+d}：{reason}",
                created_by=created_by,
            )
            lines = self._signed_lines(
                guide_id,
                points,
                topic_code=topic_code,
                credit_reason=f"人工调整补记 {points:+d}：{reason}",
                debit_reason=f"人工调整追扣 {points:+d}：{reason}",
            )
            self.store.insert_postings(event_id, guide_id, lines)
            event = self.store.get_event(event_id)
        return self.event_dto(event, include_postings=True)

    # ------------------------------------------------------------- internals
    def _post_reversal(
        self,
        *,
        guide_id: int,
        original_event_id: int,
        occurred_at: str,
        kind: str,
        source_ref: str,
        memo: str,
    ) -> int:
        original = self.store.get_event(original_event_id)
        if original is None:  # pragma: no cover - 数据一致性保护
            raise RuleError("被冲回事件不存在", code="original_event_missing")
        if original["status"] in ("reversed", "superseded"):
            raise ConflictError(
                f"事件 {original_event_id} 已处于 {original['status']}，不能重复冲回",
                code="event_already_reversed",
            )
        original_lines = self.store.list_postings([original_event_id])
        reversal_uid = _uid("EVT", None)
        # 冲回事件幂等：同一源引用只允许一次冲回。
        dup = self.store.find_event(guide_id, "reversal", source_ref)
        if dup:
            raise ConflictError(f"{source_ref} 已存在冲回事件", code="reversal_exists")
        reversal_id = self.store.insert_event(
            event_uid=reversal_uid,
            guide_id=guide_id,
            event_type="reversal",
            occurred_at=occurred_at,
            recorded_at=now_iso(),
            source_ref=source_ref,
            reversal_of=original_event_id,
            memo=memo,
        )
        reversed_lines = [
            {
                "account": row["account"],
                "direction": "credit" if row["direction"] == "debit" else "debit",
                "points": row["points"],
                "topic_code": row["topic_code"],
                "reason": f"冲回事件 {original_event_id}：{row['reason']}",
            }
            for row in original_lines
        ]
        # 被冲回事件本身零金额（如 3 星评价无分录）时，冲回事件也只登记事实。
        if reversed_lines:
            self.store.insert_postings(reversal_id, guide_id, reversed_lines)
        self.store.update_event_status(original_event_id, "reversed")
        return reversal_id

    @staticmethod
    def _signed_lines(
        guide_id: int, points: int, *, topic_code: str | None, credit_reason: str, debit_reason: str
    ) -> list[dict]:
        if points > 0:
            return [
                {"account": FUND_ACCOUNT, "direction": "debit", "points": points,
                 "topic_code": topic_code, "reason": credit_reason},
                {"account": guide_account(guide_id), "direction": "credit", "points": points,
                 "topic_code": topic_code, "reason": credit_reason},
            ]
        amount = -points
        return [
            {"account": guide_account(guide_id), "direction": "debit", "points": amount,
             "topic_code": topic_code, "reason": debit_reason},
            {"account": FUND_ACCOUNT, "direction": "credit", "points": amount,
             "topic_code": topic_code, "reason": debit_reason},
        ]

    def _require_guide(self, guide_id: int) -> None:
        if self.store.get_guide(guide_id) is None:
            raise NotFoundError(f"讲解员不存在：{guide_id}", code="guide_not_found")

    # -------------------------------------------------------------- queries
    def balance(self, guide_id: int, as_of: str | None = None) -> int:
        self._require_guide(guide_id)
        return self.store.balance_at(guide_id, to_iso(as_of) if as_of else None)

    def metrics(self, guide_id: int, as_of: str, *, min_sessions_per_topic: int = 3) -> dict:
        """计算 as_of 时点的成长指标；撤回的评价与取消的场次自动排除。"""
        as_of = to_iso(as_of)
        rule = self.rules.current()
        sessions = self.store.point_in_time_sessions(guide_id, as_of)
        reviews = self.store.point_in_time_reviews(guide_id, as_of)
        topic_sessions: dict[str, int] = defaultdict(int)
        topic_minutes: dict[str, int] = defaultdict(int)
        total_minutes = 0
        for s in sessions:
            topic_sessions[s["topic_code"]] += 1
            topic_minutes[s["topic_code"]] += s["minutes"]
            total_minutes += s["minutes"]
        capabilities = sorted(
            t for t, n in topic_sessions.items() if n >= min_sessions_per_topic
        )
        stars = [r["stars"] for r in reviews]
        avg = round(sum(stars) / len(stars), 3) if stars else 0.0
        balance = self.store.balance_at(guide_id, as_of)
        return {
            "as_of": as_of,
            "rule_version": rule["version"],
            "points_balance": balance,
            "session_count": len(sessions),
            "total_minutes": total_minutes,
            "review_count": len(reviews),
            "avg_stars": avg,
            "topic_session_counts": dict(sorted(topic_sessions.items())),
            "topic_minutes": dict(sorted(topic_minutes.items())),
            "topic_capabilities": capabilities,
        }

    def statement(self, guide_id: int, as_of: str | None = None) -> dict:
        """账户流水（分录 + 累计余额），用于逐笔核对权益来源。"""
        self._require_guide(guide_id)
        events = self.store.list_events(guide_id, limit=10_000)
        if as_of:
            as_of = to_iso(as_of)
            events = [e for e in events if e["occurred_at"] <= as_of]
        postings = {
            e["id"]: [p for p in self.store.list_postings([e["id"]])
                      if p["account"] == guide_account(guide_id)]
            for e in events
        }
        running = 0
        rows = []
        for e in events:
            delta = 0
            for p in postings[e["id"]]:
                delta += p["points"] if p["direction"] == "credit" else -p["points"]
            running += delta
            rows.append(
                {
                    "event_id": e["id"],
                    "event_uid": e["event_uid"],
                    "event_type": e["event_type"],
                    "status": e["status"],
                    "reversal_of": e["reversal_of"],
                    "occurred_at": e["occurred_at"],
                    "source_ref": e["source_ref"],
                    "memo": e["memo"],
                    "delta": delta,
                    "balance_after": running,
                }
            )
        return {
            "guide_id": guide_id,
            "account": guide_account(guide_id),
            "as_of": as_of,
            "balance": running,
            "entries": rows,
        }

    def event_dto(self, event, *, include_postings: bool = False) -> dict:
        data = {
            "event_id": event["id"],
            "event_uid": event["event_uid"],
            "guide_id": event["guide_id"],
            "event_type": event["event_type"],
            "status": event["status"],
            "reversal_of": event["reversal_of"],
            "occurred_at": event["occurred_at"],
            "recorded_at": event["recorded_at"],
            "source_ref": event["source_ref"],
            "memo": event["memo"],
            "created_by": event["created_by"],
        }
        if include_postings:
            data["postings"] = [dict(p) for p in self.store.list_postings([event["id"]])]
            for p in data["postings"]:
                p.pop("guide_id", None)
        return data
