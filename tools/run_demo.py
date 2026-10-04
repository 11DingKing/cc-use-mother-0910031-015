"""端到端场景演示：晋级 → 评价撤回（保护期不降）→ 保护期后降级。

用法：python3 tools/run_demo.py
仅使用内存库，不产生持久化文件。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from growth_ledger import GrowthService, Store


def show(title: str, value: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    svc = GrowthService(Store(":memory:"))

    # 1) 发布成长规则 v1：等级区间 + 保护期 + 权益。
    svc.publish_rule(
        bands=[
            {"level": 1, "name": "见习讲解员", "min_points": 0,
             "protection_days": 0, "benefits": []},
            {"level": 2, "name": "初级讲解员", "min_points": 30,
             "protection_days": 30, "benefits": ["培训优先报名"]},
            {"level": 3, "name": "中级讲解员", "min_points": 80,
             "protection_days": 60, "benefits": ["培训优先报名", "星级徽章"]},
            {"level": 4, "name": "高级讲解员", "min_points": 200,
             "protection_days": 90, "benefits": ["培训优先报名", "星级徽章", "外事接待资格"]},
        ],
        effective_from=date(2026, 1, 1), published_by="场馆管理员",
        note="2026 年讲解员成长规则")

    # 2) 建档并积累服务时长、评价积分。
    svc.register_guide("G-001", "小林")
    svc.post_event("G-001", "SERVICE", 20,
                   occurred_on=date(2026, 1, 5), source_ref="SVC-2026-0007")
    svc.post_event("G-001", "RATING", 40,
                   occurred_on=date(2026, 1, 6), source_ref="RATE-2026-0031")

    # 3) 定时晋级（可安全重跑）。
    run1 = svc.run_promotions(date(2026, 1, 10))
    run2 = svc.run_promotions(date(2026, 1, 10))  # 重跑：零新分录
    print(f"首次晋级：promoted={run1['promoted']}；重跑：rerun={run2['rerun']}")

    # 4) 2026-02-01 学校联系人撤回评价，人工复核通过。
    rating = next(e for e in svc.list_events("G-001")
                  if e["source_ref"] == "RATE-2026-0031")
    review = svc.open_review(
        "G-001", "RATING_CORRECTION", "学校联系人撤回该评价",
        opened_by="活动统筹员", event_id=rating["id"],
        payload={"corrected_amount": 0, "effective_on": "2026-02-01"})
    svc.decide_review(review["id"], approved=True, decided_by="场馆管理员",
                      decision_note="撤回属实，冲回 40 分")

    print("\n--- 保护期内（2026-02-01）---")
    state = svc.evaluate("G-001", date(2026, 2, 1))
    print(f"当前等级={state['level']} 候选等级={state['candidate_level']} "
          f"有效积分={state['points_available']} 权益={state['benefits']} "
          f"保护至={state['protection_until']}")

    svc.run_promotions(date(2026, 2, 1))   # 产出 PROTECTION_HELD 分录

    # 5) 保护期结束后的定时任务才降级并撤销权益。
    svc.run_promotions(date(2026, 2, 10))
    state = svc.evaluate("G-001", date(2026, 2, 10))
    print("\n--- 保护期后（2026-02-10）---")
    print(f"当前等级={state['level']} 权益={state['benefits']}")

    tl = svc.timeline("G-001")
    print("\n--- 等级分录与解释 ---")
    for m in tl["movements"]:
        print(f"[{m['sequence']}] {m['reason']} {m['from_level']}→{m['to_level']}")
        print(f"    {m['explanation']}")

    print("\n--- 权益分录 ---")
    for g in tl["benefit_grants"]:
        print(f"{g['action']} {g['benefit']}：{g['explanation']}")

    print("\n--- 守恒核对 ---")
    report = svc.conservation_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
