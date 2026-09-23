"""领域内核场景测试：冻结、结果更正、破同分、身份归并、一人一席、退赛递补、锁榜。"""

import unittest

from store import EventStore
from tournament.domain import TournamentService, DomainError

ORG = "organizer:zuwei"
REF = "referee:caipan"
JURY = "jury:zhongcai"
AUD = "auditor:shenhe"
STATION_ADMIN = "station_admin:guanli"


class DomainTestBase(unittest.TestCase):
    def setUp(self):
        self.svc = TournamentService(EventStore(":memory:"))
        self.svc.create_tournament("全国业余围棋大赛", actor=ORG)

    def open(self, sid="east", name="华东"):
        self.svc.open_station(sid, name, actor=ORG)

    def register(self, sid, gid, name, cert, *, phonetic=None, birth_year=None, actor=REF):
        ev = self.svc.register(sid, gid, name=name, cert_type="idcard", cert_no=cert,
                               phonetic=phonetic, birth_year=birth_year, actor=actor)
        return ev["payload"]["person_id"]

    def freeze(self, sid, gid, *, rounds=2, quota=1, tie_breaks=("sos", "cumulative", "draw")):
        self.svc.freeze_rules(sid, gid, {"rounds": rounds, "quota": quota,
                                         "tie_breaks": list(tie_breaks)}, actor=REF)
        self.svc.freeze_rounds(sid, gid, actor=REF)

    def pair(self, sid, gid, rnd, boards):
        self.svc.freeze_pairings(sid, gid, rnd, boards, actor=REF)

    def result(self, sid, gid, rnd, board, winner, **kw):
        kw.setdefault("record_hash", f"sha256:{sid}-{gid}-{rnd}-{board}")
        kw.setdefault("signature_source", f"signed:{REF}")
        return self.svc.submit_result(sid, gid, rnd, board, winner, actor=REF, **kw)

    def standings(self, sid, gid):
        return self.svc.standings(sid, gid)["rows"]


class FreezeTest(DomainTestBase):
    def test_rules_rounds_pairings_frozen_before_play(self):
        self.open()
        pid = self.register("east", "名手组", "甲", "110101199001010001")
        # 未冻结不能配对/交结果
        with self.assertRaises(DomainError) as e:
            self.pair("east", "名手组", 1, [{"board": "BYE1", "white": pid, "bye": True}])
        self.assertEqual(e.exception.code, "rules_not_frozen")
        self.freeze("east", "名手组", rounds=1, quota=1)
        # 冻结后拒绝报名
        with self.assertRaises(DomainError) as e:
            self.register("east", "名手组", "乙", "110101199001010002")
        self.assertEqual(e.exception.code, "rules_frozen")
        # 规则不可二次冻结
        with self.assertRaises(DomainError) as e:
            self.svc.freeze_rules("east", "名手组", {"rounds": 3, "quota": 2}, actor=REF)
        self.assertEqual(e.exception.code, "rules_frozen")
        # 配对必须覆盖全部在赛棋手
        with self.assertRaises(DomainError) as e:
            self.pair("east", "名手组", 1, [{"board": "BYE1", "white": "x", "bye": True}])
        self.assertEqual(e.exception.code, "bad_board")
        self.pair("east", "名手组", 1, [{"board": "BYE1", "white": pid, "bye": True}])
        # 配对冻结不可改
        with self.assertRaises(DomainError) as e:
            self.pair("east", "名手组", 1, [{"board": "BYE1", "white": pid, "bye": True}])
        self.assertEqual(e.exception.code, "pairings_frozen")
        # 不在冻结表里的台次不能交结果
        with self.assertRaises(DomainError) as e:
            self.result("east", "名手组", 1, "B9", "white")
        self.assertEqual(e.exception.code, "board_not_frozen")
        # 轮空台自动计胜，不接受结果
        with self.assertRaises(DomainError) as e:
            self.result("east", "名手组", 1, "BYE1", "white")
        self.assertEqual(e.exception.code, "bad_board")

    def test_rule_version_used_in_confirmation(self):
        self.open()
        pid = self.register("east", "名手组", "甲", "110101199001010001")
        self.freeze("east", "名手组", rounds=1, quota=1)
        self.pair("east", "名手组", 1, [{"board": "BYE1", "white": pid, "bye": True}])
        ev = self.svc.confirm_group("east", "名手组", actor=STATION_ADMIN)
        rv = ev["payload"]["rule_version"]
        self.assertTrue(rv.startswith("rv-"))
        self.assertEqual(ev["payload"]["qualified"][0]["rule_version"], rv)


class ResultIntegrityTest(DomainTestBase):
    def _two_players(self):
        self.open()
        a = self.register("east", "名手组", "甲", "110101199001010001", phonetic="jia")
        b = self.register("east", "名手组", "乙", "110101199001010002", phonetic="yi")
        self.freeze("east", "名手组", rounds=2, quota=1)
        return a, b

    def test_record_hash_and_signature_persisted(self):
        a, b = self._two_players()
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        ev = self.result("east", "名手组", 1, "B1", "white",
                         record_hash="sha256:abc123", signature_source="signed:裁判李#001")
        self.assertEqual(ev["payload"]["record_hash"], "sha256:abc123")
        self.assertEqual(ev["payload"]["signature_source"], "signed:裁判李#001")

    def test_redeliver_same_fact_does_not_change_anything(self):
        a, b = self._two_players()
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        self.result("east", "名手组", 1, "B1", "white",
                    record_hash="sha256:1", signature_source="s1")
        total_events = len(self.svc.store.read_all())
        with self.assertRaises(DomainError) as e:
            self.result("east", "名手组", 1, "B1", "white")
        self.assertEqual(e.exception.code, "duplicate_fact")
        self.assertEqual(len(self.svc.store.read_all()), total_events)
        # 不同结果必须走更正
        with self.assertRaises(DomainError) as e:
            self.result("east", "名手组", 1, "B1", "black")
        self.assertEqual(e.exception.code, "result_exists")

    def test_missing_record_hash_or_signature_rejected(self):
        a, b = self._two_players()
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        with self.assertRaises(DomainError) as e:
            self.svc.submit_result("east", "名手组", 1, "B1", "white",
                                   signature_source="s1", actor=REF)  # 缺棋谱哈希
        self.assertEqual(e.exception.code, "missing_record_hash")
        with self.assertRaises(DomainError) as e:
            self.svc.submit_result("east", "名手组", 1, "B1", "white",
                                   record_hash="sha256:1", actor=REF)  # 缺签字来源
        self.assertEqual(e.exception.code, "missing_signature")

    def test_late_result_requires_late_event(self):
        a, b = self._two_players()
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        self.pair("east", "名手组", 2, [{"board": "B1", "white": a, "black": b}])
        # 先交第二轮，第一轮进入迟到窗口
        self.result("east", "名手组", 2, "B1", "white")
        with self.assertRaises(DomainError) as e:
            self.result("east", "名手组", 1, "B1", "white")
        self.assertEqual(e.exception.code, "late_window")
        ev = self.result("east", "名手组", 1, "B1", "white", late=True)
        self.assertEqual(ev["type"], "late_result_submitted")

    def test_correction_appends_and_marks_old_superseded(self):
        a, b = self._two_players()
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        old = self.result("east", "名手组", 1, "B1", "white")
        cev = self.svc.correct_result("east", "名手组", 1, "B1", "black",
                                      "第七轮结果更正：核对签字表", actor=REF,
                                      record_hash="sha256:corrected",
                                      signature_source="signed:裁判长")
        self.assertEqual(cev["type"], "result_corrected")
        self.assertEqual(cev["payload"]["old_event_id"], old["event_id"])
        # 旧事件仍在
        self.assertIsNotNone(self.svc._find_event(old["event_id"]))
        # 当前生效结果为新事件
        gs = self.svc._load().groups[("east", "名手组")]
        current = self.svc._current_outcome(gs, 1, "B1")
        self.assertEqual(current.result, "win_black")
        self.assertEqual(current.correction_of, old["event_id"])
        # 不能对同一事实再更正
        with self.assertRaises(DomainError) as e:
            self.svc.correct_result("east", "名手组", 1, "B1", "black", "x", actor=REF)
        self.assertEqual(e.exception.code, "same_fact")

    def test_appeal_uphold_and_amend(self):
        a, b = self._two_players()
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        self.result("east", "名手组", 1, "B1", "white")
        appeal = self.svc.file_appeal("east", "名手组", 1, "B1", "乙方申诉数子争议", actor=REF)
        aid = appeal["payload"]["appeal_id"]
        # 维持原判：事实不变
        self.svc.decide_appeal("east", "名手组", aid, "uphold", reason="维持",
                               panel="仲裁组", actor=JURY)
        gs = self.svc._load().groups[("east", "名手组")]
        self.assertEqual(self.svc._current_outcome(gs, 1, "B1").result, "win_white")
        # 再次申诉改判
        appeal2 = self.svc.file_appeal("east", "名手组", 1, "B1", "二次申诉", actor=REF)
        ev = self.svc.decide_appeal("east", "名手组", appeal2["payload"]["appeal_id"],
                                    "amend", winner="black", reason="复核棋谱哈希后改判",
                                    panel="仲裁组", actor=JURY,
                                    record_hash="sha256:appeal-amend",
                                    signature_source="signed:仲裁组")
        self.assertEqual(ev["type"], "appeal_amended")
        gs = self.svc._load().groups[("east", "名手组")]
        self.assertEqual(self.svc._current_outcome(gs, 1, "B1").result, "win_black")

    def test_forfeit_double_loss(self):
        a, b = self._two_players()
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        ev = self.svc.submit_forfeit("east", "名手组", 1, "B1", "none", "双方弃权",
                                     signature_source="signed:裁判长", actor=REF)
        self.assertEqual(ev["payload"]["points_white"], 0.0)
        self.assertEqual(ev["payload"]["points_black"], 0.0)


class TieBreakTest(DomainTestBase):
    def test_sos_differentiates_tied_players_with_byes(self):
        self.open()
        pids = [self.register("east", "名手组", n, c)
                for n, c in [("甲", "110101199001010001"), ("乙", "110101199001010002"),
                             ("丙", "110101199001010003"), ("丁", "110101199001010004"),
                             ("戊", "110101199001010005")]]
        a, b, c, d, e = pids
        self.freeze("east", "名手组", rounds=3, quota=1)
        # R1: 甲胜乙、丙胜丁、戊轮空
        self.pair("east", "名手组", 1, [
            {"board": "B1", "white": a, "black": b},
            {"board": "B2", "white": c, "black": d},
            {"board": "BYE1", "white": e, "bye": True}])
        self.result("east", "名手组", 1, "B1", "white")
        self.result("east", "名手组", 1, "B2", "white")
        # R2: 甲胜戊、丙胜乙、丁轮空
        self.pair("east", "名手组", 2, [
            {"board": "B1", "white": a, "black": e},
            {"board": "B2", "white": c, "black": b},
            {"board": "BYE2", "white": d, "bye": True}])
        self.result("east", "名手组", 2, "B1", "white")
        self.result("east", "名手组", 2, "B2", "white")
        # R3: 甲胜丙、丁胜戊、乙轮空
        self.pair("east", "名手组", 3, [
            {"board": "B1", "white": a, "black": c},
            {"board": "B2", "white": d, "black": e},
            {"board": "BYE3", "white": b, "bye": True}])
        self.result("east", "名手组", 3, "B1", "white")
        self.result("east", "名手组", 3, "B2", "white")
        rows = self.standings("east", "名手组")
        by_name = {r["name"]: r for r in rows}
        order = [r["name"] for r in rows]
        # 甲 3 分第一；丙、丁同为 2 分，丙对手分(对甲/乙/丁)高于丁 → 丙列前
        self.assertEqual(order, ["甲", "丙", "丁", "戊", "乙"])
        self.assertEqual(by_name["丙"]["score"], by_name["丁"]["score"])
        self.assertGreater(by_name["丙"]["sos"], by_name["丁"]["sos"])
        self.assertEqual([r["rank_no"] for r in rows], [1, 2, 3, 4, 5])  # 全序无并列

    def test_explanation_cites_frozen_rule_version_and_path(self):
        self.open()
        pids = [self.register("east", "名手组", n, c, phonetic=n)
                for n, c in [("甲", "110101199001010001"), ("乙", "110101199001010002"),
                             ("丙", "110101199001010003"), ("丁", "110101199001010004")]]
        a, b, c, d = pids
        self.freeze("east", "名手组", rounds=2, quota=1)
        self.pair("east", "名手组", 1,
                  [{"board": "B1", "white": a, "black": b},
                   {"board": "B2", "white": c, "black": d}])
        self.result("east", "名手组", 1, "B1", "white")
        self.result("east", "名手组", 1, "B2", "black")
        sug = self.svc.suggest_next_pairings("east", "名手组", 2)
        self.pair("east", "名手组", 2, sug)
        for bd in sug:
            self.result("east", "名手组", 2, bd["board"], "white")
        self.svc.confirm_group("east", "名手组", actor=STATION_ADMIN)
        winner = self.standings("east", "名手组")[0]
        ex = self.svc.explain_rank("east", "名手组", winner["player_id"])
        self.assertTrue(ex["rule_version"].startswith("rv-"))
        self.assertEqual(ex["rules_snapshot"]["tie_breaks"][0], "score")
        self.assertIn("draw", ex["rules_snapshot"]["tie_breaks"])
        self.assertTrue(ex["on_quota_line"])
        self.assertEqual(ex["quota"], 1)
        # 破同分路径逐级可比
        self.assertTrue(any(c["decides"] for c in ex["tiebreak_path"]))

    def test_draw_tiebreak_is_deterministic(self):
        from tournament.standing import GameRecord, compute_tables, rank_players
        games = [
            GameRecord(1, "B1", "a", "b", "win_white", 1, 0, "e1"),
            GameRecord(1, "B2", "c", "c", "bye_win", 1, 0, "bye1"),
        ]
        tabs = compute_tables(games, ["a", "b", "c"], 1)
        r1 = rank_players(tabs, ["score", "sos", "cumulative", "draw"], draw_seed="seed")
        r2 = rank_players(tabs, ["score", "sos", "cumulative", "draw"], draw_seed="seed")
        self.assertEqual([r["player_id"] for r in r1], [r["player_id"] for r in r2])
        self.assertEqual(len({r["rank_no"] for r in r1}), 3)  # 全序，无并列


class IdentityTest(DomainTestBase):
    def test_same_cert_across_stations_is_same_person(self):
        self.open("east", "华东")
        self.open("south", "华南")
        p1 = self.register("east", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        p2 = self.register("south", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        self.assertEqual(p1, p2)
        # 同一赛区不可跨组重复报名
        with self.assertRaises(DomainError) as e:
            self.register("east", "名人组", "王伟", "110101199001011234")
        self.assertEqual(e.exception.code, "duplicate_registration")

    def test_homophone_names_only_flag_not_merged(self):
        self.open("east", "华东")
        self.register("east", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        self.register("east", "名人组", "汪威", "E12345678", phonetic="wangwei")
        flags = self.svc.review_flags()
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["status"], "open")
        self.svc.resolve_similar(flags[0]["flag_id"], "distinct", actor=AUD)
        self.assertEqual(self.svc.review_flags(), [])

    def test_two_cert_types_require_explicit_merge(self):
        self.open("east", "华东")
        self.open("north", "华北")
        p1 = self.register("east", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        p2 = self.register("north", "名人组", "王伟", "G98765432", phonetic="wangwei")
        self.assertNotEqual(p1, p2)
        with self.assertRaises(DomainError):
            self.svc.resolve_similar("nonexistent", "distinct", actor=AUD)
        self.svc.merge_persons(p2, p1, "审核员核对两证为同一人", actor=AUD)
        tr = self.svc.trace(p1)
        stations = {l["station_id"] for l in tr["locations"]}
        self.assertEqual(stations, {"east", "north"})
        # 旧 id 重定向
        tr_old = self.svc.trace(p2)
        self.assertEqual({l["station_id"] for l in tr_old["locations"]}, {"east", "north"})

    def test_merge_rejected_when_cross_group_in_same_station(self):
        self.open("east")
        p1 = self.register("east", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        p2 = self.register("east", "名人组", "汪卫", "E12340000", phonetic="wangwei")
        with self.assertRaises(DomainError) as e:
            self.svc.merge_persons(p2, p1, "误并", actor=AUD)
        self.assertEqual(e.exception.code, "bad_merge")

    def test_homophone_merged_decision_requires_prior_merge(self):
        self.open("east", "华东")
        p1 = self.register("east", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        p2 = self.register("east", "名人组", "汪威", "E12345678", phonetic="wangwei")
        flag = self.svc.review_flags()[0]
        # 未合并直接裁同人 → 拒绝
        with self.assertRaises(DomainError) as e:
            self.svc.resolve_similar(flag["flag_id"], "merged", actor=AUD)
        self.assertEqual(e.exception.code, "need_merge")
        # 同赛区跨组合并被规则拒绝（本就不可能是同人重复报名）；构造跨站可合并场景
        self.open("north", "华北")
        p3 = self.register("north", "名人组", "王伟", "G98765432", phonetic="wangwei")
        flags = self.svc.review_flags()
        f3 = next(f for f in flags if {f["person_a"], f["person_b"]} == {p1, p3})
        self.svc.merge_persons(p3, p1, "两证同人", actor=AUD)
        ev = self.svc.resolve_similar(f3["flag_id"], "merged", actor=AUD)
        self.assertEqual(ev["payload"]["decision"], "merged")
        remaining_ids = {f["flag_id"] for f in self.svc.review_flags()}
        self.assertNotIn(f3["flag_id"], remaining_ids)
        self.assertIn(flag["flag_id"], remaining_ids)  # 汪威/王伟 跨组单仍未决

    def test_full_identity_never_in_events_and_cross_view_minimized(self):
        self.open("east", "华东")
        self.open("south", "华南")
        p1 = self.register("east", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        self.register("south", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        for ev in self.svc.store.read_all():
            self.assertNotIn("110101199001011234", str(ev["payload"]))
        view = self.svc.trace(p1, viewer_station="east")
        south = [l for l in view["locations"] if l["station_id"] == "south"][0]
        self.assertNotIn("name", south)
        self.assertNotIn("cert", south)
        self.assertIn("status", south)


class QualificationTest(DomainTestBase):
    def test_one_person_one_seat_cross_station_promotes_runner_up(self):
        self.open("east", "华东")
        self.open("south", "华南")
        # 华东：王伟（共用证件）第一、李雷第二
        w_e = self.register("east", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        l1 = self.register("east", "名手组", "李雷", "110101199002020001", phonetic="lilei")
        self.freeze("east", "名手组", rounds=1, quota=1)
        self.pair("east", "名手组", 1,
                  [{"board": "B1", "white": w_e, "black": l1}])
        self.result("east", "名手组", 1, "B1", "white")
        ev_e = self.svc.confirm_group("east", "名手组", actor=STATION_ADMIN)
        self.assertEqual(ev_e["payload"]["qualified"][0]["person_id"], w_e)

        # 华南：王伟再次第一、韩梅第二
        w_s = self.register("south", "名手组", "王伟", "110101199001011234", phonetic="wangwei")
        hm = self.register("south", "名手组", "韩梅", "110101199003030001", phonetic="hanmei")
        self.assertEqual(w_s, w_e)
        self.freeze("south", "名手组", rounds=1, quota=1)
        self.pair("south", "名手组", 1,
                  [{"board": "B1", "white": w_s, "black": hm}])
        self.result("south", "名手组", 1, "B1", "white")
        ev_s = self.svc.confirm_group("south", "名手组", actor=STATION_ADMIN)
        qualified = ev_s["payload"]["qualified"]
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["person_id"], hm)  # 顺延给韩梅
        excluded = ev_s["payload"]["excluded_duplicates"]
        self.assertEqual(excluded[0]["person_id"], w_e)
        self.assertEqual(excluded[0]["occupied_at"], "east/名手组")
        # 全局一人一席
        seats = self.svc._load().seats
        self.assertEqual(len(seats), 2)  # 王伟占华东、韩梅占华南
        self.assertNotIn(w_e, {q["person_id"] for q in qualified})

    def test_seventh_round_correction_after_confirm_recalculates_and_notifies(self):
        self.open()
        a = self.register("east", "名手组", "甲", "110101199001010001", phonetic="a")
        b = self.register("east", "名手组", "乙", "110101199001010002", phonetic="b")
        self.freeze("east", "名手组", rounds=7, quota=1)
        # 前六轮 3:3，第七轮原判甲胜
        for rnd in range(1, 7):
            winner = a if rnd <= 3 else b
            boards = [{"board": "B1", "white": a, "black": b}]
            self.pair("east", "名手组", rnd, boards)
            self.result("east", "名手组", rnd, "B1",
                        "white" if winner == a else "black")
        self.pair("east", "名手组", 7, [{"board": "B1", "white": a, "black": b}])
        self.result("east", "名手组", 7, "B1", "white",
                    record_hash="sha256:old", signature_source="paper:v1")
        ev = self.svc.confirm_group("east", "名手组", actor=STATION_ADMIN)
        self.assertEqual(ev["payload"]["qualified"][0]["person_id"], a)

        # 第七轮结果更正：乙胜 → 名次翻转
        self.svc.correct_result("east", "名手组", 7, "B1", "black",
                                "第七轮结果更正：签字来源复核", actor=REF,
                                record_hash="sha256:new", signature_source="paper:v2")
        rows = self.standings("east", "名手组")
        self.assertEqual(rows[0]["player_id"], b)
        # 已确认组产生递补事件
        gs = self.svc._load().groups[("east", "名手组")]
        self.assertEqual(gs.qualified[0]["person_id"], b)
        kinds = {n["kind"] for n in self.svc.notifications("east")}
        self.assertIn("promoted", kinds)
        self.assertIn("seat_lost", kinds)
        # 轨迹保留两版事实
        tr = self.svc.trace(a)
        last = [g for g in tr["locations"][0]["games"] if g["round_no"] == 7][0]
        self.assertTrue(last["corrected"])
        self.assertEqual(last["result"], "win_black")

    def test_withdraw_promotes_runner_up_and_only_touches_group(self):
        self.open()
        a = self.register("east", "名手组", "甲", "110101199001010001", phonetic="a")
        b = self.register("east", "名手组", "乙", "110101199001010002", phonetic="b")
        self.freeze("east", "名手组", rounds=1, quota=1)
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        self.result("east", "名手组", 1, "B1", "white")
        self.svc.confirm_group("east", "名手组", actor=STATION_ADMIN)
        # 另一组不受影响
        c = self.register("east", "名师组", "丙", "110101199003030001", phonetic="c")
        self.freeze("east", "名师组", rounds=1, quota=1)
        self.pair("east", "名师组", 1, [{"board": "BYE1", "white": c, "bye": True}])
        self.svc.confirm_group("east", "名师组", actor=STATION_ADMIN)

        self.svc.withdraw("east", "名手组", a, 1, "赛前伤病退赛", actor=REF)
        gs = self.svc._load().groups[("east", "名手组")]
        self.assertEqual(gs.qualified[0]["person_id"], b)
        note_scopes = {n["scope"] for n in self.svc.notifications("east")
                       if n["kind"] in ("promoted", "seat_lost")}
        self.assertEqual(note_scopes, {"east/名手组"})  # 名师组无递补通知

    def test_withdraw_before_round_excludes_from_pairings(self):
        self.open()
        pids = [self.register("east", "名手组", n, c)
                for n, c in [("甲", "110101199001010001"), ("乙", "110101199001010002"),
                             ("丙", "110101199001010003"), ("丁", "110101199001010004")]]
        a, b, c, d = pids
        self.freeze("east", "名手组", rounds=2, quota=1)
        self.pair("east", "名手组", 1, [
            {"board": "B1", "white": a, "black": b},
            {"board": "B2", "white": c, "black": d}])
        self.result("east", "名手组", 1, "B1", "white")
        self.result("east", "名手组", 1, "B2", "white")
        # 甲在第二轮前退赛
        self.svc.withdraw("east", "名手组", a, 2, "伤病", actor=REF)
        sug = self.svc.suggest_next_pairings("east", "名手组", 2)
        players = {x for bd in sug for x in ((bd["white"], bd.get("black")) if not bd.get("bye") else (bd["white"],))}
        self.assertNotIn(a, players)
        # 冻结配对时退赛者不得出现
        with self.assertRaises(DomainError) as e:
            self.pair("east", "名手组", 2, [
                {"board": "B1", "white": a, "black": b},
                {"board": "B2", "white": c, "black": d}])
        self.assertEqual(e.exception.code, "bad_board")

    def test_withdraw_after_frozen_pairing_autoforfeits_unplayed(self):
        self.open()
        pids = [self.register("east", "名手组", n, c)
                for n, c in [("甲", "110101199001010001"), ("乙", "110101199001010002")]]
        a, b = pids
        self.freeze("east", "名手组", rounds=2, quota=1)
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        self.result("east", "名手组", 1, "B1", "white")
        self.pair("east", "名手组", 2, [{"board": "B1", "white": a, "black": b}])
        # 第二轮配对已冻结但未赛，甲退赛 → 乙自动弃权胜
        self.svc.withdraw("east", "名手组", a, 2, "伤病", actor=REF)
        gs = self.svc._load().groups[("east", "名手组")]
        oc = self.svc._current_outcome(gs, 2, "B1")
        self.assertEqual(oc.result, "forfeit_win_black")
        self.assertTrue(oc.signature_source.startswith("withdraw:"))
        # 甲虽按累进分仍列前，但退赛不占名额；确认后资格归乙
        ev = self.svc.confirm_group("east", "名手组", actor=STATION_ADMIN)
        self.assertEqual(ev["payload"]["qualified"][0]["person_id"], b)
        rows = self.standings("east", "名手组")
        a_row = next(r for r in rows if r["player_id"] == a)
        self.assertTrue(a_row["withdrawn"])
        self.assertFalse(a_row["qualified_here"])
        b_row = next(r for r in rows if r["player_id"] == b)
        self.assertTrue(b_row["qualified_here"])


    def test_reconfirm_promotes_after_seat_freed_elsewhere(self):
        self.open("east", "华东")
        self.open("south", "华南")
        # 华东：王伟胜李雷，王伟占席
        w = self.register("east", "名手组", "王伟", "110101199001011234", phonetic="ww")
        li = self.register("east", "名手组", "李雷", "110101199002020001", phonetic="ll")
        self.freeze("east", "名手组", rounds=1, quota=1)
        self.pair("east", "名手组", 1, [{"board": "B1", "white": w, "black": li}])
        self.result("east", "名手组", 1, "B1", "white")
        self.svc.confirm_group("east", "名手组", actor=STATION_ADMIN)
        # 华南：王伟胜韩梅，王伟被排除、韩梅递补
        self.register("south", "名手组", "王伟", "110101199001011234", phonetic="ww")
        han = self.register("south", "名手组", "韩梅", "110101199003030001", phonetic="hm")
        self.freeze("south", "名手组", rounds=1, quota=1)
        self.pair("south", "名手组", 1, [{"board": "B1", "white": w, "black": han}])
        self.result("south", "名手组", 1, "B1", "white")
        self.svc.confirm_group("south", "名手组", actor=STATION_ADMIN)
        # 华东更正：李雷反胜 → 王伟失席；系统不擅改华南，仅发复核建议
        self.svc.correct_result("east", "名手组", 1, "B1", "black", "复核更正", actor=REF,
                                record_hash="sha256:x", signature_source="signed:裁判长")
        notes = self.svc.notifications("south")
        self.assertTrue(any(n["kind"] == "review_suggested" for n in notes))
        # 组委会对华南发起复核递补：王伟取回华南席位（韩梅让出席位）
        ev = self.svc.reconfirm_group("south", "名手组", actor=ORG)
        qualified = [q["person_id"] for q in ev["payload"]["qualified"]]
        self.assertIn(w, qualified)
        self.assertNotIn(han, qualified)


class FinalsLockTest(DomainTestBase):
    def _minimal_group(self, sid, gid, seq):
        pid = self.register(sid, gid, f"{sid}-{gid}", f"1101011990010{seq:05d}")
        self.svc.freeze_rules(sid, gid, {"rounds": 1, "quota": 1}, actor=REF)
        self.svc.freeze_rounds(sid, gid, actor=REF)
        self.pair(sid, gid, 1, [{"board": "BYE1", "white": pid, "bye": True}])
        self.svc.confirm_group(sid, gid, actor=STATION_ADMIN)

    def test_lock_then_all_writes_rejected_and_redelivery_keeps_fact(self):
        seq = 1
        for sid, nm in [("east", "华东"), ("south", "华南"), ("west", "华西"), ("north", "华北")]:
            self.open(sid, nm)
            for gid in ("名手组", "名师组", "名人组"):
                self._minimal_group(sid, gid, seq)
                seq += 1
        ev = self.svc.lock_finals("2026 总决赛", actor=ORG)
        self.assertTrue(ev["payload"]["roster_hash"])
        self.assertEqual(len(ev["payload"]["roster"]), 12)
        roster = self.svc.finals_roster()
        self.assertEqual(len(roster["roster"]), 12)
        # 锁定后一切写命令被拒
        with self.assertRaises(DomainError) as e:
            self.svc.correct_result("east", "名手组", 1, "BYE1", "black", "x", actor=REF)
        self.assertEqual(e.exception.code, "finals_locked")
        with self.assertRaises(DomainError) as e:
            self.register("east", "名手组", "新", "110101199009090001")
        self.assertEqual(e.exception.code, "finals_locked")
        with self.assertRaises(DomainError) as e:
            self.svc.lock_finals("再次锁定", actor=ORG)
        self.assertEqual(e.exception.code, "finals_locked")
        # 只读解释仍可用，且重投被拒前后名单哈希不变
        roster_before = self.svc.finals_roster()["roster_hash"]
        with self.assertRaises(DomainError):
            self.svc.submit_result("east", "名手组", 1, "BYE1", "white",
                                   record_hash="x", signature_source="y", actor=REF)
        self.assertEqual(self.svc.finals_roster()["roster_hash"], roster_before)
        st = self.svc.standings("east", "名手组")
        self.assertTrue(st["rows"][0]["qualified_here"])
        pid = st["rows"][0]["player_id"]
        ex = self.svc.explain_rank("east", "名手组", pid)
        self.assertTrue(ex["rule_version"].startswith("rv-"))
        tr = self.svc.trace(pid)
        self.assertTrue(any(l["status"] == "qualified" for l in tr["locations"]))
        self.assertTrue(self.svc.verify()["ok"])

    def test_lock_requires_all_groups_confirmed(self):
        self.open("east")
        with self.assertRaises(DomainError) as e:
            self.svc.lock_finals("总决赛", actor=ORG)
        self.assertEqual(e.exception.code, "not_all_confirmed")
        self.assertIn("east/名手组", e.exception.message)


class ChainTest(DomainTestBase):
    def test_chain_valid_after_complex_scenario(self):
        self.open()
        a = self.register("east", "名手组", "甲", "110101199001010001", phonetic="a")
        b = self.register("east", "名手组", "乙", "110101199001010002", phonetic="b")
        self.freeze("east", "名手组", rounds=1, quota=1)
        self.pair("east", "名手组", 1, [{"board": "B1", "white": a, "black": b}])
        self.result("east", "名手组", 1, "B1", "white")
        self.svc.file_appeal("east", "名手组", 1, "B1", "申诉", actor=REF)
        self.assertTrue(self.svc.verify()["ok"])
if __name__ == "__main__":
    unittest.main()
