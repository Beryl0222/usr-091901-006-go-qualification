"""赛区聚合：冻结次序、成绩证据、迟到/申诉/纠错新事件、退赛。"""

import unittest

from events import EventError
from app import Application
from test_helpers import (freeze_station, register, play_round, play_all_rounds,
                          signature, SHA)


class StationFlowTest(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        freeze_station(self.app, "east", quota=2, rounds=2)
        self.p1 = register(self.app, "east", "甲", "11010119900101001X", seq=1)
        self.p2 = register(self.app, "east", "乙", "11010119900101002X", seq=2)
        self.p3 = register(self.app, "east", "丙", "11010119900101003X", seq=3)

    def game(self, rnd):
        games = self.app.stations.pairings["east"][rnd]
        return next(g for g in games if not g["is_bye"])

    def test_pairings_freeze_round_by_round(self):
        ev1 = self.app.stations.publish_pairings(
            "east", 1, submitted_by="裁判长", idempotency_key="pp1")
        self.assertEqual(len(ev1.payload["games"]), 2)  # 一局 + 一轮空
        with self.assertRaises(EventError):
            self.app.stations.publish_pairings(
                "east", 1, submitted_by="裁判长")
        with self.assertRaises(EventError):
            self.app.stations.publish_pairings(
                "east", 3, submitted_by="裁判长")

    def test_result_requires_record_hash_and_signature(self):
        self.app.stations.publish_pairings("east", 1, submitted_by="裁判长")
        g = self.game(1)
        with self.assertRaises(EventError):
            self.app.stations.submit_result(
                "east", g["game_id"], result_type="胜负", outcome="黑胜",
                record_hash="bad", record_kind="棋谱",
                signature=signature(), submitted_by="赵", idempotency_key="x")
        with self.assertRaises(EventError):
            self.app.stations.submit_result(
                "east", g["game_id"], result_type="胜负", outcome="黑胜",
                record_hash=SHA("s"), record_kind="棋谱",
                signature={"signee": "赵"}, submitted_by="赵",
                idempotency_key="y")

    def test_forfeit_uses_forfeit_outcomes_only(self):
        self.app.stations.publish_pairings("east", 1, submitted_by="裁判长")
        g = self.game(1)
        with self.assertRaises(EventError):
            self.app.stations.submit_result(
                "east", g["game_id"], result_type="弃权", outcome="黑胜",
                record_hash=SHA("s"), record_kind="弃权单",
                signature=signature(), submitted_by="赵", idempotency_key="x")
        ev = self.app.stations.submit_result(
            "east", g["game_id"], result_type="弃权", outcome="白弃权",
            record_hash=SHA("弃权单"), record_kind="弃权单",
            signature=signature(), submitted_by="赵", idempotency_key="f1")
        self.assertEqual(ev.source, "临场成绩")

    def test_late_result_after_round_closed_is_new_event(self):
        play_round(self.app, "east", 1)
        # 关门后不能走普通通道
        g1 = self.game(1)
        with self.assertRaises(EventError):
            self.app.stations.submit_result(
                "east", g1["game_id"], result_type="胜负", outcome="黑胜",
                record_hash=SHA("late"), record_kind="棋谱",
                signature=signature(), submitted_by="赵", idempotency_key="late1")
        # 但第 1 轮已有成绩；构造一个无成绩局演示迟到通道：
        # 第 2 轮先冻结配对、暂不提交，即视为成绩迟到
        ev = self.app.stations.publish_pairings(
            "east", 2, submitted_by="裁判长", idempotency_key="pp2")
        # 第 1 轮关门后第 2 轮尚未关门——直接用迟到通道补录第 2 轮
        g2 = next(g for g in ev.payload["games"] if not g["is_bye"])
        late = self.app.stations.record_late_result(
            "east", g2["game_id"], result_type="胜负", outcome="黑胜",
            record_hash=SHA("late2"), record_kind="棋谱",
            signature=signature("钱裁判"), reason="记分牌故障延迟上传",
            accepted_by="裁判长", submitted_by="钱裁判",
            idempotency_key="late2")
        self.assertEqual(late.type, "LateResultRecorded")
        eff = self.app.stations.effective_result("east", g2["game_id"])
        self.assertTrue(eff["current"]["event_type"], "LateResultRecorded")

    def test_correction_appends_event_and_keeps_chain(self):
        play_round(self.app, "east", 1)
        g = self.game(1)
        self.app.stations.correct_result(
            "east", g["game_id"], result_type="胜负", outcome="白胜",
            record_hash=SHA("fixed"), record_kind="棋谱",
            signature=signature("钱裁判"), reason="第七轮结果更正：黑白记反",
            submitted_by="钱裁判", idempotency_key="c1")
        eff = self.app.stations.effective_result("east", g["game_id"])
        self.assertEqual(eff["current"]["outcome"], "白胜")
        self.assertEqual(eff["current"]["record_hash"], SHA("fixed"))
        self.assertEqual([h["type"] for h in eff["history"]],
                         ["GameResultSubmitted", "ResultCorrected"])

    def test_stale_correction_base_rejected(self):
        play_round(self.app, "east", 1)
        g = self.game(1)
        self.app.stations.correct_result(
            "east", g["game_id"], result_type="胜负", outcome="白胜",
            record_hash=SHA("fix1"), record_kind="棋谱",
            signature=signature(), reason="第一次纠错",
            submitted_by="钱", idempotency_key="c1")
        # 手工构造一条指向更早基准的纠错
        state = self.app.stations.effective.get("east", {})[g["game_id"]]
        first_seq = state["history"][0]["seq"]
        payload = {
            "station_id": "east", "game_id": g["game_id"],
            "group": "名手组", "round": 1,
            "black": g["black"], "white": g["white"],
            "result_type": "胜负", "outcome": "和棋",
            "record_hash": SHA("fix2"), "record_kind": "棋谱",
            "signature": signature(), "note": None,
            "reason": "过期基准纠错", "supersedes_seq": first_seq}
        with self.assertRaises(EventError):
            self.app.stations.store.append("ResultCorrected", payload)

    def test_appeal_overturn_changes_effective_outcome(self):
        play_round(self.app, "east", 1)
        g = self.game(1)
        self.app.stations.rule_appeal(
            "east", g["game_id"], appeal_id="A-1", action="改判",
            basis="申诉成功：提子未计数", effective_outcome="和棋",
            result_type="胜负", evidence_hashes=[SHA("证据")],
            ruled_by="仲裁委员会", submitted_by="仲裁",
            idempotency_key="ap1")
        eff = self.app.stations.effective_result("east", g["game_id"])
        self.assertEqual(eff["current"]["outcome"], "和棋")
        self.assertEqual(eff["appeal"]["action"], "改判")

    def test_withdrawal_marks_unplayed_games_as_forfeits(self):
        play_round(self.app, "east", 1)
        # p3 第 2 轮退赛：未下对局自动判负，名次标记 eligible=False
        self.app.stations.withdraw(
            "east", self.p3, "名手组", reason="伤病", from_round=2,
            submitted_by="裁判长", idempotency_key="w1")
        play_round(self.app, "east", 2)
        rows = self.app.stations.standings("east")["名手组"]
        p3row = next(r for r in rows if r["person_id"] == self.p3)
        self.assertFalse(p3row["eligible"])

    def test_finalize_requires_complete_results(self):
        play_round(self.app, "east", 1)
        self.app.stations.publish_pairings("east", 2, submitted_by="裁判长")
        with self.assertRaises(EventError):
            self.app.stations.finalize("east", submitted_by="admin")

    def test_registration_closes_after_first_pairing(self):
        self.app.stations.publish_pairings("east", 1, submitted_by="裁判长")
        from events import EventError
        with self.assertRaises(EventError):
            register(self.app, "east", "丁", "11010119900101004X", seq=4)

    def test_withdraw_after_finalize_rejected_use_federation_path(self):
        play_all_rounds(self.app, "east", 2)
        self.app.stations.finalize("east", submitted_by="admin",
                                   idempotency_key="fin2")
        with self.assertRaises(EventError):
            self.app.stations.withdraw(
                "east", self.p1, "名手组", reason="赛后伤退",
                submitted_by="裁判长", idempotency_key="w-late")

    def test_redelivered_result_after_finalize_keeps_single_fact(self):
        play_all_rounds(self.app, "east", 2)
        self.app.stations.finalize("east", submitted_by="admin",
                                   idempotency_key="fin")
        g = self.game(1)
        tail = self.app.store.tail_hash
        count = len(self.app.store)
        self.app.stations.submit_result(
            "east", g["game_id"], result_type="胜负", outcome="黑胜",
            record_hash=SHA(f"east-{g['game_id']}-r1"),
            record_kind="棋谱", signature=signature(),
            submitted_by="赵裁判",
            idempotency_key=f"res-east-{g['game_id']}-r1")
        self.assertEqual(self.app.store.tail_hash, tail)
        self.assertEqual(len(self.app.store), count)


if __name__ == "__main__":
    unittest.main()
