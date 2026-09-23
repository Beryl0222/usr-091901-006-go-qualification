"""仅追加事件存储。

所有业务事实都以不可变事件落库：
- 全局哈希链（prev_hash + 载荷 → entry_hash），任何篡改或删除都会断链；
- 每流（分区聚合）单调版本号，配合重放实现乐观并发；
- 客户端 event_id 幂等：重复投递返回同一事件，绝不产生第二条事实；
- 身份类 PII 以独立密封行保存，普通事件读取不返回明文；
- 拆封必须留下审计记录。

仅依赖标准库，SQLite 以单连接加锁供线程 HTTP 服务使用。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Optional

GENESIS = "0" * 64
STORE_VERSION = 1


class ConflictError(Exception):
    """流版本与预期不符（并发修改）。"""


class StreamDeletedError(Exception):
    """读取的流不存在（被快照清理或从未创建）。"""


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_entry(prev_hash: str, payload: Any) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + b"\n" + _canonical(payload)).hexdigest()


class EventStore:
    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        self._init_schema()

    # ---------- schema ----------

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    stream TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    etype TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    actor TEXT,
                    occurred_at TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    UNIQUE(stream, version)
                );
                CREATE INDEX IF NOT EXISTS events_stream ON events(stream, version);
                CREATE TABLE IF NOT EXISTS seals(
                    seal_id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    ciphertext BLOB NOT NULL,
                    nonce BLOB NOT NULL,
                    created_seq INTEGER NOT NULL,
                    unsealed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS seals_subject ON seals(subject);
                CREATE TABLE IF NOT EXISTS seal_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seal_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency(
                    request_key TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                """
            )
            row = self._conn.execute("SELECT value FROM meta WHERE key='store_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('store_version', ?)", (str(STORE_VERSION),)
                )
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('created_at', ?)", (_now_iso(),)
                )

    # ---------- append ----------

    def append(
        self,
        stream: str,
        etype: str,
        payload: dict,
        *,
        actor: Optional[str] = None,
        event_id: Optional[str] = None,
        expected_version: Optional[int] = None,
        request_key: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> dict:
        """追加一个事件。

        expected_version 为该流当前最后版本号；新建流传 -1。
        request_key 命中时直接返回首次写入的事件（幂等重放）。
        """
        event_id = event_id or str(uuid.uuid4())
        occurred_at = occurred_at or _now_iso()
        with self._lock:
            if request_key is not None:
                hit = self._conn.execute(
                    "SELECT event_id FROM idempotency WHERE request_key=?", (request_key,)
                ).fetchone()
                if hit is not None:
                    row = self._conn.execute(
                        "SELECT * FROM events WHERE event_id=?", (hit["event_id"],)
                    ).fetchone()
                    return _row_to_event(row)

            last_version_row = self._conn.execute(
                "SELECT MAX(version) AS v FROM events WHERE stream=?", (stream,)
            ).fetchone()
            current_version = -1 if last_version_row["v"] is None else last_version_row["v"]
            if expected_version is not None and expected_version != current_version:
                raise ConflictError(f"stream={stream} expected={expected_version} actual={current_version}")
            next_version = current_version + 1
            # 全局哈希链：前驱是全局最后一个事件，任何删除/重排都会断链
            global_last = self._conn.execute(
                "SELECT entry_hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = GENESIS if global_last is None else global_last["entry_hash"]

            record_payload = dict(payload)
            entry_hash = digest_entry(prev_hash, {
                "event_id": event_id,
                "stream": stream,
                "version": next_version,
                "type": etype,
                "payload": record_payload,
                "actor": actor,
                "occurred_at": occurred_at,
            })
            try:
                with self._conn:
                    cur = self._conn.execute(
                        """INSERT INTO events(event_id, stream, version, etype, payload, actor,
                                              occurred_at, prev_hash, entry_hash)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            event_id, stream, next_version, etype,
                            json.dumps(record_payload, ensure_ascii=False), actor,
                            occurred_at, prev_hash, entry_hash,
                        ),
                    )
                    inserted_seq = cur.lastrowid
                    if request_key is not None:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO idempotency(request_key, event_id, at) VALUES(?,?,?)",
                            (request_key, event_id, occurred_at),
                        )
            except sqlite3.IntegrityError as exc:  # 同 event_id 重放或版本竞争
                row = self._conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
                if row is not None:
                    return _row_to_event(row)
                raise ConflictError(str(exc)) from exc
            row = self._conn.execute("SELECT * FROM events WHERE seq=?", (inserted_seq,)).fetchone()
            return _row_to_event(row)

    # ---------- read ----------

    def read_stream(self, stream: str, *, from_version: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE stream=? AND version>=? ORDER BY version",
                (stream, from_version),
            ).fetchall()
            return [_row_to_event(r) for r in rows]

    def read_all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
            return [_row_to_event(r) for r in rows]

    def stream_version(self, stream: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM events WHERE stream=?", (stream,)
            ).fetchone()
            return -1 if row["v"] is None else row["v"]

    def has_stream(self, stream: str) -> bool:
        return self.stream_version(stream) >= 0

    def lookup_idempotency(self, request_key: str) -> Optional[dict]:
        """命中幂等键时返回首次写入的完整事件，否则 None。"""
        with self._lock:
            hit = self._conn.execute(
                "SELECT event_id FROM idempotency WHERE request_key=?", (request_key,)
            ).fetchone()
            if hit is None:
                return None
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id=?", (hit["event_id"],)
            ).fetchone()
            return _row_to_event(row) if row else None

    def fold(self, streams: Iterable[str], initial: Any, reducer: Callable[[Any, dict], Any]) -> Any:
        state = initial
        for stream in streams:
            for event in self.read_stream(stream):
                state = reducer(state, event)
        return state

    # ---------- verification ----------

    def verify_chain(self) -> dict:
        """重放全链，返回校验结果。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY seq"
            ).fetchall()
        prev = GENESIS
        seen_streams: dict[str, int] = {}
        for row in rows:
            if row["prev_hash"] != prev:
                return {"ok": False, "reason": "链断裂", "seq": row["seq"]}
            expected = digest_entry(row["prev_hash"], {
                "event_id": row["event_id"],
                "stream": row["stream"],
                "version": row["version"],
                "type": row["etype"],
                "payload": json.loads(row["payload"]),
                "actor": row["actor"],
                "occurred_at": row["occurred_at"],
            })
            if not hmac.compare_digest(expected, row["entry_hash"]):
                return {"ok": False, "reason": "哈希不匹配", "seq": row["seq"]}
            expected_version = seen_streams.get(row["stream"], -1) + 1
            if row["version"] != expected_version:
                return {"ok": False, "reason": "流版本不连续", "seq": row["seq"]}
            seen_streams[row["stream"]] = row["version"]
            prev = row["entry_hash"]
        return {"ok": True, "events": len(rows), "streams": len(seen_streams), "tail": prev}

    # ---------- PII seals ----------

    def seal_identity(self, subject: str, plaintext: dict, *, created_seq: int) -> str:
        """以数据库密钥密封一份身份明文。密钥来自 meta（首次自动生成）。"""
        key = self._master_key()
        plain = json.dumps(plaintext, ensure_ascii=False).encode("utf-8")
        seal_id = str(uuid.uuid4())
        nonce = uuid.uuid4().bytes
        ciphertext = _xor_keystream(key, nonce, plain)  # 见 _xor_keystream 说明
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO seals(seal_id, subject, ciphertext, nonce, created_seq) VALUES(?,?,?,?,?)",
                (seal_id, subject, ciphertext, nonce, created_seq),
            )
        return seal_id

    def unseal_identity(self, seal_id: str, *, actor: str, reason: str) -> Optional[dict]:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM seals WHERE seal_id=?", (seal_id,)).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "INSERT INTO seal_audit(seal_id, actor, reason, at) VALUES(?,?,?,?)",
                (seal_id, actor, reason, _now_iso()),
            )
            self._conn.execute("UPDATE seals SET unsealed=1 WHERE seal_id=?", (seal_id,))
            key = self._master_key()
            plain = _xor_keystream(key, row["nonce"], row["ciphertext"])
            return json.loads(plain.decode("utf-8"))

    def list_seals(self, subject: Optional[str] = None) -> list[dict]:
        with self._lock:
            if subject is None:
                rows = self._conn.execute(
                    "SELECT seal_id, subject, created_seq, unsealed FROM seals ORDER BY created_seq"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT seal_id, subject, created_seq, unsealed FROM seals WHERE subject=? ORDER BY created_seq",
                    (subject,),
                ).fetchall()
            return [dict(r) for r in rows]

    def seal_audit_log(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM seal_audit ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def _master_key(self) -> bytes:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key='master_key'").fetchone()
            if row is not None:
                return bytes.fromhex(row["value"])
            key = uuid.uuid4().bytes + uuid.uuid4().bytes
            with self._conn:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('master_key', ?)", (key.hex(),)
                )
            return key

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------- helpers ----------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _row_to_event(row: sqlite3.Row) -> dict:
    return {
        "event_id": row["event_id"],
        "stream": row["stream"],
        "version": row["version"],
        "type": row["etype"],
        "payload": json.loads(row["payload"]),
        "actor": row["actor"],
        "occurred_at": row["occurred_at"],
        "prev_hash": row["prev_hash"],
        "entry_hash": row["entry_hash"],
    }


def _xor_keystream(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """CTR 风格流加密：以 SHA256(key ‖ nonce ‖ counter) 生成密钥流。

    演示用途的对称密封，保证静态明文不落库；部署时可替换为 AES-GCM，
    存储接口保持不变。
    """
    out = bytearray(len(data))
    counter = 0
    offset = 0
    while offset < len(data):
        block = hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        for i, b in enumerate(block):
            if offset >= len(data):
                break
            out[offset] = data[offset] ^ b
            offset += 1
        counter += 1
    return bytes(out)
