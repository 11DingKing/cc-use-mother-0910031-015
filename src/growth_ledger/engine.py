"""纯计算引擎：复式分录平衡、时间线重放、快照核对、变动解释。

不依赖数据库，输入均为 dict 列表，便于单测与重放审计。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

ACCOUNT_AVAILABLE = "AVAILABLE"   # 可用积分（资产方向，借方为正）
ACCOUNT_EARNED = "EARNED"         # 已获积分（收入方向，贷方为负）
ACCOUNT_REVERSED = "REVERSED"     # 冲回积分（收入抵减，借方为正）

REASON_INITIAL = "INITIAL"
REASON_SCHEDULED = "SCHEDULED_PROMOTION"
REASON_HOLD = "PROTECTION_HELD"
REASON_DEMOTION = "SCHEDULED_DEMOTION"
REASON_REVIEW = "REVIEW"


def iso(d: date) -> str:
    return d.isoformat()


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


# ---------- 平衡与守恒 ----------

def check_event_balanced(entries: list[dict]) -> None:
    """每个业务事件的全部分录金额合计必须为 0。"""
    total = sum(e["amount"] for e in entries)
    if total != 0:
        raise ValueError(
            f"事件 {entries[0]['event_id'] if entries else '?'} 分录不平衡："
            f"合计 {total} != 0"
        )


def balance_report(entries: list[dict]) -> dict[str, Any]:
    """
    账实守恒报告：
    - 逐事件借贷平衡；
    - 全局恒等式：AVAILABLE + EARNED + REVERSED == 0
      （可用 = 已获贷方发生额 − 冲回借方发生额）。
    """
    by_event: dict[str, int] = defaultdict(int)
    account_totals: dict[str, int] = defaultdict(int)
    for e in entries:
        by_event[e["event_id"]] += e["amount"]
        account_totals[e["account"]] += e["amount"]
    unbalanced = {k: v for k, v in by_event.items() if v != 0}
    identity_sum = (account_totals[ACCOUNT_AVAILABLE]
                    + account_totals[ACCOUNT_EARNED]
                    + account_totals[ACCOUNT_REVERSED])
    return {
        "balanced": not unbalanced and identity_sum == 0,
        "unbalanced_events": unbalanced,
        "account_totals": dict(account_totals),
        "points_available": account_totals[ACCOUNT_AVAILABLE],
        "points_earned_gross": -account_totals[ACCOUNT_EARNED],
        "points_reversed": account_totals[ACCOUNT_REVERSED],
        "identity_sum": identity_sum,
    }


# ---------- 规则 ----------

def band_for_points(bands: list[dict], points: int) -> dict:
    chosen = bands[0]
    for band in bands:
        if points >= band["min_points"]:
            chosen = band
    return chosen


# ---------- 时间线重放 ----------

def replay_points(entries: list[dict], as_of: str) -> dict[str, Any]:
    """重放截至某日（含）的分录，得到各账户余额与按事件的明细构成。"""
    totals = {ACCOUNT_AVAILABLE: 0, ACCOUNT_EARNED: 0, ACCOUNT_REVERSED: 0}
    contributors: list[dict] = []
    seen_events: set[str] = set()
    for e in entries:
        if e["occurred_on"] <= as_of:
            totals[e["account"]] += e["amount"]
            seen_events.add(e["event_id"])
    return {"as_of": as_of, "accounts": totals, "event_count": len(seen_events)}


def project_state(entries: list[dict], movements: list[dict], grants: list[dict],
                  rule: dict | None, as_of: str) -> dict[str, Any]:
    """
    依据历史事实重放讲解员在 as_of 当日的状态。

    关键不变量：积分事件本身不会改等级——等级只来自 movement 流水。
    因此冲回/更正只影响“候选等级”，已发生的等级历史原样保留。
    """
    points = replay_points(entries, as_of)
    available = points["accounts"][ACCOUNT_AVAILABLE]

    past_movements = [m for m in movements if m["effective_on"] <= as_of]
    last = past_movements[-1] if past_movements else None
    level = last["to_level"] if last else (rule["bands"][0]["level"] if rule else 1)
    protection_until = last["protection_until"] if last else None
    if protection_until is not None and protection_until < as_of:
        protection_active = False
    else:
        protection_active = protection_until is not None and protection_until >= as_of

    candidate = band_for_points(rule["bands"], available) if rule else None
    active_benefits = active_benefit_set(grants, as_of)

    return {
        "as_of": as_of,
        "rule_version": rule["version"] if rule else None,
        "points_available": available,
        "points_earned_gross": -points["accounts"][ACCOUNT_EARNED],
        "points_reversed": points["accounts"][ACCOUNT_REVERSED],
        "level": level,
        "candidate_level": candidate["level"] if candidate else None,
        "candidate_band": candidate,
        "protection_until": protection_until,
        "protection_active": protection_active,
        "benefits": sorted(active_benefits),
        "movement_count": len(past_movements),
        "entry_count": sum(1 for e in entries if e["occurred_on"] <= as_of),
    }


def active_benefit_set(grants: list[dict], as_of: str) -> set[str]:
    active: set[str] = set()
    for g in grants:
        if g["granted_on"] <= as_of:
            if g["action"] == "GRANT":
                active.add(g["benefit"])
            else:
                active.discard(g["benefit"])
    return active


def reconcile_benefits(active: set[str], desired: set[str]) -> tuple[list[str], list[str]]:
    """权益对账：返回 (待授予, 待撤销)，使权益集合与目标区间一致。"""
    return sorted(desired - active), sorted(active - desired)


# ---------- 快照哈希 ----------

def snapshot_content_hash(payload: dict[str, Any]) -> str:
    body = {
        "guide_id": payload["guide_id"],
        "rule_version": payload["rule_version"],
        "as_of_date": payload["as_of_date"],
        "points_earned": payload["points_earned"],
        "points_available": payload["points_available"],
        "level": payload["level"],
        "protection_until": payload["protection_until"],
        "benefits": sorted(payload["benefits"]),
        "entry_count": payload["entry_count"],
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def chain_hash(prev_hash: str, content_hash: str, sequence: int) -> str:
    raw = f"{sequence}|{prev_hash}|{content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_snapshots(snapshots: list[dict]) -> list[str]:
    """校验快照内容哈希与哈希链。"""
    violations: list[str] = []
    prev = ""
    for snap in snapshots:
        content = snapshot_content_hash(snap)
        if content != snap.get("content_hash"):
            violations.append(f"快照 {snap['id']} 内容哈希不匹配（余额被改写）")
        expected_chain = chain_hash(prev, content, snap["sequence"])
        if expected_chain != snap["hash"]:
            violations.append(f"快照 {snap['id']} 链哈希不匹配（链被重写）")
        if snap["prev_hash"] != prev:
            violations.append(f"快照 {snap['id']} 哈希链断裂")
        prev = snap["hash"]
    return violations


# ---------- 解释文案 ----------

def explain_points(events: list[dict], entries: list[dict], as_of: str) -> str:
    """用业务事件语言解释可用积分的构成。"""
    entry_by_event: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e["occurred_on"] <= as_of:
            entry_by_event[e["event_id"]].append(e)
    parts: list[str] = []
    for ev in events:
        if ev["id"] not in entry_by_event:
            continue
        if ev["amount"] < 0:
            # 冲回事件本身不在构成中重复列示，原事件已标注“已冲回”。
            continue
        if ev["status"] == "POSTED":
            parts.append(f"{ev['event_type']} +{ev['amount']}（{ev['source_ref']}）")
        elif ev["status"] == "REVERSED":
            parts.append(
                f"{ev['event_type']} {ev['amount']}（{ev['source_ref']}）已冲回"
            )
    return "；".join(parts) if parts else "暂无积分事件"


def explain_movement(*, rule: dict, from_level: int, to_level: int,
                     points_before: int, points_after: int, as_of: str,
                     events: list[dict], entries: list[dict], reason: str,
                     trigger: str, protection_until: str | None) -> str:
    band = band_for_points(rule["bands"], points_after)
    lower, upper = band["min_points"], None
    for b in rule["bands"]:
        if b["level"] == band["level"]:
            idx = rule["bands"].index(b)
            upper = (rule["bands"][idx + 1]["min_points"]
                     if idx + 1 < len(rule["bands"]) else None)
    range_text = f"[{lower},{upper})" if upper is not None else f">={lower}"
    breakdown = explain_points(events, entries, as_of)
    if from_level == to_level and reason == REASON_HOLD:
        current_band = next(b for b in rule["bands"] if b["level"] == from_level)
        return (
            f"截至 {as_of} 有效积分 {points_after}（{breakdown}）对应候选等级"
            f"低于当前等级 {from_level}，但等级保护期至 {protection_until}，"
            f"依据规则 v{rule['version']} 维持 {current_band['name']} 不降级；"
            f"触发：{trigger}"
        )
    direction = "晋级" if to_level > from_level else "降级" if to_level < from_level else "定级"
    text = (
        f"{direction}：{from_level} → {to_level}（{band['name']}）；"
        f"截至 {as_of} 有效积分 {points_after}，构成：{breakdown}；"
        f"依据规则 v{rule['version']} 区间 {range_text}；触发：{trigger}"
    )
    if protection_until:
        text += f"；进入新等级保护期至 {protection_until}"
    return text


def protection_end(on_date: date, protection_days: int) -> date | None:
    if protection_days <= 0:
        return None
    return on_date + timedelta(days=protection_days)
