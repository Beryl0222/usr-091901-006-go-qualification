"""身份归并与赛区隐私测试。"""

import unittest

from events import EventError
from app import Application
from identity import (canonical_doc_number, mask_name, mask_number)


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.app = Application()

    def register(self, station, name, doc, dtype="身份证", **kw):
        return self.app.identity.register(
            station_id=station, group="名手组", name=name,
            documents=[{"type": dtype, "number": doc}],
            submitted_by="admin",
            idempotency_key=kw.pop("idem", f"k-{station}-{doc}"), **kw)

    def test_15_18_id_card_recognized_as_same_person(self):
        e1 = self.register("east", "张三", "11010119900101123X")
        e2 = self.register("west", "张三", "110101900101123")
        self.assertEqual(e1.payload["person_id"], e2.payload["person_id"])

    def test_canonical_keys_cover_both_id_forms(self):
        keys = canonical_doc_number("身份证", "11010119900101123X")
        self.assertIn(("身份证", "110101900101123"), keys)

    def test_duplicate_document_directly_merges_at_registration(self):
        e1 = self.register("east", "张三", "11010119800101123")
        e2 = self.register("west", "老张", "11010119800101123")
        self.assertEqual(e1.payload["person_id"], e2.payload["person_id"])
        person = self.app.identity.person(e1.payload["person_id"])
        self.assertIn("老张", person["aliases"])

    def test_homophone_names_are_candidates_not_auto_merged(self):
        self.register("east", "张伟", "11010119910101001X",
                      gender="男", birth_date="1991-01-01",
                      idem="a")
        self.register("west", "张威", "11010219910101002X",
                      gender="男", birth_date="1991-01-01",
                      idem="b")
        candidates = self.app.identity.homophone_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["name_key"], "zhangwei")
        # 未审核合并前是两个根身份
        roots = {self.app.identity.resolve(p)
                 for p in candidates[0]["persons"]}
        self.assertEqual(len(roots), 2)

    def test_reviewer_confirms_homophone_merge(self):
        e1 = self.register("east", "张伟", "11010119910101001X",
                           gender="男", idem="a")
        e2 = self.register("west", "张威", "11010219910101002X",
                           gender="男", idem="b")
        self.app.identity.confirm_merge(
            e1.payload["person_id"], e2.payload["person_id"],
            reason="本人现场确认", submitted_by="审核员",
            idempotency_key="m1")
        self.assertEqual(
            self.app.identity.resolve(e2.payload["person_id"]),
            self.app.identity.resolve(e1.payload["person_id"]))

    def test_merge_without_evidence_requires_reason(self):
        # 两个完全不相干的人
        e1 = self.register("east", "王强", "11010119910101001X", idem="a")
        e2 = self.register("west", "李娜", "11010219920202002X", idem="b")
        with self.assertRaises(EventError):
            self.app.identity.confirm_merge(
                e1.payload["person_id"], e2.payload["person_id"],
                reason="", submitted_by="审核员")

    def test_same_station_same_group_duplicate_registration_blocked(self):
        self.register("east", "张三", "11010119900101123", idem="first")
        with self.assertRaises(EventError):
            self.register("east", "张三", "11010119900101123",
                          idem="second")

    def test_station_view_is_masked_and_passcodes_differ(self):
        e1 = self.register("east", "张三", "11010119900101123X", idem="a")
        self.register("west", "张三", "110101900101123", idem="b")
        pid = e1.payload["person_id"]
        view = self.app.identity.station_view("east")
        self.assertEqual(len(view), 1)
        self.assertNotIn("123", view[0]["documents_masked"][0]["number"][1:-1])
        self.assertTrue(view[0]["name_masked"].startswith("张"))
        self.assertIn("*", view[0]["name_masked"])
        pe = self.app.identity.passcode(pid, "east")
        pw = self.app.identity.passcode(pid, "west")
        self.assertNotEqual(pe, pw)
        # 赛区视角拿不到任何完整证件号
        import json
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("11010119900101123X", blob)

    def test_mask_helpers(self):
        self.assertEqual(mask_name("张三"), "张*")
        self.assertEqual(mask_number("123456"), "1****6")


if __name__ == "__main__":
    unittest.main()
