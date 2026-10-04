"""成长规则版本。

规则一经发布即不可变：``rule_versions`` 仅追加，所有积分事件、评定与权益都
记录其依据的 ``rule_version``。新规则从下一次评定起生效，历史分录与历史快照
永不重算，因此“规则改版”不能改写已发权益。
"""
from __future__ import annotations

from .errors import RuleError, ValidationError
from .store import Store
from .util import to_iso

DECISIONS = {"promote", "hold", "demote", "warn", "review"}

# 积分事件类型 → 对讲解员账户的方向约定。
EVENT_TYPES = {
    "service": "credit",          # 服务时长积分
    "review": "credit",           # 评价积分（可能为负，见 points_policy）
    "manual_adjust": "both",      # 人工调整（points 可正可负）
    "reversal": "auto",           # 冲回：自动取被冲回事件的反向
}


DEFAULT_PARAMS = {
    # 连续达标周期数；这里“周期”指一次评定批次，保护单位为天。
    "promote_min_points_ratio": 1.0,   # 达到目标级区间下限即晋级
    "demote_grace_warnings": 1,        # 降级前先给几次预警
    "protection_days_after_promotion": 90,  # 晋级保护期
    "protection_days_after_demotion": 30,   # 降级冷静期（期内不连续降级）
    "min_service_minutes_for_level": 0,
}

# 评价星级 → 积分（1、2 星为负向）。
DEFAULT_POINTS_POLICY = {
    "service_minutes_per_point": 30,   # 每 30 分钟服务 1 分
    "service_points_cap_per_session": 8,
    "review_stars": {"5": 6, "4": 3, "3": 0, "2": -4, "1": -8},
    "withdraw_zeroes_review": True,    # 撤回：以反向分录清零，不删除旧事件
    "topic_bonus_per_capability": 2,   # 每个达标主题能力的附加分（一次性/周期评定）
}

DEFAULT_LEVELS = [
    {
        "code": "L1",
        "name": "见习讲解员",
        "min_points": 0,
        "min_sessions": 0,
        "min_avg_stars": 0.0,
        "topic_capabilities": [],
    },
    {
        "code": "L2",
        "name": "初级讲解员",
        "min_points": 20,
        "min_sessions": 6,
        "min_avg_stars": 4.0,
        "topic_capabilities": ["basic_tour"],
    },
    {
        "code": "L3",
        "name": "骨干讲解员",
        "min_points": 60,
        "min_sessions": 15,
        "min_avg_stars": 4.2,
        "topic_capabilities": ["basic_tour", "themed_tour"],
    },
    {
        "code": "L4",
        "name": "专家讲解员",
        "min_points": 120,
        "min_sessions": 30,
        "min_avg_stars": 4.5,
        "topic_capabilities": ["basic_tour", "themed_tour", "curator_talk"],
    },
]

DEFAULT_BENEFITS = [
    {
        "code": "BADGE_L2",
        "name": "初级讲解员电子徽章",
        "level_code": "L2",
        "revoke_on_demotion": True,
    },
    {
        "code": "TRAIN_THEMED",
        "name": "主题讲解进阶培训名额",
        "level_code": "L3",
        "revoke_on_demotion": True,
    },
    {
        "code": "STIPEND_L3",
        "name": "骨干交通补贴",
        "level_code": "L3",
        "revoke_on_demotion": True,
    },
    {
        "code": "CURATOR_ROLE",
        "name": "策展人对谈资格",
        "level_code": "L4",
        "revoke_on_demotion": False,  # 已授予的高阶资格不因降级自动收回
    },
]


class RuleBook:
    """规则版本的发布与读取。"""

    def __init__(self, store: Store):
        self.store = store

    def publish(
        self,
        *,
        params: dict | None = None,
        points_policy: dict | None = None,
        levels: list[dict] | None = None,
        benefits: list[dict] | None = None,
        note: str | None = None,
        published_at: str | None = None,
    ) -> dict:
        params = {**DEFAULT_PARAMS, **(params or {})}
        policy = {**DEFAULT_POINTS_POLICY, **(points_policy or {})}
        levels = levels or [dict(l) for l in DEFAULT_LEVELS]
        benefits = benefits or [dict(b) for b in DEFAULT_BENEFITS]
        self._validate(params, policy, levels, benefits)

        latest = self.store.get_rule_version()
        version = (latest["version"] + 1) if latest else 1
        digest = self.store.insert_rule_version(
            version=version,
            params=params,
            points_policy=policy,
            levels=levels,
            benefits=benefits,
            published_at=to_iso(published_at, default_now=True),
            note=note,
        )
        return {"version": version, "content_hash": digest}

    def ensure_default(self) -> dict:
        """无任何版本时发布内置默认规则；幂等。"""
        existing = self.store.get_rule_version()
        if existing:
            return {"version": existing["version"], "content_hash": existing["content_hash"]}
        return self.publish(note="内置默认成长规则")

    def get(self, version: int | None = None) -> dict:
        rule = self.store.get_rule_version(version)
        if rule is None:
            raise RuleError(f"规则版本不存在：{version}", code="rule_version_not_found", status=404)
        return rule

    def current(self) -> dict:
        return self.get(None)

    def list_versions(self) -> list[dict]:
        return self.store.list_rule_versions()

    @staticmethod
    def level_index(rule: dict, code: str | None) -> int:
        codes = [l["code"] for l in rule["levels"]]
        if code is None:
            return 0
        if code not in codes:
            raise RuleError(f"等级 {code} 不在规则版本 {rule['version']} 中")
        return codes.index(code)

    @staticmethod
    def benefits_for(rule: dict, code: str) -> list[dict]:
        return [b for b in rule["benefits"] if b["level_code"] == code]

    # ---------------------------------------------------------------- validate
    def _validate(self, params: dict, policy: dict, levels: list[dict], benefits: list[dict]) -> None:
        if not levels:
            raise ValidationError("至少需要一个等级")
        codes: list[str] = []
        prev_points = -1
        prev_sessions = -1
        for level in levels:
            for key in ("code", "name", "min_points", "min_sessions", "min_avg_stars", "topic_capabilities"):
                if key not in level:
                    raise ValidationError(f"等级缺少字段 {key}")
            code = level["code"]
            if code in codes:
                raise ValidationError(f"等级编码重复：{code}")
            codes.append(code)
            if level["min_points"] < 0 or level["min_points"] <= prev_points:
                raise ValidationError(f"等级 {code} 的 min_points 必须严格递增且非负")
            if level["min_sessions"] < 0 or level["min_sessions"] < prev_sessions:
                raise ValidationError(f"等级 {code} 的 min_sessions 必须非递减")
            if not 0.0 <= float(level["min_avg_stars"]) <= 5.0:
                raise ValidationError(f"等级 {code} 的 min_avg_stars 必须在 0~5 之间")
            prev_points = level["min_points"]
            prev_sessions = level["min_sessions"]
        for benefit in benefits:
            for key in ("code", "name", "level_code", "revoke_on_demotion"):
                if key not in benefit:
                    raise ValidationError(f"权益缺少字段 {key}")
            if benefit["level_code"] not in codes:
                raise ValidationError(f"权益 {benefit['code']} 引用了不存在的等级 {benefit['level_code']}")
        stars = policy.get("review_stars", {})
        for key, val in stars.items():
            int(key)  # 键必须能表示星级
            if not isinstance(val, int):
                raise ValidationError("评价积分必须是整数")
        if int(policy.get("service_minutes_per_point", 0)) <= 0:
            raise ValidationError("service_minutes_per_point 必须为正整数")
        for key in (
            "protection_days_after_promotion",
            "protection_days_after_demotion",
            "demote_grace_warnings",
        ):
            if int(params.get(key, 0)) < 0:
                raise ValidationError(f"{key} 不能为负")
