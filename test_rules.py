"""规则冻结与破同分次序测试。"""

import unittest

from events import EventError
from app import Application


class RulesFreezeTest(unittest.TestCase):
    def setUp(self):
        self.app = Application()

    def freeze(self, **over):
        raw = {
            "station_id": "east", "rules_version": "r1",
            "groups": [{"name": "名手组", "quota": 4},
                       {"name": "名师组", "quota": 3},
                       {"name": "名人组", "quota": 3}],
            "rounds": 7,
            "tiebreak": ["对手分总和", "对手分削去", "累进分",
                         "直接对局", "所胜对手分", "抽签"],
        }
        raw.update(over)
        return self.app.stations.freeze(raw, submitted_by="admin")

    def test_ten_quotas_across_three_groups(self):
        self.freeze()
        book = self.app.stations.stations["east"]
        self.assertEqual(book["total_quota"], 10)

    def test_double_freeze_rejected(self):
        self.freeze()
        with self.assertRaises(EventError):
            self.freeze()

    def test_tiebreak_must_end_with_draw(self):
        with self.assertRaises(EventError):
            self.freeze(tiebreak=["对手分总和", "累进分"])

    def test_tiebreak_steps_unique(self):
        with self.assertRaises(EventError):
            self.freeze(tiebreak=["对手分总和", "对手分总和", "抽签"])

    def test_unknown_group_rejected(self):
        with self.assertRaises(EventError):
            self.app.stations.freeze({
                "station_id": "west", "rules_version": "r1",
                "groups": [{"name": "少儿组", "quota": 1}],
                "rounds": 3, "tiebreak": ["抽签"]}, submitted_by="admin")

    def test_rules_version_attached_to_pairings(self):
        self.freeze()
        self.app.identity.register(
            station_id="east", group="名手组", name="甲",
            documents=[{"type": "护照", "number": "E1"}],
            submitted_by="admin", idempotency_key="r1")
        ev = self.app.stations.publish_pairings(
            "east", 1, submitted_by="裁判长", idempotency_key="p1")
        self.assertEqual(ev.payload["rules_version"], "r1")


if __name__ == "__main__":
    unittest.main()
