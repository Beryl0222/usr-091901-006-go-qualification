"""总决赛核算：跨区去重、递补、通知范围、锁定、解释、重放。"""

import tempfile
import unittest
from pathlib import Path

from events import EventError
from test_helpers import World, register, freeze_station, play_all_rounds, winner_outcome


class FederationTest(unittest.TestCase):
    def setUp(self):
        self.world = World(quota=1, rounds=1)
        self.app = self.world.app

    def two_stations_shared_player(self):
        # 两站都让名单第 0 位（张三）赢，确保他在两站都直接占位
        first_wins = lambda g, b, w: "黑胜" if b == 0 else "白胜"
        east = self.world.setup_station(
            "east", [("张三", "11010119900101123X"),
                     ("李四", "11010219900202456X")],
            decide=first_wins)
        west = self.world.setup_station(
            "west", [("张三", "110101900101123"),
                     ("王五", "11010319900303789X")],
            decide=first_wins)
        self.assertEqual(east[0], west[0])
        self.app.federation.freeze_federation_rules(
            expected_stations=["east", "west"],
            zone_priority=["east", "west"], submitted_by="org",
            idempotency_key="fr")
        self.app.federation.project_station(
            "east", submitted_by="org", idempotency_key="pe")
        self.app.federation.project_station(
            "west", submitted_by="org", idempotency_key="pw")
        return east, west

    def test_lock_blocks_when_one_person_holds_two_seats(self):
        self.two_stations_shared_player()
        with self.assertRaises(EventError):
            self.app.federation.lock_final_roster(submitted_by="org")

    def test_dedup_keeps_priority_zone_and_advances_local_runner_up(self):
        east, west = self.two_stations_shared_player()
        changes = self.app.federation.resolve_duplicates(submitted_by="org")
        self.assertEqual(len(changes), 1)
        st_e = self.app.federation.promotion_status("east", "名手组")
        st_w = self.app.federation.promotion_status("west", "名手组")
        self.assertIn(east[0], st_e["occupied"])
        self.assertNotIn(east[0], st_w["occupied"])
        self.assertIn(west[1], st_w["occupied"])
        self.assertEqual(st_w["occupied"][west[1]]["mode"], "advanced")
        self.assertEqual(st_w["occupied"][west[1]]["replaces"], east[0])
        lock = self.app.federation.lock_final_roster(
            submitted_by="org", idempotency_key="lock")
        roster = self.app.federation.final_roster()
        holders = [r["person_id"] for r in roster["groups"]["名手组"]]
        self.assertEqual(len(holders), len(set(holders)))
        self.assertEqual(len(holders), 2)

    def test_recompute_and_notifications_scoped_to_affected_group(self):
        self.two_stations_shared_player()
        self.app.federation.resolve_duplicates(submitted_by="org")
        west_notes = self.app.federation.notifications(station_id="west")
        east_notes = self.app.federation.notifications(station_id="east")
        templates_w = {n["template"] for n in west_notes}
        self.assertIn("promotion_advanced", templates_w)
        self.assertIn("promotion_released", templates_w)
        # 东赛区组别不受影响：锁名单前不应收到任何递补类通知
        self.assertFalse(
            any(n["template"] in ("promotion_advanced", "promotion_released",
                                  "quota_vacancy")
                for n in east_notes))

    def test_withdrawal_after_projection_advances_next_and_logs_reason(self):
        ids = self.world.setup_station(
            "south", [("甲", "31010119900101111X"),
                      ("乙", "31010119900202222X"),
                      ("丙", "31010119900303333X")],
            decide=lambda g, b, w: "黑胜" if b == 0 else "白胜")
        self.app.federation.freeze_federation_rules(
            expected_stations=["south"], zone_priority=["south"],
            submitted_by="org", idempotency_key="fr2")
        self.app.federation.project_station(
            "south", submitted_by="org", idempotency_key="ps")
        status = self.app.federation.promotion_status("south", "名手组")
        top = status["ordered"][0]
        self.assertIn(top, status["occupied"])
        self.app.federation.revoke_qualification(
            "south", "名手组", top, reason="个人原因放弃总决赛",
            submitted_by="org", idempotency_key="rev1")
        status = self.app.federation.promotion_status("south", "名手组")
        self.assertNotIn(top, status["occupied"])
        self.assertEqual(len(status["occupied"]), 1)
        holder = next(iter(status["occupied"]))
        self.assertEqual(status["occupied"][holder]["replaces"], top)

    def test_vacancy_when_no_runner_up_blocks_lock(self):
        ids = self.world.setup_station(
            "north", [("独苗", "21010119900101111X")],
            decide=lambda g, b, w: "黑胜")
        self.app.federation.freeze_federation_rules(
            expected_stations=["north"], zone_priority=["north"],
            submitted_by="org", idempotency_key="fr3")
        self.app.federation.project_station(
            "north", submitted_by="org", idempotency_key="pn")
        self.app.federation.revoke_qualification(
            "north", "名手组", ids[0], reason="弃赛",
            submitted_by="org", idempotency_key="revn")
        status = self.app.federation.promotion_status("north", "名手组")
        self.assertTrue(status["vacancies"])
        with self.assertRaises(EventError):
            self.app.federation.lock_final_roster(submitted_by="org")

    def test_locked_roster_is_immutable_and_redelivery_changes_nothing(self):
        self.two_stations_shared_player()
        self.app.federation.resolve_duplicates(submitted_by="org")
        lock = self.app.federation.lock_final_roster(
            submitted_by="org", idempotency_key="lock")
        tail = self.app.store.tail_hash
        # 再次锁定（同键）直接返回原事件
        again = self.app.federation.lock_final_roster(
            submitted_by="org", idempotency_key="lock")
        self.assertEqual(again.seq, lock.seq)
        # 去重/撤销在锁后被拒绝
        with self.assertRaises(EventError):
            self.app.federation.resolve_duplicates(submitted_by="org")
        self.assertEqual(self.app.store.tail_hash, tail)

    def test_explain_player_games_rules_and_boundary(self):
        east, west = self.two_stations_shared_player()
        self.app.federation.resolve_duplicates(submitted_by="org")
        self.app.federation.lock_final_roster(
            submitted_by="org", idempotency_key="lock")
        ex = self.app.federation.explain(west[1])  # 王五：递补者
        self.assertEqual(len(ex["participations"]), 1)
        p = ex["participations"][0]
        self.assertEqual(p["rules_version"], "rules-2026-v1")
        self.assertTrue(p["games"], "解释必须列出经过的对局")
        self.assertEqual(p["seat"]["mode"], "advanced")
        self.assertIsNotNone(ex["roster_lock"])
        self.assertIn("递补", ex["qualification"])
        # 直接晋级者解释临界名次
        ex0 = self.app.federation.explain(east[0])
        self.assertIn("直接晋级", ex0["qualification"])

    def test_rebuild_from_journal_preserves_roster_fingerprint(self):
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            world = World(store_path=path, quota=1, rounds=1)
            ids = world.setup_station(
                "east", [("甲", "11010119900101111X"),
                         ("乙", "11010119900202222X")],
                decide=lambda g, b, w: "黑胜" if b == 0 else "白胜")
            world.app.federation.freeze_federation_rules(
                expected_stations=["east"], zone_priority=["east"],
                submitted_by="org", idempotency_key="fr")
            world.app.federation.project_station(
                "east", submitted_by="org", idempotency_key="pe")
            world.app.federation.lock_final_roster(
                submitted_by="org", idempotency_key="lock")
            fp = world.app.federation.final_roster()["fingerprint"]
            from app import rebuild
            app2 = rebuild(path)
            self.assertTrue(app2.store.verify_chain())
            self.assertEqual(
                app2.federation.final_roster()["fingerprint"], fp)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
