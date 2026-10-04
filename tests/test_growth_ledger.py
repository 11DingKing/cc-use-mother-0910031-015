"""成长等级后端测试：守恒、冲回不改写历史、保护期、复核、幂等重跑、解释。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from growth_ledger import GrowthService, LedgerUnbalancedError, Store
from growth_ledger import engine


BANDS = [
    {"level": 1, "name": "见习讲解员", "min_points": 0,
     "protection_days": 0, "benefits": []},
    {"level": 2, "name": "初级讲解员", "min_points": 30,
     "protection_days": 30, "benefits": ["培训优先报名"]},
    {"level": 3, "name": "中级讲解员", "min_points": 80,
     "protection_days": 60, "benefits": ["培训优先报名", "星级徽章"]},
    {"level": 4, "name": "高级讲解员", "min_points": 200,
     "protection_days": 90, "benefits": ["培训优先报名", "星级徽章", "外事接待资格"]},
]


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = GrowthService(Store(":memory:"))
        self.svc.publish_rule(
            BANDS, effective_from=date(2026, 1, 1),
            published_by="admin", note="初版规则")

    def _promoted_guide(self, guide_id: str = "G1"):
        """建档 + 60 分 + 晋级，返回晋级后的时间线。"""
        self.svc.register_guide(guide_id, "小林")
        self.svc.post_event(guide_id, "SERVICE", 20,
                            occurred_on=date(2026, 1, 5), source_ref="SVC-1")
        self.svc.post_event(guide_id, "RATING", 40,
                            occurred_on=date(2026, 1, 6), source_ref="RATE-1")
        run = self.svc.run_promotions(date(2026, 1, 10))
        return run

    # ---------- 规则版本 ----------

    def test_rules_are_versioned_and_immutable(self) -> None:
        with self.assertRaises(Exception):
            self.svc.publish_rule(BANDS, effective_from=date(2026, 1, 2),
                                  published_by="admin", version=1)
        self.svc.publish_rule(BANDS, effective_from=date(2026, 6, 1),
                              published_by="admin")
        self.assertEqual([r["version"] for r in self.svc.list_rules()], [1, 2])
        with self.assertRaises(ValueError):
            self.svc.publish_rule(
                [{"level": 1, "name": "X", "min_points": 5}],
                effective_from=date(2027, 1, 1), published_by="admin")

    # ---------- 分录与守恒 ----------

    def test_every_event_posts_balanced_double_entries(self) -> None:
        self.svc.register_guide("G1", "小林")
        self.svc.post_event("G1", "SERVICE", 20, source_ref="S1")
        self.svc.post_event("G1", "RATING", 40, source_ref="R1")
        report = self.svc.timeline("G1")["balance"]
        self.assertTrue(report["balanced"])
        self.assertEqual(report["points_available"], 60)
        self.assertEqual(report["points_earned_gross"], 60)
        self.assertEqual(report["points_reversed"], 0)

    def test_duplicate_source_ref_rejected_but_idempotency_key_replays(self) -> None:
        self.svc.register_guide("G1", "小林")
        self.svc.post_event("G1", "RATING", 10, source_ref="R1",
                            idempotency_key="key-1")
        self.svc.post_event("G1", "RATING", 10, source_ref="R1",
                            idempotency_key="key-1")
        self.assertEqual(len(self.svc.list_events("G1")), 1)

    # ---------- 定时晋级与幂等重跑 ----------

    def test_scheduled_promotion_writes_movement_benefits_snapshot(self) -> None:
        self._promoted_guide()
        tl = self.svc.timeline("G1")
        movements = tl["movements"]
        self.assertEqual([(m["from_level"], m["to_level"], m["reason"])
                          for m in movements],
                         [(1, 1, "INITIAL"), (1, 2, "SCHEDULED_PROMOTION")])
        self.assertIn("RATE-1", movements[-1]["explanation"])
        self.assertIn("v1", movements[-1]["explanation"])
        grants = tl["benefit_grants"]
        self.assertEqual([(g["benefit"], g["action"]) for g in grants],
                         [("培训优先报名", "GRANT")])
        self.assertIn("SCHEDULED_PROMOTION", grants[0]["explanation"])
        self.assertEqual(movements[-1]["protection_until"], "2026-02-09")
        self.assertEqual(tl["snapshots"][-1]["level"], 2)

    def test_promotion_run_is_safely_rerunnable(self) -> None:
        self._promoted_guide()
        before = len(self.svc.timeline("G1")["movements"])
        rerun = self.svc.run_promotions(date(2026, 1, 10))
        third = self.svc.run_promotions(date(2026, 1, 10))
        self.assertTrue(rerun["rerun"] and third["rerun"])
        self.assertEqual(len(self.svc.timeline("G1")["movements"]), before)

    # ---------- 评价更正：冲回不改写历史、保护期 ----------

    def test_rating_correction_in_protection_holds_level(self) -> None:
        self._promoted_guide()  # 保护期至 2026-02-09
        original_promotion = self.svc.timeline("G1")["movements"][-1]

        # 2026-02-01 评价撤回（40 分），有效积分降到 20，候选等级回落 L1。
        rating = next(e for e in self.svc.list_events("G1")
                      if e["source_ref"] == "RATE-1")
        review = self.svc.open_review(
            "G1", "RATING_CORRECTION", "学校联系人撤回该评价",
            opened_by="ops", event_id=rating["id"],
            payload={"corrected_amount": 0, "effective_on": "2026-02-01"})
        result = self.svc.decide_review(
            review["id"], approved=True, decided_by="manager",
            decision_note="撤回属实")
        self.assertEqual(result["status"], "APPROVED")

        state = self.svc.evaluate("G1", date(2026, 2, 1))
        self.assertEqual(state["candidate_level"], 1)
        self.assertEqual(state["level"], 2)            # 保护期内维持
        self.assertTrue(state["protection_active"])

        run = self.svc.run_promotions(date(2026, 2, 1))
        self.assertEqual(run["held"], 1)
        movements = self.svc.timeline("G1")["movements"]
        hold = movements[-1]
        self.assertEqual(hold["reason"], "PROTECTION_HELD")
        self.assertEqual(hold["from_level"], hold["to_level"])
        self.assertIn("保护期", hold["explanation"])

        # 关键：历史晋级分录原样保留，未被改写。
        self.assertEqual(
            self.svc.timeline("G1")["movements"][1]["id"],
            original_promotion["id"])
        self.assertEqual(
            self.svc.timeline("G1")["movements"][1]["to_level"], 2)

        # 权益仍在，可凭原 GRANT 分录核对。
        self.assertEqual(self.svc.evaluate("G1", date(2026, 2, 1))["benefits"],
                         ["培训优先报名"])

    def test_demotion_after_protection_revokes_benefits_with_explanation(self) -> None:
        self.test_rating_correction_in_protection_holds_level()
        run = self.svc.run_promotions(date(2026, 2, 10))  # 保护期已过
        self.assertEqual(run["promoted"], 0)
        tl = self.svc.timeline("G1")
        last = tl["movements"][-1]
        self.assertEqual((last["from_level"], last["to_level"], last["reason"]),
                         (2, 1, "SCHEDULED_DEMOTION"))
        self.assertEqual(last["protection_until"], None)
        self.assertIn("已冲回", last["explanation"])
        revoke = [g for g in tl["benefit_grants"] if g["action"] == "REVOKE"]
        self.assertEqual(len(revoke), 1)
        self.assertIn(last["id"], revoke[0]["explanation"])
        self.assertEqual(self.svc.evaluate("G1", date(2026, 2, 10))["benefits"], [])

    def test_reversal_keeps_gross_history_and_conserves_balance(self) -> None:
        self._promoted_guide()
        rating = next(e for e in self.svc.list_events("G1")
                      if e["source_ref"] == "RATE-1")
        review = self.svc.open_review(
            "G1", "POINT_REVERSAL", "评价单作废",
            opened_by="ops", event_id=rating["id"])
        self.svc.decide_review(review["id"], approved=True, decided_by="manager")

        # 原事件保留为 REVERSED，不删除；冲回是独立事件、独立分录。
        original = self.svc.list_events("G1")
        self.assertEqual({e["status"] for e in original if e["source_ref"] == "RATE-1"},
                         {"REVERSED"})
        reversal = next(e for e in original if e["event_type"] == "RATING_REVERSAL")
        self.assertEqual(reversal["amount"], -40)

        entries = self.svc.list_entries("G1")
        rev_entries = [e for e in entries if e["event_id"] == reversal["id"]]
        self.assertEqual(sum(e["amount"] for e in rev_entries), 0)  # 冲回也平衡
        report = self.svc.timeline("G1")["balance"]
        self.assertTrue(report["balanced"])
        self.assertEqual(report["points_earned_gross"], 60)  # 毛获得不变
        self.assertEqual(report["points_reversed"], 40)
        self.assertEqual(report["points_available"], 20)

    def test_rejected_review_changes_nothing(self) -> None:
        self._promoted_guide()
        rating = next(e for e in self.svc.list_events("G1")
                      if e["source_ref"] == "RATE-1")
        review = self.svc.open_review(
            "G1", "POINT_REVERSAL", "存疑撤回",
            opened_by="ops", event_id=rating["id"])
        self.svc.decide_review(review["id"], approved=False, decided_by="manager")
        self.assertEqual(self.svc.evaluate("G1")["points_available"], 60)
        with self.assertRaises(Exception):
            self.svc.decide_review(review["id"], approved=True, decided_by="manager")

    def test_rating_correction_reposts_new_amount(self) -> None:
        self._promoted_guide()
        rating = next(e for e in self.svc.list_events("G1")
                      if e["source_ref"] == "RATE-1")
        review = self.svc.open_review(
            "G1", "RATING_CORRECTION", "评分由 5 星更正为 4 星",
            opened_by="ops", event_id=rating["id"],
            payload={"corrected_amount": 25})
        self.svc.decide_review(review["id"], approved=True, decided_by="manager")
        state = self.svc.evaluate("G1")
        self.assertEqual(state["points_available"], 45)  # 20 + 25
        self.assertEqual(state["points_earned_gross"], 85)  # 20+40+25 毛额
        self.assertEqual(state["points_reversed"], 40)

    # ---------- 人工定级 ----------

    def test_manual_level_override(self) -> None:
        self._promoted_guide()
        review = self.svc.open_review(
            "G1", "LEVEL_OVERRIDE", "特殊贡献，人工晋级",
            opened_by="ops", payload={"to_level": 3})
        self.svc.decide_review(review["id"], approved=True, decided_by="director")
        state = self.svc.evaluate("G1")
        self.assertEqual(state["level"], 3)
        self.assertEqual(set(state["benefits"]),
                         {"培训优先报名", "星级徽章"})
        self.assertTrue(state["protection_active"])

    # ---------- 快照与哈希链 ----------

    def test_snapshots_are_immutable_and_tamper_evident(self) -> None:
        self._promoted_guide()
        snaps = self.svc.list_snapshots("G1")
        self.assertGreaterEqual(len(snaps), 2)
        self.assertEqual(snaps[0]["prev_hash"], "")
        for prev, nxt in zip(snaps, snaps[1:]):
            self.assertEqual(nxt["prev_hash"], prev["hash"])

        # 直接篡改数据库里的历史快照，巡检必须发现。
        import json
        row = self.svc.store.conn.execute(
            "SELECT data FROM level_snapshots WHERE sequence = 1").fetchone()
        tampered = json.loads(row["data"])
        tampered["points_available"] = 999
        self.svc.store.conn.execute(
            "UPDATE level_snapshots SET data = ? WHERE id = ?",
            (json.dumps(tampered, ensure_ascii=False), tampered["id"]))
        self.svc.store.conn.commit()
        report = self.svc.conservation_report()
        self.assertFalse(report["balanced"])
        self.assertTrue(any("哈希" in v for v in report["snapshot_violations"]))

    # ---------- 守恒守卫：任何写操作后都平衡 ----------

    def test_conservation_holds_through_full_lifecycle(self) -> None:
        self._promoted_guide()
        rating = next(e for e in self.svc.list_events("G1")
                      if e["source_ref"] == "RATE-1")
        review = self.svc.open_review(
            "G1", "RATING_CORRECTION", "更正", opened_by="ops",
            event_id=rating["id"], payload={"corrected_amount": 35})
        self.svc.decide_review(review["id"], approved=True, decided_by="manager")
        self.svc.run_promotions(date(2026, 2, 10))
        self.assertTrue(self.svc.conservation_report()["balanced"])

    def test_guard_rolls_back_unbalanced_write(self) -> None:
        # 手工构造不平衡分录，守卫必须拒绝并回滚。
        self.svc.register_guide("G9", "测试")
        store = self.svc.store
        store.begin()
        try:
            store.insert_entry({
                "id": "ENT_bad1", "event_id": "EVT_bad", "guide_id": "G9",
                "account": engine.ACCOUNT_EARNED, "amount": -10,
                "occurred_on": "2026-01-05", "created_at": "2026-01-05T00:00:00",
                "reversal_of": None, "reason": "bad"})
            with self.assertRaises(LedgerUnbalancedError):
                self.svc._assert_conservation("G9")
        finally:
            store.rollback()
        self.assertTrue(self.svc.conservation_report()["balanced"])


if __name__ == "__main__":
    unittest.main()
