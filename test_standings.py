"""破同分次序：逐级切分、抽签可复现、重算确定性。"""

import unittest

from events import EventError
from app import Application
from rules import RuleBook
from standings import compute_standings


def book(tiebreak):
    return RuleBook({
        "station_id": "t", "rules_version": "r1",
        "groups": [{"name": "名手组", "quota": 2}],
        "total_quota": 2, "rounds": 3, "tiebreak": tiebreak,
        "pairing_policy": "瑞士制", "komi": 7.5, "draw_seed": "seed-1",
        "notes": ""})


def game(rnd, g_id, black, white, outcome):
    return {"round": rnd, "game_id": g_id, "black": black, "white": white,
            "outcome": outcome, "is_bye": False, "group": "名手组"}


def bye(rnd, g_id, pid):
    return {"round": rnd, "game_id": g_id, "black": pid, "white": None,
            "outcome": None, "is_bye": True, "group": "名手组"}


class StandingsTest(unittest.TestCase):
    def test_total_order_even_when_all_tiebreakers_tie(self):
        rb = book(["对手分总和", "抽签"])
        # 两人完全镜像（互为对手、和棋）：积分、对手分全平 → 抽签
        games = [game(1, "g1", "A", "B", "和棋")]
        rows = compute_standings(rb, "名手组", ["A", "B"], games)
        self.assertEqual([r["rank"] for r in rows], [1, 2])

    def test_draw_is_deterministic_with_frozen_seed(self):
        rb = book(["对手分总和", "抽签"])
        games = [game(1, "g1", "A", "B", "和棋")]
        r1 = [(r["person_id"], r["rank"])
              for r in compute_standings(rb, "名手组", ["A", "B"], games)]
        r2 = [(r["person_id"], r["rank"])
              for r in compute_standings(rb, "名手组", ["A", "B"], games)]
        self.assertEqual(r1, r2)
        # 不同种子应可改变结果（至少键不同）
        rb2 = book(["对手分总和", "抽签"])
        rb2.data["draw_seed"] = "seed-2"
        keys1 = {r["person_id"]: r["metrics"]["抽签"] for r in
                 compute_standings(rb, "名手组", ["A", "B"], games)}
        keys2 = {r["person_id"]: r["metrics"]["抽签"] for r in
                 compute_standings(rb2, "名手组", ["A", "B"], games)}
        self.assertNotEqual(keys1, keys2)

    def test_sos_decides_equal_scores(self):
        rb = book(["对手分总和", "抽签"])
        # A 胜 C(弱)，B 胜 D(强)；C/D 之间 D 胜 C，使 B 的对手分更高
        games = [
            game(1, "g1", "A", "C", "黑胜"),
            game(1, "g2", "D", "B", "白胜"),
            game(2, "g3", "D", "C", "黑胜"),
            bye(2, "b1", "A"), bye(2, "b2", "B"),
        ]
        rows = compute_standings(
            rb, "名手组", ["A", "B", "C", "D"], games)
        order = [r["person_id"] for r in rows]
        # A、B 全胜 2 分；B 的对手 D 有分，A 的对手 C 0 分 → B 在前
        self.assertLess(order.index("B"), order.index("A"))
        b_row = next(r for r in rows if r["person_id"] == "B")
        self.assertIn("对手分总和", b_row["metrics"])

    def test_bye_counts_as_win(self):
        rb = book(["对手分总和", "抽签"])
        games = [bye(1, "b1", "A"), game(1, "g1", "B", "C", "黑胜")]
        rows = compute_standings(rb, "名手组", ["A", "B", "C"], games)
        a = next(r for r in rows if r["person_id"] == "A")
        self.assertEqual(a["score"], 1.0)

    def test_withdrawn_player_ineligible(self):
        rb = book(["对手分总和", "抽签"])
        games = [game(1, "g1", "A", "B", "黑胜")]
        rows = compute_standings(rb, "名手组", ["A", "B"], games,
                                 withdrawn={"B"})
        b = next(r for r in rows if r["person_id"] == "B")
        self.assertFalse(b["eligible"])

    def test_rank_is_ordinal_not_cluster_index(self):
        rb = book(["对手分总和", "累进分", "抽签"])
        # 构造 3 人 2 轮：1 个 2 分，2 个 1 分
        games = [
            game(1, "g1", "A", "B", "黑胜"),
            bye(1, "b1", "C"),
            game(2, "g2", "A", "C", "黑胜"),
            bye(2, "b2", "B"),
        ]
        rows = compute_standings(rb, "名手组", ["A", "B", "C"], games)
        ranks = sorted(r["rank"] for r in rows)
        self.assertEqual(ranks, [1, 2, 3])

    def test_swiss_avoids_repeat_pairing(self):
        from standings import swiss_pairings
        rb = book(["对手分总和", "抽签"])
        games = [game(1, "g1", "A", "B", "黑胜"),
                 game(1, "g2", "C", "D", "黑胜")]
        used = {("A", "B"), ("C", "D")}
        r2 = swiss_pairings(rb, "名手组", ["A", "B", "C", "D"], games, 2,
                            previous_pairs=used)
        pairs = {tuple(sorted((g["black"], g["white"])))
                 for g in r2 if not g["is_bye"]}
        self.assertTrue(pairs)
        self.assertFalse(pairs & used)


if __name__ == "__main__":
    unittest.main()
