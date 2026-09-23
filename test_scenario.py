"""完整剧情：四赛区 × 名手/名师/名人 × 七轮，共 40 个晋级位。

覆盖：同音跨区报名经审核归并、15/18 位证件归并、第七轮成绩纠错、
赛中退赛、赛后放弃资格递补、通知范围、名单锁定、重放一致性。
"""

import tempfile
import unittest
from pathlib import Path

from app import Application, rebuild
from test_helpers import (freeze_station, register, signature, SHA,
                          winner_outcome)

STATIONS = ["east", "west", "south", "north"]
GROUPS = [("名手组", 4), ("名师组", 3), ("名人组", 3)]
ROUNDS = 7
PLAYERS_PER_GROUP = 8


def build_full_world(path=None):
    app = Application(store_path=path)
    docs_used = set()

    def docnum(station_idx, group_idx, i, variant=0):
        # 标准 18 位身份证：6 位区划 + 8 位生日 + 3 位顺序 + 1 位校验
        region = f"{11 + station_idx:02d}01{group_idx:02d}"
        birth = f"198{group_idx}{station_idx + 1:02d}{(i % 28) + 1:02d}"
        return region + birth + f"{i:03d}X"

    person_strength = {}
    hero = None
    hero_doc18 = None
    for si, st in enumerate(STATIONS):
        freeze_station(
            app, st,
            groups=[{"name": g, "quota": q} for g, q in GROUPS],
            rounds=ROUNDS)
        for gi, (group, _quota) in enumerate(GROUPS):
            ids = []
            for i in range(PLAYERS_PER_GROUP):
                # 特殊：西站名手组头名直接以“东站英雄”的 15 位老身份证报名
                if st == "west" and group == "名手组" and i == 0:
                    old15 = hero_doc18[:6] + hero_doc18[8:17]
                    ev = app.identity.register(
                        station_id="west", group="名手组",
                        name="名手-east-0",
                        documents=[{"type": "身份证", "number": old15}],
                        submitted_by="admin",
                        idempotency_key=f"hero-west-{st}")
                    pid = ev.payload["person_id"]
                    assert pid == hero
                else:
                    name = f"{group[:2]}-{st}-{i}"
                    doc = docnum(si, gi, i)
                    docs_used.add(doc)
                    pid = register(app, st, name, doc, group=group,
                                   gender="男",
                                   birth_date=f"198{gi}-{(si % 12) + 1:02d}-"
                                              f"{(i % 28) + 1:02d}",
                                   seq=f"{group}-{i}")
                    if st == "east" and group == "名手组" and i == 0:
                        hero = pid
                        hero_doc18 = doc
                ids.append(pid)
                person_strength[pid] = i  # 编号越小越强

    # 同音异字跨区候选：south 名人组“张威”= north 名人组“张伟”
    zw = register(app, "south", "张威", "44010119900101999X",
                  group="名人组", gender="男", birth_date="1990-01-01",
                  seq="zhangwei-s")
    zhang = register(app, "north", "张伟", "44010219900101888X",
                     group="名人组", gender="男", birth_date="1990-01-01",
                     seq="zhangwei-n")
    candidates = app.identity.homophone_candidates()
    assert any(set(c["persons"]) == {zw, zhang} for c in candidates)
    app.identity.confirm_merge(zw, zhang, reason="本人持户口簿现场确认",
                               submitted_by="审核员",
                               idempotency_key="merge-zw")

    corrected_games = []
    for si, st in enumerate(STATIONS):
        for rnd in range(1, ROUNDS + 1):
            ev = app.stations.publish_pairings(
                st, rnd, submitted_by="裁判长",
                idempotency_key=f"pair-{st}-{rnd}")
            real_games = [g for g in ev.payload["games"] if not g["is_bye"]]
            # 第七轮 east 名手组最后一局演示“误录→纠错”
            correction_target = None
            if rnd == 7 and st == "east":
                master_games = [g for g in real_games
                                if g["group"] == "名手组"]
                correction_target = master_games[-1]["game_id"] \
                    if master_games else None
            for g in real_games:
                sb, sw = person_strength.get(g["black"], 99), \
                         person_strength.get(g["white"], 99)
                outcome = "黑胜" if sb <= sw else "白胜"
                if rnd == 7 and st == "east" and g["group"] == "名手组" \
                        and g["game_id"] == correction_target:
                    app.stations.submit_result(
                        st, g["game_id"], result_type="胜负", outcome="黑胜",
                        record_hash=SHA(f"wrong-{g['game_id']}"),
                        record_kind="棋谱", signature=signature(),
                        submitted_by="赵裁判",
                        idempotency_key=f"wrong-{st}-{g['game_id']}")
                    app.stations.correct_result(
                        st, g["game_id"], result_type="胜负", outcome="白胜",
                        record_hash=SHA(f"right-{g['game_id']}"),
                        record_kind="棋谱",
                        signature=signature("裁判长"),
                        reason="第七轮结果更正：记分表黑白栏颠倒",
                        submitted_by="裁判长",
                        idempotency_key=f"corr-{st}-{g['game_id']}")
                    corrected_games.append((st, g["game_id"]))
                    continue
                app.stations.submit_result(
                    st, g["game_id"], result_type="胜负", outcome=outcome,
                    record_hash=SHA(f"{st}-{g['game_id']}-r{rnd}"),
                    record_kind="棋谱", signature=signature(),
                    submitted_by="赵裁判",
                    idempotency_key=f"res-{st}-{g['game_id']}-r{rnd}")
        app.stations.finalize(st, submitted_by="admin",
                              idempotency_key=f"final-{st}")

    app.federation.freeze_federation_rules(
        expected_stations=STATIONS, zone_priority=STATIONS,
        submitted_by="org", idempotency_key="fed-rules")
    for st in STATIONS:
        app.federation.project_station(
            st, submitted_by="org", idempotency_key=f"proj-{st}")
    # 去重（hero 与合并后的张威/张伟都可能占两席）
    app.federation.resolve_duplicates(submitted_by="org")
    # 锁前最后一处变动：south 名师组头名放弃资格 → 站内递补
    status = app.federation.promotion_status("south", "名师组")
    top = next(iter(status["occupied"]))
    app.federation.revoke_qualification(
        "south", "名师组", top, reason="升学冲突放弃总决赛",
        submitted_by="org", idempotency_key="revoke-south-master")
    return app, {"corrected_games": corrected_games, "hero": hero}


class FullScenarioTest(unittest.TestCase):
    def test_four_zones_three_groups_lock_and_explain(self):
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            app, ctx = build_full_world(path)
            # 1. 40 个名额且全员唯一
            lock = app.federation.lock_final_roster(
                submitted_by="org", idempotency_key="lock")
            roster = app.federation.final_roster()
            all_holders = [row["person_id"]
                           for rows in roster["groups"].values()
                           for row in rows]
            self.assertEqual(len(all_holders), 40)
            self.assertEqual(len(set(all_holders)), 40)
            for group, quota in GROUPS:
                self.assertEqual(len(roster["groups"][group]),
                                 quota * len(STATIONS))

            # 2. 第七轮纠错保留事件链且解释可见
            st, game_id = ctx["corrected_games"][0]
            eff = app.stations.effective_result(st, game_id)
            self.assertEqual(eff["current"]["outcome"], "白胜")
            types_ = [h["type"] for h in eff["history"]]
            self.assertEqual(types_, ["GameResultSubmitted",
                                      "ResultCorrected"])

            # 3. 解释某位临界晋级者：规则版本、对局、破同分依据齐全
            boundary = roster["groups"]["名手组"][-1]["person_id"]
            ex = app.federation.explain(boundary)
            part = ex["participations"][0]
            self.assertEqual(len(part["games"]), ROUNDS)
            self.assertTrue(part["games"][-1].get("record_hash"))
            self.assertEqual(part["rules_version"], "rules-2026-v1")
            self.assertIn(part["boundary"]["relation"], ("ahead", "last"))
            self.assertIsNotNone(ex["roster_lock"])

            # 4. hero 的解释说明他只占一个席位
            ex_hero = app.federation.explain(ctx["hero"])
            qualified = [p for p in ex_hero["participations"]
                         if p["seat"]]
            self.assertEqual(len(qualified), 1)

            # 5. 通知只发往受影响赛区组别
            south_notes = app.federation.notifications(station_id="south")
            self.assertTrue(any(
                n["scope"]["group"] == "名师组" and
                n["template"] == "promotion_advanced"
                for n in south_notes))
            north_notes = app.federation.notifications(station_id="north")
            self.assertFalse(any(
                n["template"] == "promotion_advanced" and
                n["scope"]["group"] == "名师组"
                for n in north_notes))

            # 6. 审计与重放：名单指纹不变、链完整
            self.assertTrue(app.store.verify_chain())
            fp = roster["fingerprint"]
            tail = app.store.tail_hash
            app2 = rebuild(path)
            self.assertTrue(app2.store.verify_chain())
            self.assertEqual(app2.store.tail_hash, tail)
            self.assertEqual(app2.federation.final_roster()["fingerprint"],
                             fp)
            # 重放后任何一轮成绩重投都不改变事实
            ev_before = len(app2.store)
            st2, gid2 = ctx["corrected_games"][0]
            app2.stations.correct_result(
                st2, gid2, result_type="胜负", outcome="白胜",
                record_hash=SHA(f"right-{gid2}"), record_kind="棋谱",
                signature=signature("裁判长"),
                reason="第七轮结果更正：记分表黑白栏颠倒",
                submitted_by="裁判长",
                idempotency_key=f"corr-{st2}-{gid2}")
            self.assertEqual(len(app2.store), ev_before)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
