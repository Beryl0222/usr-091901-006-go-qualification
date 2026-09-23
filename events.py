"""仅追加事件日志：哈希链、幂等重投、JSONL 持久化与重放。

领域里的一切当前状态都是事件的折叠结果；迟到结果、申诉裁决、
录入纠错都不允许改写历史事件，只能追加新事件修正当前效力。
"""

import hashlib
import json
import threading
import time
from pathlib import Path

GENESIS = "0" * 64
_EVENT_SCHEMA_VERSION = 1


class EventError(ValueError):
    """事件被拒绝（违反写入前置条件）。"""


def canonical(payload):
    """事件载荷的规范 JSON 字节，保证哈希在任何机器上可复现。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(payload):
    return hashlib.sha256(canonical(payload)).hexdigest()


class Event:
    """一条已提交事件。序号单调递增，prev_hash 串成哈希链。"""

    __slots__ = ("seq", "ts", "type", "payload", "idempotency_key",
                 "submitted_by", "source", "prev_hash", "hash")

    def __init__(self, seq, ts, etype, payload, idempotency_key,
                 submitted_by, source, prev_hash, hash_):
        self.seq = seq
        self.ts = ts
        self.type = etype
        self.payload = payload
        self.idempotency_key = idempotency_key
        self.submitted_by = submitted_by
        self.source = source
        self.prev_hash = prev_hash
        self.hash = hash_

    def to_dict(self):
        return {"seq": self.seq, "ts": self.ts, "type": self.type,
                "payload": self.payload, "idempotency_key": self.idempotency_key,
                "submitted_by": self.submitted_by, "source": self.source,
                "prev_hash": self.prev_hash, "hash": self.hash}

    @classmethod
    def from_dict(cls, row):
        return cls(row["seq"], row["ts"], row["type"], row["payload"],
                   row.get("idempotency_key"), row.get("submitted_by"),
                   row.get("source"), row["prev_hash"], row["hash"])


class EventStore:
    """线程安全的仅追加日志；可选 JSONL 持久化。"""

    def __init__(self, path=None, clock=None):
        self._lock = threading.RLock()
        self._events = []
        self._by_idem = {}
        self._subscribers = []
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.path = Path(path) if path else None
        if self.path and self.path.exists():
            self._load()

    # ---- 读取 ----
    def events(self, etype=None):
        with self._lock:
            if etype is None:
                return list(self._events)
            return [e for e in self._events if e.type == etype]

    @property
    def tail_hash(self):
        with self._lock:
            return self._events[-1].hash if self._events else GENESIS

    def get_by_idempotency(self, key):
        """按幂等键取回首次事件；不存在返回 None。"""
        if key is None:
            return None
        with self._lock:
            return self._by_idem.get(key)

    def __len__(self):
        with self._lock:
            return len(self._events)

    # ---- 订阅：聚合在锁外重放，写入在锁内分发 ----
    def subscribe(self, handler):
        with self._lock:
            for event in self._events:
                handler(event)
            self._subscribers.append(handler)

    # ---- 写入 ----
    def append(self, etype, payload, *, idempotency_key=None,
               submitted_by=None, source=None):
        """提交一条事件。

        相同 idempotency_key 的重复投递返回首次事件本身，
        不产生新事实——这是“重新投递任何一轮成绩都不会改变
        已确认唯一事实”的落点。
        """
        if not isinstance(etype, str) or not etype:
            raise EventError("事件类型不能为空")
        if not isinstance(payload, dict):
            raise EventError("事件载荷必须是对象")
        with self._lock:
            if idempotency_key is not None:
                prior = self._by_idem.get(idempotency_key)
                if prior is not None:
                    if prior.type != etype or digest(prior.payload) != digest(payload):
                        raise EventError("幂等键对应的请求内容与首次提交不一致")
                    return prior
            seq = len(self._events) + 1
            ts = self._clock()
            prev = self._events[-1].hash if self._events else GENESIS
            body = {"v": _EVENT_SCHEMA_VERSION, "seq": seq, "ts": ts,
                    "type": etype, "payload": payload,
                    "idempotency_key": idempotency_key,
                    "submitted_by": submitted_by, "source": source,
                    "prev_hash": prev}
            hash_ = digest(body)
            event = Event(seq, ts, etype, payload, idempotency_key,
                          submitted_by, source, prev, hash_)
            self._events.append(event)
            if idempotency_key is not None:
                self._by_idem[idempotency_key] = event
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            for handler in list(self._subscribers):
                handler(event)
            return event

    # ---- 重放 ----
    def _load(self):
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = Event.from_dict(json.loads(line))
            if event.seq != len(self._events) + 1:
                raise EventError(f"事件序号断裂: {event.seq}")
            expected_prev = self._events[-1].hash if self._events else GENESIS
            if event.prev_hash != expected_prev:
                raise EventError(f"哈希链断裂于序号 {event.seq}")
            self._verify_hash(event)
            self._events.append(event)
            if event.idempotency_key is not None:
                if event.idempotency_key in self._by_idem:
                    raise EventError(f"幂等键重复: {event.idempotency_key}")
                self._by_idem[event.idempotency_key] = event

    @staticmethod
    def _verify_hash(event):
        body = {"v": _EVENT_SCHEMA_VERSION, "seq": event.seq, "ts": event.ts,
                "type": event.type, "payload": event.payload,
                "idempotency_key": event.idempotency_key,
                "submitted_by": event.submitted_by, "source": event.source,
                "prev_hash": event.prev_hash}
        if digest(body) != event.hash:
            raise EventError(f"事件内容与哈希不符: {event.seq}")

    def verify_chain(self):
        """全量校验哈希链与内容，供审计端点调用。"""
        with self._lock:
            prev = GENESIS
            seen = set()
            for event in self._events:
                if event.prev_hash != prev:
                    raise EventError(f"哈希链断裂于序号 {event.seq}")
                self._verify_hash(event)
                if event.idempotency_key is not None:
                    if event.idempotency_key in seen:
                        raise EventError("检测到重复幂等键")
                    seen.add(event.idempotency_key)
                prev = event.hash
            return True
