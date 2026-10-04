"""端到端演示数据脚本。

运行：``python3 tools/seed_demo.py [db路径]``

场景（时间均为 UTC）：
- 2026-01 两位讲解员积累服务与好评，2 月批次晋级 L2；
- 王晓明 2 月补足主题能力，3 月批次晋级 L3 并获得骨干权益；
- 3 月两条差评触发降级条件，但处于晋级保护期，等级与权益维持；
  随后一条差评被撤回（冲回分录）、一条被更正（差额分录），历史原样保留；
- 李雯 3 月出现差评，6 月批次预警；场馆对其发起人工复核后确认降级，
  L2 徽章按规则收回；
- 同一批次号重跑，结果原样回放、零新增写入；
- 最后输出快照哈希链与全局守恒自检。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from growth_ladder.service import GrowthService  # noqa: E402


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "growth.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    svc = GrowthService(db_path)

    wang = svc.create_guide("G001", "王晓明")["id"]
    li = svc.create_guide("G002", "李雯")["id"]

    def service(gid, day, topic, minutes=30, uid=None):
        return svc.record_service(
            gid,
            {
                "minutes": minutes,
                "topic_code": topic,
                "session_uid": uid or f"S-{gid}-{day}",
                "started_at": f"2026-{day}T09:00:00+00:00",
            },
        )

    def review(gid, day, stars, uid, session=None):
        return svc.record_review(
            gid,
            {
                "stars": stars,
                "review_uid": uid,
                "session_ref": session,
                "published_at": f"2026-{day}T10:00:00+00:00",
            },
        )

    # ---- 2026-01：两人均完成 6 场基础服务，评价 1 个五星 + 5 个四星 ----
    for i, day in enumerate(["01-06", "01-09", "01-13", "01-17", "01-22", "01-27"]):
        for gid in (wang, li):
            service(gid, day, "basic_tour")
            stars = 5 if i == 0 else 4
            review(gid, day, stars, f"R-{gid}-{day}")

    feb = svc.run_cycle({"as_of": "2026-02-01T00:00:00+00:00"})

    # ---- 王晓明 2 月：9 场主题服务 + 9 条评价（2 五星 / 7 四星），达到 L3 ----
    for i, day in enumerate(["02-03", "02-06", "02-10", "02-13", "02-17",
                             "02-20", "02-23", "02-25", "02-27"]):
        service(wang, day, "themed_tour")
        review(wang, day, 5 if i < 2 else 4, f"R-{wang}-{day}")

    mar = svc.run_cycle({"as_of": "2026-03-01T00:00:00+00:00"})

    # ---- 3 月两条一星差评 ----
    review(wang, "03-10", 1, "R-G1-BAD-1")
    review(wang, "03-11", 1, "R-G1-BAD-2")
    review(li, "03-10", 1, "R-G2-BAD-1")

    protect = svc.run_cycle({"as_of": "2026-03-12T00:00:00+00:00"})

    # ---- 一条差评查实系恶意评价被撤回；一条更正为五星（差额分录 +14） ----
    withdrawn = svc.withdraw_review("R-G1-BAD-1", {"at": "2026-03-15T12:00:00+00:00"})
    corrected = svc.correct_review(
        "R-G1-BAD-2", {"new_stars": 5, "at": "2026-03-16T12:00:00+00:00"}
    )

    # ---- 6 月批次：王晓明 5 月补足 3 场主题服务与五星好评，重新满足 L3；
    #      李雯已过晋级保护期，收到降级预警 ----
    for day in ["05-08", "05-15", "05-22"]:
        service(wang, day, "themed_tour")
        review(wang, day, 5, f"R-{wang}-{day}")
    june = svc.run_cycle({"as_of": "2026-06-01T00:00:00+00:00"})

    # ---- 场馆怀疑李雯的服务记录不实，发起人工复核，批次评定挂起 ----
    hold = svc.request_hold(
        li,
        {
            "reason_code": "suspect_service_logs",
            "reason": "多条服务签到与学校联系人反馈不符，需要人工核对",
            "evidence": {"suspect_sessions": ["S-2-01-22", "S-2-01-27"]},
            "requested_by": "venue-admin",
            "requested_at": "2026-06-15T00:00:00+00:00",
        },
    )
    held_cycle = svc.run_cycle({"as_of": "2026-06-16T00:00:00+00:00"})
    resolution = svc.resolve_hold(
        hold["hold_id"],
        {
            "resolution": "confirm_demote",
            "comment": "核实两场服务未实际发生，确认降级；积分另行按流程冲回",
            "resolved_by": "venue-admin",
            "at": "2026-06-20T00:00:00+00:00",
        },
    )

    # ---- 同一批次号重跑：必须零新增、结果一致 ----
    rerun = svc.run_cycle({"as_of": "2026-03-12T00:00:00+00:00",
                           "run_id": protect["run_id"]})

    def pick(run, gid):
        return next(g for g in run["detail"]["guides"] if g["guide_id"] == gid)

    print("=" * 70)
    print("批次结果摘要")
    print("=" * 70)
    for name, run in [("2月", feb), ("3月", mar), ("3月中(差评后)", protect),
                      ("6月", june), ("6月中(复核挂起)", held_cycle)]:
        print(f"\n[{name}批次 {run['run_id']}] replayed={run.get('replayed', False)}")
        for g in run["detail"]["guides"]:
            print(f"  G{g['guide_id']}: {g['decision']:<7} {g['from']} -> {g['to']}"
                  f"  权益效果 {g['effects']} 条 (评定单 #{g['evaluation_id']})")

    print("\n" + "=" * 70)
    print("撤回与更正（旧分录不动，仅追加冲回/差额）")
    print("=" * 70)
    for label, evt in [("撤回 R-G1-BAD-1", withdrawn), ("更正 R-G1-BAD-2", corrected)]:
        print(f"\n{label}: 事件 #{evt['event_id']} {evt['event_type']} / {evt['memo']}")
        for p in evt["postings"]:
            print(f"  {p['direction']:<6} {p['points']:>3}  {p['account']:<20} {p['reason']}")

    print("\n" + "=" * 70)
    print("人工复核结论")
    print("=" * 70)
    ev = resolution["evaluation"]
    print(f"hold #{resolution['hold']['hold_id']} -> {resolution['hold']['resolution']}")
    print(f"评定 #{ev['evaluation_id']}: {ev['decision']} {ev['from_level_code']}->{ev['to_level_code']}")
    for r in ev["reasons"]:
        print("  理由：", r)
    for e in ev["effects"]:
        print(f"  权益：{e['action']:<8} {e['benefit_code']} — {e['reason']}")

    print("\n重跑 3 月中批次：", json.dumps({
        "run_id": rerun["run_id"], "replayed": rerun["replayed"],
        "evaluated": rerun["evaluated_guides"], "changed": rerun["changed_guides"],
    }, ensure_ascii=False))

    print("\n" + "=" * 70)
    print("快照哈希链（王晓明，末两条）与守恒自检")
    print("=" * 70)
    snaps = svc.snapshots(wang)
    for s in snaps[-2:]:
        print(f"  #{s['id']} {s['as_of']} 余额={s['points_balance']} 等级={s['level_code']}")
        print(f"     digest={s['digest'][:16]}… prev={str(s['prev_digest'])[:16]}…")
    report = svc.integrity()
    print("\n守恒自检：", json.dumps(report, ensure_ascii=False))
    assert report["balanced"], "守恒检查未通过！"
    assert rerun["replayed"] is True
    print("\n演示完成，数据库位于：", db_path)
    svc.close()


if __name__ == "__main__":
    main()
