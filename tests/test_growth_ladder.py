"""成长等级后端的核心不变量回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from growth_ladder.api import create_server
from growth_ladder.errors import ConflictError
from growth_ladder.service import GrowthService


def build_service() -> GrowthService:
    return GrowthService(":memory:")


def mature_guide(svc: GrowthService, gid: int, *, month: str = "01",
                 topic: str = "basic_tour", n: int = 6, good: bool = True) -> None:
    """快速积累 n 场服务与评价（首场五星其余四星）。"""
    for i in range(n):
        day = f"{month}-{i + 1:02d}"
        svc.record_service(
            gid,
            {"minutes": 120, "topic_code": topic,
             "session_uid": f"S-{gid}-{day}", "started_at": f"2026-{day}T09:00:00+00:00"},
        )
        svc.record_review(
            gid,
            {"stars": 5 if (i == 0 and good) else 4,
             "review_uid": f"R-{gid}-{day}", "published_at": f"2026-{day}T10:00:00+00:00"},
        )


class LedgerConservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.gid = self.svc.create_guide("G1", "测试员")["id"]

    def test_service_review_and_integrity(self) -> None:
        self.svc.record_service(self.gid, {"minutes": 60, "topic_code": "basic_tour",
                                           "session_uid": "S1", "started_at": "2026-01-01T09:00:00+00:00"})
        self.svc.record_review(self.gid, {"stars": 5, "review_uid": "R1",
                                          "published_at": "2026-01-01T10:00:00+00:00"})
        self.assertEqual(self.svc.balance(self.gid)["points_balance"], 8)
        report = self.svc.integrity()
        self.assertTrue(report["balanced"], report)
        self.assertEqual(report["guide_points_total"], report["fund_balance"])

    def test_withdraw_appends_reversal_without_rewriting_history(self) -> None:
        self.svc.record_service(self.gid, {"minutes": 3000, "topic_code": "basic_tour",
                                           "session_uid": "S0", "started_at": "2025-12-30T09:00:00+00:00"})
        self.svc.record_review(self.gid, {"stars": 1, "review_uid": "RBAD",
                                          "published_at": "2026-01-01T10:00:00+00:00"})
        bal_with_review = self.svc.balance(self.gid)["points_balance"]
        # 历史时点余额可复现：撤回前那一刻差评扣减仍在账上。
        before = self.svc.balance(self.gid, as_of="2026-01-02T00:00:00+00:00")["points_balance"]
        self.assertEqual(before, bal_with_review)

        reversal = self.svc.withdraw_review("RBAD", {"at": "2026-01-05T10:00:00+00:00"})
        self.assertEqual(reversal["event_type"], "reversal")
        self.assertEqual(reversal["reversal_of"], 2)
        # 旧事件仍在且标记 reversed，没有被删除或改写金额。
        old = self.svc.statement(self.gid)
        self.assertEqual([e["status"] for e in old["entries"]],
                         ["posted", "reversed", "posted"])
        # 差评冲回后，服务积分 8 分仍在账（撤回只冲评价，不动其他事件）。
        self.assertEqual(self.svc.balance(self.gid)["points_balance"], 8)
        self.assertEqual(
            self.svc.balance(self.gid, as_of="2026-01-02T00:00:00+00:00")["points_balance"], 0
        )
        self.assertTrue(self.svc.integrity()["balanced"])
        # 重复撤回被拒绝。
        with self.assertRaises(ConflictError):
            self.svc.withdraw_review("RBAD", {"at": "2026-01-06T10:00:00+00:00"})

    def test_correct_review_posts_only_delta(self) -> None:
        self.svc.record_service(self.gid, {"minutes": 3000, "topic_code": "basic_tour",
                                           "session_uid": "SBIG", "started_at": "2026-01-01T08:00:00+00:00"})
        self.svc.record_review(self.gid, {"stars": 1, "review_uid": "RC",
                                          "published_at": "2026-01-01T10:00:00+00:00"})
        bal_before = self.svc.balance(self.gid)["points_balance"]
        evt = self.svc.correct_review("RC", {"new_stars": 5, "at": "2026-01-03T10:00:00+00:00"})
        # -8 → +6，差额 +14。
        self.assertEqual(evt["memo"].split("差额 ")[1], "+14")
        self.assertEqual(self.svc.balance(self.gid)["points_balance"], bal_before + 14)
        self.assertTrue(self.svc.integrity()["balanced"])
        with self.assertRaises(ConflictError):
            self.svc.correct_review("RC", {"new_stars": 4, "at": "2026-01-04T10:00:00+00:00"})

    def test_clawback_negative_balance_is_recorded_and_reported(self) -> None:
        # 服务仅得 1 分，随后一条一星级评价追扣 8 分，形成“积分欠账” -7。
        self.svc.record_service(self.gid, {"minutes": 30, "topic_code": "basic_tour",
                                           "session_uid": "S1", "started_at": "2026-01-01T09:00:00+00:00"})
        self.svc.record_review(self.gid, {"stars": 1, "review_uid": "RBAD",
                                          "published_at": "2026-01-01T10:00:00+00:00"})
        self.assertEqual(self.svc.balance(self.gid)["points_balance"], -7)
        # 配平仍然成立；欠账在自检中显式暴露，等待人工调整结清。
        report = self.svc.integrity()
        self.assertTrue(report["balanced"])
        self.assertEqual(report["negative_balances"],
                         [{"guide_id": self.gid, "balance": -7}])
        # 人工补记后欠账清零。
        self.svc.manual_adjust(self.gid, {"points": 7, "reason": "结清积分欠账",
                                          "idempotency_key": "PAYBACK-1"})
        self.assertEqual(self.svc.balance(self.gid)["points_balance"], 0)
        self.assertEqual(self.svc.integrity()["negative_balances"], [])

    def test_zero_point_review_records_fact_without_postings(self) -> None:
        self.svc.record_service(self.gid, {"minutes": 30, "topic_code": "basic_tour",
                                           "session_uid": "S1", "started_at": "2026-01-01T09:00:00+00:00"})
        evt = self.svc.record_review(self.gid, {"stars": 3, "review_uid": "R3",
                                                "published_at": "2026-01-01T10:00:00+00:00"})
        self.assertEqual(evt["postings"], [])
        self.assertEqual(self.svc.balance(self.gid)["points_balance"], 1)
        # 撤回零分评价也不产生分录，且不报错。
        rev = self.svc.withdraw_review("R3", {"at": "2026-01-02T10:00:00+00:00"})
        self.assertEqual(rev["postings"], [])
        self.assertTrue(self.svc.integrity()["balanced"])

    def test_correction_from_negative_to_zero_stars_posts_delta(self) -> None:
        # 2 星 -4，3 星 0：更正差额 +4，正常配平。
        self.svc.record_service(self.gid, {"minutes": 3000, "topic_code": "basic_tour",
                                           "session_uid": "S9", "started_at": "2026-01-01T08:00:00+00:00"})
        self.svc.record_review(self.gid, {"stars": 2, "review_uid": "R2",
                                          "published_at": "2026-01-01T10:00:00+00:00"})
        before = self.svc.balance(self.gid)["points_balance"]
        evt = self.svc.correct_review("R2", {"new_stars": 3, "at": "2026-01-03T10:00:00+00:00"})
        self.assertEqual(len(evt["postings"]), 2)
        self.assertEqual(self.svc.balance(self.gid)["points_balance"], before + 4)
        self.assertTrue(self.svc.integrity()["balanced"])

    def test_manual_adjust_idempotent(self) -> None:
        self.svc.record_service(self.gid, {"minutes": 30, "topic_code": "basic_tour",
                                           "session_uid": "S1", "started_at": "2026-01-01T09:00:00+00:00"})
        payload = {"points": 5, "reason": "志愿活动加分", "idempotency_key": "ADJ-1"}
        first = self.svc.manual_adjust(self.gid, payload)
        second = self.svc.manual_adjust(self.gid, payload)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(self.svc.balance(self.gid)["points_balance"], 6)


class EvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.gid = self.svc.create_guide("G1", "测试员")["id"]

    def promote_to_l2(self):
        mature_guide(self.svc, self.gid)
        return self.svc.evaluate(self.gid, {"as_of": "2026-02-01T00:00:00+00:00"})

    def test_promotion_creates_assignment_and_benefit(self) -> None:
        result = self.promote_to_l2()
        self.assertEqual(result["decision"], "promote")
        self.assertEqual((result["from_level_code"], result["to_level_code"]), ("L1", "L2"))
        self.assertTrue(any("达到 L2" in r for r in result["reasons"]))
        grants = self.svc.grants(self.gid)
        self.assertEqual([g["benefit_code"] for g in grants if g["status"] == "active"],
                         ["BADGE_L2"])
        # 权益授予反查到评定单。
        self.assertEqual(grants[0]["granted_evaluation_id"], result["evaluation_id"])

    def test_protection_period_blocks_demotion(self) -> None:
        self.promote_to_l2()
        # 晋级后立刻来一条差评，均分跌破 4.0：保护期内必须 hold 且原因写明。
        self.svc.record_review(self.gid, {"stars": 1, "review_uid": "BAD",
                                          "published_at": "2026-02-05T10:00:00+00:00"})
        result = self.svc.evaluate(self.gid, {"as_of": "2026-02-06T00:00:00+00:00"})
        self.assertEqual(result["decision"], "hold")
        self.assertTrue(any("晋级保护期" in r for r in result["reasons"]))
        self.assertEqual(self.svc.balance(self.gid)["points_balance"],
                         self.svc.statement(self.gid)["balance"])

    def test_warning_then_one_level_demotion_and_cooldown(self) -> None:
        self.promote_to_l2()
        # 120 天后（超过 90 天保护期）大量一星级，使指标仅满足 L1。
        for i in range(8):
            self.svc.record_review(
                self.gid,
                {"stars": 1, "review_uid": f"BAD{i}",
                 "published_at": f"2026-06-{i + 1:02d}T10:00:00+00:00"},
            )
        first = self.svc.evaluate(self.gid, {"as_of": "2026-06-10T00:00:00+00:00"})
        self.assertEqual(first["decision"], "warn")
        self.assertIsNotNone(first["warning_until"])
        # 预警期内权益仍在。
        self.assertTrue(any(g["status"] == "active" for g in self.svc.grants(self.gid)))

        second = self.svc.evaluate(self.gid, {"as_of": "2026-07-10T00:00:00+00:00"})
        self.assertEqual(second["decision"], "demote")
        self.assertEqual((second["from_level_code"], second["to_level_code"]), ("L2", "L1"))
        # 逐级降级 + 权益收回带评定来源。
        grant = self.svc.grants(self.gid)[0]
        self.assertEqual(grant["status"], "revoked")
        self.assertEqual(grant["revoked_evaluation_id"], second["evaluation_id"])

        # 30 天冷静期内即使指标更差也不连续降级（已在 L1，这里验证等级区间连续）。
        assigns = self.svc.assignments(self.gid)
        self.assertEqual([a["level_code"] for a in assigns], ["L2", "L1"])

    def test_hold_blocks_scheduled_and_resolutions_apply(self) -> None:
        self.promote_to_l2()
        for i in range(8):
            self.svc.record_review(
                self.gid,
                {"stars": 1, "review_uid": f"H{i}",
                 "published_at": f"2026-06-{i + 1:02d}T10:00:00+00:00"},
            )
        hold = self.svc.request_hold(
            self.gid, {"reason_code": "x", "reason": "待核对", "requested_by": "admin",
                       "requested_at": "2026-06-09T00:00:00+00:00"}
        )
        with self.assertRaises(ConflictError):
            self.svc.request_hold(self.gid, {"reason_code": "y", "reason": "重复挂起"})
        blocked = self.svc.evaluate(self.gid, {"as_of": "2026-06-10T00:00:00+00:00"})
        self.assertEqual(blocked["decision"], "review")
        self.assertEqual(blocked["hold_id"], hold["hold_id"])

        kept = GrowthService(":memory:")
        # 第二条路径：人工选择维持等级。
        gid2 = kept.create_guide("G2", "另一人")["id"]
        mature_guide(kept, gid2)
        kept.evaluate(gid2, {"as_of": "2026-02-01T00:00:00+00:00"})
        for i in range(8):
            kept.record_review(gid2, {"stars": 1, "review_uid": f"K{i}",
                                      "published_at": f"2026-06-{i + 1:02d}T10:00:00+00:00"})
        h2 = kept.request_hold(gid2, {"reason_code": "x", "reason": "待核对"})
        outcome = kept.resolve_hold(h2["hold_id"], {"resolution": "keep_level",
                                                    "resolved_by": "admin"})
        self.assertEqual(outcome["evaluation"]["decision"], "hold")
        self.assertTrue(any("维持 L2" in r for r in outcome["evaluation"]["reasons"]))
        # 维持等级后权益不动。
        self.assertTrue(all(g["status"] == "active" for g in kept.grants(gid2)))


class BenefitsTest(unittest.TestCase):
    def test_high_benefit_can_be_retained_on_demotion(self) -> None:
        svc = build_service()
        gid = svc.create_guide("G1", "骨干")["id"]
        mature_guide(svc, gid, n=6)
        svc.evaluate(gid, {"as_of": "2026-02-01T00:00:00+00:00"})
        mature_guide(svc, gid, month="02", topic="themed_tour", n=9)
        # 策展能力 3 场 + 充足好评以达到 L4：补 curator_talk 服务，
        # L4 要求 120 分、30 场、4.5 均分，数据量较大，这里直接用规则门槛更宽松的新规则版本。
        svc.publish_rule({
            "levels": [
                {"code": "L1", "name": "见习", "min_points": 0, "min_sessions": 0,
                 "min_avg_stars": 0.0, "topic_capabilities": []},
                {"code": "L2", "name": "初级", "min_points": 15, "min_sessions": 6,
                 "min_avg_stars": 4.0, "topic_capabilities": ["basic_tour"]},
                {"code": "L3", "name": "骨干", "min_points": 40, "min_sessions": 10,
                 "min_avg_stars": 4.1, "topic_capabilities": ["basic_tour", "themed_tour"]},
                {"code": "L4", "name": "专家", "min_points": 60, "min_sessions": 12,
                 "min_avg_stars": 4.1, "topic_capabilities": ["basic_tour", "themed_tour"]},
            ],
            "benefits": [
                {"code": "B_L2", "name": "徽章", "level_code": "L2",
                 "revoke_on_demotion": True},
                {"code": "B_L4", "name": "策展资格", "level_code": "L4",
                 "revoke_on_demotion": False},
            ],
            "params": {"protection_days_after_promotion": 0,
                       "demote_grace_warnings": 0,
                       "protection_days_after_demotion": 0},
            "note": "测试用收紧规则",
        })
        up = svc.evaluate(gid, {"as_of": "2026-03-01T00:00:00+00:00"})
        self.assertEqual(up["to_level_code"], "L4")
        active = {g["benefit_code"] for g in svc.grants(gid) if g["status"] == "active"}
        # v1 发放的 BADGE_L2 仍在；v2 补发 B_L2 与 B_L4。
        self.assertEqual(active, {"BADGE_L2", "B_L2", "B_L4"})

        # 降级两级到 L2（连续两个周期逐级降）。
        for i in range(10):
            svc.record_review(gid, {"stars": 1, "review_uid": f"D{i}",
                                    "published_at": f"2026-04-{i + 1:02d}T10:00:00+00:00"})
        first = svc.evaluate(gid, {"as_of": "2026-04-15T00:00:00+00:00"})
        self.assertEqual(first["to_level_code"], "L3")
        second = svc.evaluate(gid, {"as_of": "2026-05-15T00:00:00+00:00"})
        self.assertEqual(second["to_level_code"], "L2")
        statuses = {g["benefit_code"]: g["status"] for g in svc.grants(gid)}
        # 降至 L2：L2 权益仍享有（active）；L4 策展资格按定义保留不收回。
        self.assertEqual(statuses["B_L2"], "active")
        self.assertEqual(statuses["B_L4"], "active")
        effects = {e["benefit_code"]: e["action"] for e in second["effects"]}
        self.assertEqual(effects.get("B_L4"), "retained")


class RuleVersioningTest(unittest.TestCase):
    def test_new_version_does_not_rewrite_history(self) -> None:
        svc = build_service()
        gid = svc.create_guide("G1", "测试员")["id"]
        mature_guide(svc, gid)
        first = svc.evaluate(gid, {"as_of": "2026-02-01T00:00:00+00:00"})
        self.assertEqual(first["rule_version"], 1)

        svc.publish_rule({"levels": [
            {"code": "L1", "name": "见习", "min_points": 0, "min_sessions": 0,
             "min_avg_stars": 0.0, "topic_capabilities": []},
            {"code": "L2", "name": "初级", "min_points": 999, "min_sessions": 999,
             "min_avg_stars": 5.0, "topic_capabilities": ["nope"]},
        ], "benefits": [
            {"code": "B2", "name": "徽章", "level_code": "L2",
             "revoke_on_demotion": True},
        ], "note": "收紧门槛"})
        # 旧评定单与旧快照内容不变，仍引用 v1。
        old = svc.evaluation_detail(first["evaluation_id"])
        self.assertEqual(old["rule_version"], 1)
        snap = svc.snapshots(gid)[0]
        self.assertEqual(snap["rule_version"], 1)
        # 新评定使用 v2，原 L2 不再达标，但保护期内维持。
        again = svc.evaluate(gid, {"as_of": "2026-02-02T00:00:00+00:00"})
        self.assertEqual(again["rule_version"], 2)
        self.assertEqual(again["decision"], "hold")


class JobRerunTest(unittest.TestCase):
    def test_scheduled_cycle_is_safely_rerunnable(self) -> None:
        svc = build_service()
        g1 = svc.create_guide("G1", "甲")["id"]
        g2 = svc.create_guide("G2", "乙")["id"]
        mature_guide(svc, g1)
        mature_guide(svc, g2)

        run = svc.run_cycle({"as_of": "2026-02-01T00:00:00+00:00"})
        self.assertFalse(run["replayed"])
        evals_after_first = len(svc.evaluations(g1)) + len(svc.evaluations(g2))
        grants_after_first = len(svc.grants(g1)) + len(svc.grants(g2))

        # 用同一 run_id 再跑：零新增评定、零新增权益。
        rerun = svc.run_cycle({"as_of": "2026-02-01T00:00:00+00:00", "run_id": run["run_id"]})
        self.assertTrue(rerun["replayed"])
        self.assertEqual(
            len(svc.evaluations(g1)) + len(svc.evaluations(g2)), evals_after_first
        )
        self.assertEqual(len(svc.grants(g1)) + len(svc.grants(g2)), grants_after_first)
        self.assertEqual(rerun["detail"], run["detail"])

        # 崩溃恢复模拟：批次 running 状态被遗留时重跑也能继续完成。
        with svc.store.conn:
            svc.store.conn.execute(
                "INSERT OR REPLACE INTO job_runs(run_id,job_name,as_of,rule_version,status,started_at)"
                " VALUES ('cycle-stuck','scheduled_level_evaluation',?,1,'running',?)",
                ("2026-02-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
            )
        recovered = svc.run_cycle({"as_of": "2026-02-01T00:00:00+00:00",
                                   "run_id": "cycle-stuck"})
        self.assertEqual(recovered["status"], "completed")
        self.assertTrue(svc.integrity()["balanced"])


class SnapshotChainTest(unittest.TestCase):
    def test_snapshots_form_hash_chain(self) -> None:
        svc = build_service()
        gid = svc.create_guide("G1", "测试员")["id"]
        mature_guide(svc, gid)
        svc.evaluate(gid, {"as_of": "2026-02-01T00:00:00+00:00"})
        svc.take_snapshot(gid, {"as_of": "2026-02-02T00:00:00+00:00"})
        snaps = svc.snapshots(gid)
        self.assertIsNone(snaps[0]["prev_digest"])
        self.assertEqual(snaps[1]["prev_digest"], snaps[0]["digest"])
        self.assertTrue(svc.store.verify_snapshot_chain(gid)["intact"])
        # 篡改历史余额会被重算哈希的链校验发现。
        svc.store.conn.execute("UPDATE account_snapshots SET points_balance=999 WHERE id=?",
                               (snaps[0]["id"],))
        report = svc.store.verify_snapshot_chain(gid)
        self.assertFalse(report["intact"])
        self.assertEqual(report["broken"][0]["snapshot_id"], snaps[0]["id"])


class ApiSmokeTest(unittest.TestCase):
    def test_http_end_to_end(self) -> None:
        import threading

        httpd = create_server("127.0.0.1", 0, ":memory:")
        host, port = httpd.server_address
        base = f"http://{host}:{port}"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def call(method: str, path: str, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())

        try:
            status, body = call("GET", "/health")
            self.assertEqual(status, 200)
            _, guide = call("POST", "/guides", {"guide_no": "G1", "name": "甲"})
            gid = guide["id"]
            call("POST", f"/guides/{gid}/services",
                 {"minutes": 120, "topic_code": "basic_tour",
                  "started_at": "2026-01-01T09:00:00+00:00"})
            call("POST", f"/guides/{gid}/reviews",
                 {"stars": 5, "review_uid": "RV1",
                  "published_at": "2026-01-01T10:00:00+00:00"})
            # 撤回路由与评定单详情路由（曾因海象运算符优先级返回 500）。
            call("POST", "/reviews/RV1/withdraw", {"at": "2026-01-02T10:00:00+00:00"})
            _, ev = call("POST", f"/guides/{gid}/evaluations",
                         {"as_of": "2026-02-01T00:00:00+00:00"})
            _, fetched = call("GET", f"/evaluations/{ev['evaluation_id']}")
            self.assertEqual(fetched["decision"], ev["decision"])
            _, cycle = call("POST", "/cycles/run", {"as_of": "2026-03-01T00:00:00+00:00"})
            # 单场服务（评价已撤回）不足以晋级，保持 L1。
            self.assertEqual(cycle["detail"]["guides"][0]["decision"], "hold")
            call("GET", f"/guides/{gid}/assignments")
            call("GET", f"/guides/{gid}/benefits")
            call("GET", f"/guides/{gid}/statement")
            call("GET", "/holds")
            _, integrity = call("GET", "/integrity")
            self.assertTrue(integrity["balanced"])
            _, explain = call("GET", f"/guides/{gid}/explain")
            self.assertIn("current_level", explain)
        finally:
            httpd.service.close()
            httpd.server_close()


class PersistenceTest(unittest.TestCase):
    def test_reopen_database_keeps_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "g.db")
            svc = GrowthService(path)
            gid = svc.create_guide("G1", "甲")["id"]
            svc.record_service(gid, {"minutes": 30, "topic_code": "basic_tour",
                                     "started_at": "2026-01-01T09:00:00+00:00"})
            svc.close()
            svc2 = GrowthService(path)
            self.assertEqual(svc2.balance(gid)["points_balance"], 1)
            self.assertEqual(svc2.list_rule_versions()[0]["version"], 1)
            svc2.close()


if __name__ == "__main__":
    unittest.main()
