"""事件存储：哈希链、版本号、幂等、密封与审计。"""

import sqlite3
import unittest

from store import EventStore, ConflictError, GENESIS


class EventStoreTest(unittest.TestCase):
    def setUp(self):
        self.s = EventStore(":memory:")

    def test_append_and_version(self):
        e1 = self.s.append("g:east/名手组", "rules_frozen", {"v": 1}, actor="r", expected_version=-1)
        self.assertEqual(e1["version"], 0)
        self.assertEqual(e1["prev_hash"], GENESIS)
        self.assertTrue(e1["entry_hash"])
        e2 = self.s.append("g:east/名手组", "rounds_frozen", {"v": 2}, actor="r", expected_version=0)
        self.assertEqual(e2["version"], 1)
        self.assertEqual(e2["prev_hash"], e1["entry_hash"])
        self.assertEqual(len(self.s.read_stream("g:east/名手组")), 2)

    def test_expected_version_conflict(self):
        self.s.append("s", "t", {"a": 1}, expected_version=-1)
        with self.assertRaises(ConflictError):
            self.s.append("s", "t", {"a": 2}, expected_version=-1)

    def test_streams_are_independent_but_share_global_chain(self):
        a = self.s.append("a", "t", {}, expected_version=-1)
        b = self.s.append("b", "t", {}, expected_version=-1)
        self.assertEqual(a["version"], 0)
        self.assertEqual(b["version"], 0)  # 流版本各自独立
        self.assertEqual(b["prev_hash"], a["entry_hash"])  # 全局链跨流串联

    def test_idempotent_request_key(self):
        e1 = self.s.append("s", "t", {"x": 1}, request_key="k-1", expected_version=-1)
        e2 = self.s.append("s", "t", {"x": 2}, request_key="k-1", expected_version=0)
        self.assertEqual(e1["event_id"], e2["event_id"])
        self.assertEqual(e2["payload"], {"x": 1})
        self.assertEqual(len(self.s.read_stream("s")), 1)
        self.assertIsNotNone(self.s.lookup_idempotency("k-1"))
        self.assertIsNone(self.s.lookup_idempotency("missing"))

    def test_event_id_replay(self):
        e1 = self.s.append("s", "t", {"x": 1}, event_id="fixed-id", expected_version=-1)
        e2 = self.s.append("s", "t", {"x": 9}, event_id="fixed-id", expected_version=0)
        self.assertEqual(e2["payload"], {"x": 1})
        self.assertEqual(e1["event_id"], e2["event_id"])

    def test_verify_chain_ok_and_detects_tamper(self):
        self.s.append("a", "t", {"v": 1}, expected_version=-1)
        self.s.append("b", "t", {"v": 2}, expected_version=-1)
        self.s.append("a", "t", {"v": 3}, expected_version=0)
        self.assertTrue(self.s.verify_chain()["ok"])
        with self.s._conn:
            self.s._conn.execute("UPDATE events SET payload=? WHERE etype='t' LIMIT 1",
                                 ('{"v": 999}',))
        result = self.s.verify_chain()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "哈希不匹配")

    def test_seal_roundtrip_and_audit(self):
        self.s.append("reg", "player_registered", {"pid": "p1"}, expected_version=-1)
        seal_id = self.s.seal_identity("p1:east:名手组",
                                       {"name": "王伟", "cert_no": "110101199001011234"},
                                       created_seq=1)
        # 静态存储不含明文
        row = self.s._conn.execute("SELECT ciphertext FROM seals").fetchone()
        self.assertNotIn(b"110101199001011234", row["ciphertext"])
        plain = self.s.unseal_identity(seal_id, actor="auditor:wang", reason="资格复核")
        self.assertEqual(plain["cert_no"], "110101199001011234")
        audit = self.s.seal_audit_log()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["reason"], "资格复核")
        # 每次拆封都留痕
        self.s.unseal_identity(seal_id, actor="auditor:li", reason="复核2")
        self.assertEqual(len(self.s.seal_audit_log()), 2)


if __name__ == "__main__":
    unittest.main()
