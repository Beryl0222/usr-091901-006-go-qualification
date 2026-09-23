"""事件日志：哈希链、幂等重投、持久化重放。"""

import json
import tempfile
import unittest
from pathlib import Path

from app import Application, rebuild
from events import EventStore, EventError
from test_helpers import SHA


class EventStoreTest(unittest.TestCase):
    def test_hash_chain_links_events(self):
        store = EventStore()
        e1 = store.append("Tested", {"a": 1})
        e2 = store.append("Tested", {"a": 2})
        self.assertEqual(e1.prev_hash, "0" * 64)
        self.assertEqual(e2.prev_hash, e1.hash)
        self.assertTrue(store.verify_chain())

    def test_redelivery_returns_first_event_unchanged(self):
        store = EventStore()
        first = store.append("GameResultSubmitted", {"game": "g1", "v": 1},
                             idempotency_key="k1")
        second = store.append("GameResultSubmitted", {"game": "g1", "v": 1},
                              idempotency_key="k1")
        self.assertIs(first, second)
        self.assertEqual(len(store), 1)

    def test_same_key_different_payload_rejected(self):
        store = EventStore()
        store.append("GameResultSubmitted", {"game": "g1"},
                     idempotency_key="k1")
        with self.assertRaises(EventError):
            store.append("GameResultSubmitted", {"game": "g2"},
                         idempotency_key="k1")

    def test_persistence_replay_rebuilds_state(self):
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            app = Application(store_path=path)
            app.stations.freeze({
                "station_id": "east", "rules_version": "r1",
                "groups": [{"name": "名手组", "quota": 1}],
                "rounds": 1, "tiebreak": ["抽签"]},
                submitted_by="admin", idempotency_key="fz")
            app2 = rebuild(path)
            self.assertIn("east", app2.stations.stations)
            self.assertEqual(app.store.tail_hash, app2.store.tail_hash)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_tampered_file_detected(self):
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            store = EventStore(path)
            store.append("Tested", {"a": 1})
            rows = [json.loads(x) for x in Path(path).read_text(
                encoding="utf-8").splitlines()]
            rows[0]["payload"]["a"] = 999
            Path(path).write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                encoding="utf-8")
            with self.assertRaises(EventError):
                EventStore(path)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
