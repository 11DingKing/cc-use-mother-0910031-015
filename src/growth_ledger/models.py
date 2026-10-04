"""领域模型：全部不可变，状态变化只通过追加分录体现。"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime


def _utcnow() -> datetime:
    return datetime.now().astimezone()


# ---------- 规则与等级区间 ----------

@dataclass(frozen=True, slots=True)
class LevelBand:
    """成长规则中的一个等级区间（阈值含下界、不含上界）。"""

    level: int
    name: str
    min_points: int
    protection_days: int = 0
    benefits: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GrowthRule:
    """成长规则版本。版本一经生效不可修改，只能发布新版本。"""

    version: int
    published_at: datetime
    effective_from: date
    published_by: str
    bands: tuple[LevelBand, ...]
    note: str = ""

    def band_for(self, points: int) -> LevelBand:
        chosen = self.bands[0]
        for band in self.bands:
            if points >= band.min_points:
                chosen = band
        return chosen

    def validate(self) -> None:
        if self.version <= 0:
            raise ValueError("规则版本号必须为正整数")
        if not self.bands:
            raise ValueError("至少需要一个等级区间")
        levels = [b.level for b in self.bands]
        if levels != sorted(levels) or len(set(levels)) != len(levels):
            raise ValueError("等级必须唯一且按序排列")
        thresholds = [b.min_points for b in self.bands]
        if thresholds != sorted(thresholds) or len(set(thresholds)) != len(thresholds):
            raise ValueError("等级积分阈值必须严格递增")
        if thresholds[0] != 0:
            raise ValueError("最低等级阈值必须为 0")
        if any(b.protection_days < 0 for b in self.bands):
            raise ValueError("保护期不能为负")


# ---------- 讲解员 ----------

@dataclass(frozen=True, slots=True)
class Guide:
    id: str
    name: str
    created_at: datetime = field(default_factory=_utcnow)
    initial_level: int = 1


# ---------- 积分事件与分录 ----------

@dataclass(frozen=True, slots=True)
class PointEntry:
    """
    单条积分分录。每个业务事件产生成对分录，组内金额合计必须为 0：

    - 对正向事件：一条 EARNED（+n，服务/评价获得的积分），
      一条 AVAILABLE（+n，讲解员当前可用积分）；
    - 对冲回事件：反向四分录 —— EARNED -n、REVERSAL_CLEARING +n、
      AVAILABLE -n、REVERSAL_CLEARING -n（或等价写法），
      本系统统一采用“红冲”两分录：EARNED -n 与 AVAILABLE -n，
      并以 reversal_of 指向被冲回分录；冲回分录同样满足借贷平衡。
    """

    id: str
    event_id: str
    guide_id: str
    account: str          # EARNED / AVAILABLE
    amount: int           # 红冲为负数
    occurred_on: date
    created_at: datetime
    reversal_of: str | None = None
    reason: str = ""

    def is_reversal(self) -> bool:
        return self.reversal_of is not None


@dataclass(frozen=True, slots=True)
class PointEvent:
    """业务积分事件（服务时长、评价等）。REVERSED 后原始事件与分录均不删除。"""

    id: str
    guide_id: str
    event_type: str       # SERVICE / RATING / MANUAL_ADJUST ...
    amount: int
    occurred_on: date
    created_at: datetime
    source_ref: str       # 外部单据号，如评价单 ID
    status: str           # POSTED / REVERSED / UNDER_REVIEW
    reversed_by: str | None = None
    note: str = ""
    idempotency_key: str | None = None


# ---------- 等级运行分录（升降级流水） ----------

@dataclass(frozen=True, slots=True)
class LevelMovement:
    """一次等级变动分录，解释从哪级到哪级、为什么、依据什么。"""

    id: str
    guide_id: str
    sequence: int
    rule_version: int
    from_level: int
    to_level: int
    points_before: int
    points_after: int
    reason: str           # SCHEDULED_PROMOTION / REVIEW / PROTECTION_HELD ...
    triggered_by: str
    trigger_event_id: str | None
    effective_on: date
    created_at: datetime
    protection_until: date | None = None
    explanation: str = ""


# ---------- 权益授予 ----------

@dataclass(frozen=True, slots=True)
class BenefitGrant:
    """权益授予/撤销分录。GRANT 与 REVOKE 成对可核对，永不物理删除。"""

    id: str
    guide_id: str
    benefit: str
    action: str           # GRANT / REVOKE
    level: int
    movement_id: str | None
    granted_on: date
    created_at: datetime
    reason: str
    explanation: str = ""


# ---------- 人工复核 ----------

@dataclass(frozen=True, slots=True)
class ReviewCase:
    id: str
    guide_id: str
    event_id: str | None
    kind: str             # RATING_CORRECTION / POINT_REVERSAL / LEVEL_OVERRIDE / PROTECTION_DISPUTE
    status: str           # OPEN / APPROVED / REJECTED
    reason: str
    opened_by: str
    opened_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str = ""
    payload: dict = field(default_factory=dict)


# ---------- 快照 ----------

@dataclass(frozen=True, slots=True)
class LevelSnapshot:
    """
    某讲解员在某次重算后的余额快照。快照不可变（append-only），
    带前序快照哈希，形成哈希链；重算结果与最新快照一致时不再新增。
    """

    id: str
    guide_id: str
    sequence: int
    rule_version: int
    as_of_date: date
    points_earned: int
    points_available: int
    level: int
    protection_until: date | None
    benefits: tuple[str, ...]
    movement_ids: tuple[str, ...]
    entry_count: int
    created_at: datetime
    content_hash: str
    prev_hash: str
    hash: str


# ---------- 定时晋级运行记录（可安全重跑） ----------

@dataclass(frozen=True, slots=True)
class PromotionRun:
    id: str
    run_date: date
    rule_version: int
    status: str           # STARTED / COMMITTED
    started_at: datetime
    committed_at: datetime | None
    processed: int
    promoted: int
    demoted: int
    held: int
    batch_key: str        # 幂等键：(run_date, rule_version)


def asdict(obj: object) -> dict:
    """递归转 dict（元组转列表，嵌套 dataclass 一并展开），供 JSON 序列化。"""
    def convert(value: object) -> object:
        if dataclasses.is_dataclass(value):
            return {f.name: convert(getattr(value, f.name))
                    for f in dataclasses.fields(value)}  # type: ignore[arg-type]
        if isinstance(value, tuple):
            return [convert(v) for v in value]
        if isinstance(value, list):
            return [convert(v) for v in value]
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        return value

    return convert(obj)  # type: ignore[return-value]
