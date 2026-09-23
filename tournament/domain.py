"""赛事资格与核算领域内核。

设计原则
--------
1. 命令只追加事件，从不修改/删除历史；迟到、申诉、纠错都是引用旧事件的新事件。
2. 规则、轮次、配对、破同分次序在开赛前冻结，之后所有计算与解释都引用冻结版本。
3. 身份通过证件 HMAC 相关标识归并；完整证件明文只进密封表。
4. 一个 person 全局最多占用一个总决赛席位；重复占位在受影响组内顺延递补。
5. 退赛/更正只重算受影响组；总决赛锁定后任何命令都不能再改变事实。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import ident
from .pairing import suggest_pairings
from .standing import (
    DEFAULT_TIEBREAKS, GameRecord, compute_tables, rank_players,
)
from store import EventStore


class DomainError(Exception):
    """业务规则冲突。code 供 API 映射状态码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# 结果类型 → （白方分, 黑方分）
def _points(winner: str) -> tuple[float, float]:
    if winner == "white":
        return 1.0, 0.0
    if winner == "black":
        return 0.0, 1.0
    if winner == "draw":
        return 0.5, 0.5
    if winner == "none":  # 双弃权
        return 0.0, 0.0
    raise DomainError("bad_result", f"未知胜负取值: {winner}")


def _canonical_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ===================================================================
# 读模型
# ===================================================================

@dataclass
class Registration:
    person_id: str
    name: str
    phonetic: Optional[str]
    cert_type: str
    masked_cert: str
    seal_id: str
    station_id: str
    group_id: str
    birth_year: Optional[int] = None
    withdrawn_from: Optional[int] = None
    withdraw_reason: Optional[str] = None


@dataclass
class Outcome:
    """一个棋盘当前生效的结果及其来源事件。"""
    round_no: int
    board: str
    white: str
    black: Optional[str]  # None = 轮空
    result: str
    points_white: float
    points_black: float
    source_event: str
    record_hash: Optional[str] = None
    signature_source: Optional[str] = None
    late: bool = False
    correction_of: Optional[str] = None
    appeal_ref: Optional[str] = None
    superseded_by: Optional[str] = None
    note: Optional[str] = None

    def effective(self) -> bool:
        return self.superseded_by is None


@dataclass
class GroupState:
    station_id: str
    group_id: str
    name: str = ""
    registrations: dict[str, Registration] = field(default_factory=dict)  # person_id
    rules: Optional[dict] = None
    rule_version: Optional[str] = None
    rules_event: Optional[str] = None
    rounds: Optional[dict] = None
    pairings: dict[int, list[dict]] = field(default_factory=dict)
    outcomes: dict[tuple[int, str], list[Outcome]] = field(default_factory=dict)
    confirmed: bool = False
    confirm_event: Optional[str] = None
    qualified: list[dict] = field(default_factory=list)
    excluded_duplicates: list[dict] = field(default_factory=list)
    finals_locked: bool = False


@dataclass
class ReadModel:
    tournament: dict = field(default_factory=dict)
    stations: dict[str, dict] = field(default_factory=dict)
    groups: dict[tuple[str, str], GroupState] = field(default_factory=dict)
    code_to_person: dict[str, str] = field(default_factory=dict)
    persons: dict[str, dict] = field(default_factory=dict)  # pid → {codes:[], merged_from:[]}
    similar_flags: list[dict] = field(default_factory=list)
    # person 的全部报名（跨站）
    footprint: dict[str, list[str]] = field(default_factory=dict)
    # 已确认席位 person → {station, group, rank}
    seats: dict[str, dict] = field(default_factory=dict)
    finals: Optional[dict] = None
    notifications: list[dict] = field(default_factory=list)


class TournamentService:
    STATION_GROUPS = ("名手组", "名师组", "名人组")

    def __init__(self, store: EventStore, identity_secret: Optional[bytes] = None):
        self.store = store
        self._lock = threading.RLock()
        if identity_secret is None:
            identity_secret = self._load_or_create_secret()
        self._secret = identity_secret

    def _load_or_create_secret(self) -> bytes:
        with self.store._lock, self.store._conn:
            row = self.store._conn.execute("SELECT value FROM meta WHERE key='identity_secret'").fetchone()
            if row is not None:
                return bytes.fromhex(row["value"])
            secret = uuid.uuid4().bytes + uuid.uuid4().bytes
            self.store._conn.execute(
                "INSERT INTO meta(key, value) VALUES('identity_secret', ?)", (secret.hex(),)
            )
            return secret

    # ---------------------------------------------------------------
    # 投影
    # ---------------------------------------------------------------

    def _stream(self, *parts) -> str:
        return ":".join(parts)

    def _load(self) -> ReadModel:
        """重放全部事件得到一份当前读模型。

        持有领域锁，保证查询路径看到的是某条已提交命令的完整结果；
        RLock 可重入，命令方法持锁后多次调用安全。返回对象为该次调用私有快照。
        """
        with self._lock:
            m = ReadModel()
            for ev in self.store.read_all():
                self._apply(m, ev)
            return m

    def _apply(self, m: ReadModel, ev: dict) -> None:
        t, p = ev["type"], ev["payload"]
        if t == "tournament_created":
            m.tournament = p
        elif t == "station_opened":
            m.stations[p["station_id"]] = p
            for g in p["groups"]:
                gs = GroupState(station_id=p["station_id"], group_id=g["group_id"], name=g.get("name", g["group_id"]))
                m.groups[(p["station_id"], g["group_id"])] = gs
        elif t == "player_registered":
            gs = m.groups[(p["station_id"], p["group_id"])]
            reg = Registration(
                person_id=p["person_id"], name=p["name"], phonetic=p.get("phonetic"),
                cert_type=p["cert_type"], masked_cert=p["masked_cert"], seal_id=p["seal_id"],
                station_id=p["station_id"], group_id=p["group_id"], birth_year=p.get("birth_year"),
            )
            gs.registrations[p["person_id"]] = reg
            m.footprint.setdefault(p["person_id"], []).append(
                f"{p['station_id']}/{p['group_id']}"
            )
        elif t == "identity_linked":
            m.code_to_person[p["match_code"]] = p["person_id"]
            m.persons.setdefault(p["person_id"], {"codes": [], "merged_from": []})
            m.persons[p["person_id"]]["codes"].append(p["match_code"])
        elif t == "persons_merged":
            src, dst = p["from_person"], p["into_person"]
            entry = m.persons.setdefault(dst, {"codes": [], "merged_from": []})
            if src in m.persons:
                entry["merged_from"].append(src)
                for c in m.persons[src]["codes"]:
                    m.code_to_person[c] = dst
                entry["codes"].extend(m.persons[src]["codes"])
                m.persons.pop(src)
            for code, pid in list(m.code_to_person.items()):
                if pid == src:
                    m.code_to_person[code] = dst
            m.footprint[dst] = m.footprint.get(dst, []) + m.footprint.pop(src, [])
            if src in m.seats:
                m.seats[dst] = m.seats.pop(src)
            self._rename_person(m, src, dst)
        elif t == "similar_name_flagged":
            m.similar_flags.append(p)
        elif t == "similar_name_resolved":
            for f in m.similar_flags:
                if f["flag_id"] == p["flag_id"]:
                    f["status"] = p["decision"]
        elif t == "rules_frozen":
            gs = m.groups[(p["station_id"], p["group_id"])]
            gs.rules, gs.rule_version, gs.rules_event = p["rules"], p["rule_version"], ev["event_id"]
        elif t == "rounds_frozen":
            gs = m.groups[(p["station_id"], p["group_id"])]
            gs.rounds = p
        elif t == "pairings_frozen":
            gs = m.groups[(p["station_id"], p["group_id"])]
            gs.pairings[p["round_no"]] = p["boards"]
        elif t in ("result_submitted", "forfeit_recorded", "late_result_submitted"):
            oc = Outcome(
                round_no=p["round_no"], board=p["board"], white=p["white"], black=p.get("black"),
                result=p["result"], points_white=p["points_white"], points_black=p["points_black"],
                source_event=ev["event_id"], record_hash=p.get("record_hash"),
                signature_source=p.get("signature_source"), late=(t == "late_result_submitted"),
                note=p.get("note"),
            )
            gs = m.groups[(p["station_id"], p["group_id"])]
            gs.outcomes.setdefault((p["round_no"], p["board"]), []).append(oc)
        elif t in ("result_corrected", "appeal_amended"):
            # 旧结果失效
            gs = m.groups[(p["station_id"], p["group_id"])]
            for oc in gs.outcomes.get((p["round_no"], p["board"]), []):
                if oc.source_event == p["old_event_id"]:
                    oc.superseded_by = ev["event_id"]
            oc = Outcome(
                round_no=p["round_no"], board=p["board"], white=p["white"], black=p.get("black"),
                result=p["result"], points_white=p["points_white"], points_black=p["points_black"],
                source_event=ev["event_id"], record_hash=p.get("record_hash"),
                signature_source=p.get("signature_source"),
                correction_of=p.get("old_event_id") if t == "result_corrected" else None,
                appeal_ref=p.get("appeal_id") if t == "appeal_amended" else None,
                note=p.get("reason"),
            )
            gs.outcomes.setdefault((p["round_no"], p["board"]), []).append(oc)
        elif t == "appeal_rejected":
            pass  # 原结果保持有效，裁决过程留在事件里
        elif t == "player_withdrawn":
            gs = m.groups[(p["station_id"], p["group_id"])]
            reg = gs.registrations.get(p["person_id"])
            if reg:
                reg.withdrawn_from = p["from_round"]
                reg.withdraw_reason = p.get("reason")
        elif t == "group_confirmed":
            gs = m.groups[(p["station_id"], p["group_id"])]
            gs.confirmed = True
            gs.confirm_event = ev["event_id"]
            gs.qualified = p["qualified"]
            gs.excluded_duplicates = p.get("excluded_duplicates", [])
            for q in p["qualified"]:
                m.seats[q["person_id"]] = {
                    "station_id": p["station_id"], "group_id": p["group_id"],
                    "rank": q["rank_no"], "rule_version": q["rule_version"],
                }
            for ex in p.get("excluded_duplicates", []):
                # 若该人此前在本组占位（不会发生，占位只在 confirm 时），不进 seats
                pass
        elif t == "quota_adjusted":
            gs = m.groups[(p["station_id"], p["group_id"])]
            old = {q["person_id"] for q in gs.qualified}
            gs.qualified = p["qualified"]
            gs.excluded_duplicates = p.get("excluded_duplicates", [])
            for pid in old - {q["person_id"] for q in gs.qualified}:
                m.seats.pop(pid, None)
            for q in gs.qualified:
                m.seats.setdefault(q["person_id"], {
                    "station_id": p["station_id"], "group_id": p["group_id"],
                    "rank": q["rank_no"], "rule_version": q["rule_version"],
                })
        elif t == "finals_locked":
            m.finals = p
            for key in m.groups:
                m.groups[key].finals_locked = True
        elif t == "notification_emitted":
            m.notifications.append(p)

    @staticmethod
    def _rename_person(m: ReadModel, src: str, dst: str) -> None:
        """合并后把各读模型中的报名与名单引用迁到新 person。"""
        for gs in m.groups.values():
            if src in gs.registrations:
                reg = gs.registrations.pop(src)
                reg.person_id = dst
                gs.registrations[dst] = reg
            for bucket in (gs.qualified, gs.excluded_duplicates):
                for q in bucket:
                    if q.get("person_id") == src:
                        q["person_id"] = dst
        for f in m.similar_flags:
            if f["person_a"] == src:
                f["person_a"] = dst
            if f["person_b"] == src:
                f["person_b"] = dst

    # ---------------------------------------------------------------
    # 命令：赛事与赛区
    # ---------------------------------------------------------------

    def create_tournament(self, name: str, *, actor: str, event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            if m.tournament:
                raise DomainError("already_exists", "赛事已创建")
            tid = "T1"
            return self.store.append(
                self._stream("tournament", tid), "tournament_created",
                {"tournament_id": tid, "name": name},
                actor=actor, event_id=event_id, expected_version=-1,
            )

    def open_station(self, station_id: str, name: str, *, actor: str,
                     event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            self._require_tournament(m)
            if station_id in m.stations:
                raise DomainError("already_exists", f"赛区 {station_id} 已开站")
            groups = [{"group_id": g, "name": g} for g in self.STATION_GROUPS]
            payload = {"station_id": station_id, "name": name, "groups": groups}
            return self.store.append(
                self._stream("station", station_id), "station_opened", payload,
                actor=actor, event_id=event_id,
                expected_version=self.store.stream_version(self._stream("station", station_id)),
            )

    # ---------------------------------------------------------------
    # 命令：报名与身份
    # ---------------------------------------------------------------

    def register(self, station_id: str, group_id: str, *, name: str, cert_type: str,
                 cert_no: str, phonetic: str | None = None, birth_year: int | None = None,
                 actor: str, event_id: str | None = None, request_key: str | None = None) -> dict:
        with self._lock:
            if request_key is not None:
                replay = self.store.lookup_idempotency(request_key)
                if replay is not None:
                    return replay
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            if gs.rules is not None:
                raise DomainError("rules_frozen", "规则冻结后不再接受报名")
            code = ident.match_code(self._secret, cert_type, cert_no)
            pid = m.code_to_person.get(code)
            new_person = pid is None
            if new_person:
                pid = "p" + uuid.uuid4().hex[:10]

            # 同赛区一人只能进一个组
            for (sid, gid), other in m.groups.items():
                if sid == station_id and pid in other.registrations:
                    raise DomainError(
                        "duplicate_registration",
                        f"该棋手已在本赛区 {gid} 报名，一人在同一赛区只能报一个组别",
                    )
            if pid in gs.registrations:
                raise DomainError("duplicate_registration", "该棋手已在本组报名")

            # 密封完整身份（明文不落普通事件）
            reg_event_id = event_id or str(uuid.uuid4())
            seal_id = self.store.seal_identity(
                f"{pid}:{station_id}:{group_id}",
                {"name": name, "cert_type": cert_type, "cert_no": cert_no,
                 "phonetic": phonetic, "birth_year": birth_year},
                created_seq=len(self.store.read_all()),
            )
            payload = {
                "station_id": station_id, "group_id": group_id, "person_id": pid,
                "name": name, "phonetic": ident.phonetic_key(name, phonetic) if phonetic else None,
                "cert_type": cert_type, "masked_cert": ident.mask_cert(cert_type, cert_no),
                "seal_id": seal_id, "birth_year": birth_year,
            }
            ev = self.store.append(
                self._stream("registration", station_id, group_id), "player_registered", payload,
                actor=actor, event_id=reg_event_id, request_key=request_key,
            )
            if new_person:
                self.store.append(
                    self._stream("identity-index"), "identity_linked",
                    {"match_code": code, "person_id": pid, "cert_type": cert_type},
                    actor=actor,
                )
            self._flag_similar_names(m, station_id, group_id, pid, name, phonetic, actor)
            return ev

    def _flag_similar_names(self, m: ReadModel, station_id, group_id, pid, name, phonetic, actor):
        """同音且无共同证件 → 人工复核；同音绝不自动合并。每对人仅挂一张未决单。"""
        if not phonetic:
            return
        pkey = ident.phonetic_key(name, phonetic)
        open_pairs = {frozenset((f["person_a"], f["person_b"]))
                      for f in m.similar_flags if f["status"] == "open"}
        # 已裁决为 distinct 的人员对不再重复挂单（尊重既往人工结论）
        decided_distinct = {frozenset((f["person_a"], f["person_b"]))
                            for f in m.similar_flags if f["status"] == "distinct"}
        for (sid, gid), gs in m.groups.items():
            for other_pid, reg in gs.registrations.items():
                if other_pid == pid:
                    continue
                pair = frozenset((pid, other_pid))
                if (reg.phonetic == pkey and not self._share_code(m, pid, other_pid)
                        and pair not in open_pairs and pair not in decided_distinct):
                    flag_id = "f" + uuid.uuid4().hex[:10]
                    self.store.append(
                        self._stream("identity-review"), "similar_name_flagged",
                        {"flag_id": flag_id, "person_a": pid, "person_b": other_pid,
                         "name_a": name, "name_b": reg.name,
                         "stations": [f"{station_id}/{group_id}", f"{sid}/{gid}"],
                         "reason": "姓名同音、证件无关联", "status": "open"},
                        actor=actor,
                    )
                    open_pairs.add(pair)

    def _share_code(self, m: ReadModel, a: str, b: str) -> bool:
        if a == b:
            return True
        codes_a = set(m.persons.get(a, {}).get("codes", []))
        codes_b = set(m.persons.get(b, {}).get("codes", []))
        return bool(codes_a & codes_b)

    def resolve_similar(self, flag_id: str, decision: str, *, actor: str,
                        event_id: str | None = None) -> dict:
        """资格审核员对同音提示裁决：distinct（确属两人）或 merged（先合并再核销）。"""
        with self._lock:
            m = self._load()
            flag = next((f for f in m.similar_flags if f["flag_id"] == flag_id), None)
            if flag is None:
                raise DomainError("not_found", "复核单不存在")
            if flag["status"] != "open":
                raise DomainError("already_decided", f"该复核单已裁为 {flag['status']}")
            if decision not in ("distinct", "merged"):
                raise DomainError("bad_decision", "decision 必须是 distinct 或 merged")
            if decision == "merged":
                ca = self._canonical_person(m, flag["person_a"])
                cb = self._canonical_person(m, flag["person_b"])
                if ca != cb:
                    raise DomainError(
                        "need_merge",
                        "裁决为同人前必须先调用 /identity/merge 完成身份合并",
                    )
            return self.store.append(
                self._stream("identity-review"), "similar_name_resolved",
                {"flag_id": flag_id, "decision": "distinct" if decision == "distinct" else "merged",
                 "person_a": flag["person_a"], "person_b": flag["person_b"]},
                actor=actor, event_id=event_id,
            )

    @staticmethod
    def _canonical_person(m: ReadModel, pid: str) -> str:
        """返回合并后存续的 person_id；未被合并则原样返回。"""
        if pid in m.persons:
            return pid
        for dst, entry in m.persons.items():
            if pid in entry["merged_from"]:
                return dst
        return pid

    def merge_persons(self, from_person: str, into_person: str, reason: str, *, actor: str,
                      event_id: str | None = None) -> dict:
        """显式合并两种证件表示指向的同一棋手（须在涉及组别冻结规则前完成）。"""
        with self._lock:
            m = self._load()
            if from_person == into_person:
                raise DomainError("bad_merge", "不能合并同一个人")
            if from_person not in m.persons or into_person not in m.persons:
                raise DomainError("not_found", "棋手不存在")
            involved_groups = [gs for gs in m.groups.values()
                               if from_person in gs.registrations or into_person in gs.registrations]
            for gs in involved_groups:
                if gs.rules is not None:
                    raise DomainError(
                        "rules_frozen",
                        f"{gs.station_id}/{gs.group_id} 规则已冻结，身份合并须在开赛前完成",
                    )
            # 合并后若造成同赛区跨组重复，说明是误并，阻止
            spots: dict[str, str] = {}
            for pid in (from_person, into_person):
                for loc in m.footprint.get(pid, []):
                    station = loc.split("/")[0]
                    if station in spots and spots[station] != loc:
                        raise DomainError(
                            "bad_merge",
                            f"合并会造成 {station} 赛区同人居于两个组别，拒绝合并",
                        )
                    spots[station] = loc
            return self.store.append(
                self._stream("identity", into_person), "persons_merged",
                {"from_person": from_person, "into_person": into_person, "reason": reason},
                actor=actor, event_id=event_id,
            )

    # ---------------------------------------------------------------
    # 命令：冻结规则 / 轮次 / 配对
    # ---------------------------------------------------------------

    def freeze_rules(self, station_id: str, group_id: str, rules: dict, *, actor: str,
                     event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            if gs.rules is not None:
                raise DomainError("rules_frozen", "规则已冻结，不能二次冻结")
            if not gs.registrations:
                raise DomainError("no_players", "尚无报名，不能冻结规则")
            snapshot = {
                "komi": rules.get("komi", 7.5),
                "pairing_method": rules.get("pairing_method", "swiss"),
                "rounds": int(rules.get("rounds", 7)),
                "quota": int(rules.get("quota")),
                "win_points": rules.get("win_points", {"win": 1, "draw": 0.5, "loss": 0}),
                "tie_breaks": list(rules.get("tie_breaks", DEFAULT_TIEBREAKS)),
                "late_policy": rules.get("late_policy", "迟到结果须经裁判长提交为迟到事件"),
                "forfeit_policy": rules.get("forfeit_policy", "弃权判负，对方全胜"),
                "frozen_by": actor,
            }
            if "score" not in snapshot["tie_breaks"]:
                snapshot["tie_breaks"] = ["score"] + snapshot["tie_breaks"]
            if "draw" not in snapshot["tie_breaks"]:
                # 抽签兜底：冻结的破同分次序必须能产生全序，防止临界并列算错名额
                snapshot["tie_breaks"].append("draw")
            if snapshot["rounds"] < 1 or snapshot["quota"] < 1:
                raise DomainError("bad_rules", "轮次数与名额必须为正")
            rule_version = "rv-" + uuid.uuid4().hex[:10]
            payload = {"station_id": station_id, "group_id": group_id,
                       "rule_version": rule_version, "rules": snapshot}
            return self.store.append(
                self._stream("group", station_id, group_id), "rules_frozen", payload,
                actor=actor, event_id=event_id,
                expected_version=self.store.stream_version(self._stream("group", station_id, group_id)),
            )

    def freeze_rounds(self, station_id: str, group_id: str, schedule: list[dict] | None = None,
                      *, actor: str, event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._require_rules(gs)
            if gs.rounds is not None:
                raise DomainError("rounds_frozen", "轮次已冻结")
            rounds = gs.rules["rounds"]
            schedule = schedule or [{"round_no": i + 1} for i in range(rounds)]
            if len(schedule) != rounds or any(s["round_no"] != i + 1 for i, s in enumerate(schedule)):
                raise DomainError("bad_schedule", "赛程必须与规则轮次数一一对应")
            payload = {"station_id": station_id, "group_id": group_id,
                       "rounds": rounds, "schedule": schedule}
            return self.store.append(
                self._stream("group", station_id, group_id), "rounds_frozen", payload,
                actor=actor, event_id=event_id,
            )

    def suggest_next_pairings(self, station_id: str, group_id: str, round_no: int) -> list[dict]:
        m = self._load()
        gs = self._group(m, station_id, group_id)
        self._require_rules(gs)
        players = [p for p, r in gs.registrations.items()
                   if not r.withdrawn_from or r.withdrawn_from > round_no]
        played: set[tuple[str, str]] = set()
        scores = self._provisional_scores(gs)
        for outcomes in gs.outcomes.values():
            for oc in outcomes:
                if oc.effective() and oc.black:
                    played.add((oc.white, oc.black))
        return suggest_pairings(players, played=played, scores=scores, round_no=round_no)

    def freeze_pairings(self, station_id: str, group_id: str, round_no: int, boards: list[dict],
                        *, actor: str, event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            self._require_rules(gs)
            if gs.rounds is None:
                raise DomainError("rounds_not_frozen", "请先冻结轮次")
            if not (1 <= round_no <= gs.rules["rounds"]):
                raise DomainError("bad_round", "轮次超出冻结赛程")
            if round_no in gs.pairings:
                raise DomainError("pairings_frozen", f"第 {round_no} 轮配对已冻结")
            if round_no > 1 and (round_no - 1) not in gs.pairings:
                raise DomainError("bad_order", "必须按轮次顺序冻结配对")
            self._validate_boards(gs, round_no, boards)
            payload = {"station_id": station_id, "group_id": group_id,
                       "round_no": round_no, "boards": boards}
            return self.store.append(
                self._stream("group", station_id, group_id), "pairings_frozen", payload,
                actor=actor, event_id=event_id,
            )

    def _validate_boards(self, gs: GroupState, round_no: int, boards: list[dict]) -> None:
        active = {p for p, r in gs.registrations.items()
                  if not r.withdrawn_from or r.withdrawn_from > round_no}
        seen: set[str] = set()
        board_ids: set[str] = set()
        for b in boards:
            board = b.get("board")
            if not board or board in board_ids:
                raise DomainError("bad_board", f"台号缺失或重复: {board}")
            board_ids.add(board)
            w = b.get("white")
            if w not in gs.registrations:
                raise DomainError("bad_board", f"未知棋手 {w}")
            if w not in active:
                raise DomainError("bad_board", f"{w} 已退赛，不能出现在第 {round_no} 轮配对")
            if w in seen:
                raise DomainError("bad_board", f"{w} 在本轮出现多次")
            seen.add(w)
            if b.get("bye"):
                if b.get("black") is not None:
                    raise DomainError("bad_board", "轮空台不能有黑方")
                continue
            blk = b.get("black")
            if blk not in gs.registrations:
                raise DomainError("bad_board", f"台次 {board} 黑方无效")
            if blk not in active:
                raise DomainError("bad_board", f"{blk} 已退赛，不能出现在第 {round_no} 轮配对")
            if blk in seen:
                raise DomainError("bad_board", f"台次 {board} 棋手重复")
            seen.add(blk)
        missing = active - seen
        if missing:
            raise DomainError("bad_board", f"配对未覆盖在赛棋手: {sorted(missing)}")

    # ---------------------------------------------------------------
    # 命令：结果 / 弃权 / 迟到 / 更正 / 申诉 / 退赛
    # ---------------------------------------------------------------

    def submit_result(self, station_id: str, group_id: str, round_no: int, board: str,
                      winner: str, *, record_hash: str | None = None,
                      signature_source: str | None = None, late: bool = False,
                      actor: str, event_id: str | None = None,
                      request_key: str | None = None) -> dict:
        with self._lock:
            if request_key is not None:
                replay = self.store.lookup_idempotency(request_key)
                if replay is not None:
                    return replay
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            frozen = self._frozen_board(gs, round_no, board)
            if frozen.get("bye"):
                raise DomainError("bad_board", "轮空台不接受结果提交（系统自动计轮空胜）")
            existing = self._current_outcome(gs, round_no, board)
            pw, pb = _points(winner)
            result = {"white": "win_white", "black": "win_black",
                      "draw": "draw", "none": "double_loss"}[winner]
            if existing is not None:
                # 重新投递：完全相同的事实 → 拒绝重复但不产生新事实；不同 → 必须走更正
                if existing.result == result:
                    raise DomainError(
                        "duplicate_fact",
                        f"第 {round_no} 轮 {board} 的同一结果已确认（事件 {existing.source_event}）；"
                        "事实唯一，重复投递不会改变任何记录",
                    )
                raise DomainError(
                    "result_exists",
                    f"第 {round_no} 轮 {board} 已有生效结果（{existing.result}），"
                    "改变结果必须提交更正事件",
                )
            # 契约：新对局胜负必须保留棋谱哈希与签字来源；double_loss（双无胜负）无棋谱
            if not signature_source:
                raise DomainError("missing_signature", "提交结果必须给出签字来源 signature_source")
            if winner != "none" and not record_hash:
                raise DomainError("missing_record_hash", "提交对局胜负必须给出棋谱哈希 record_hash")
            if not late and self._is_late(gs, round_no):
                raise DomainError(
                    "late_window",
                    f"第 {round_no} 轮已进入迟到窗口，请以迟到结果事件提交",
                )
            etype = "late_result_submitted" if late else "result_submitted"
            payload = {"station_id": station_id, "group_id": group_id, "round_no": round_no,
                       "board": board, "white": frozen["white"], "black": frozen.get("black"),
                       "result": result, "points_white": pw, "points_black": pb,
                       "record_hash": record_hash, "signature_source": signature_source}
            ev = self.store.append(
                self._stream("group", station_id, group_id), etype, payload,
                actor=actor, event_id=event_id, request_key=request_key,
            )
            self._maybe_recalc_confirmed(m, gs, actor)
            return ev

    def submit_forfeit(self, station_id: str, group_id: str, round_no: int, board: str,
                       winner: str, reason: str, *, signature_source: str | None = None,
                       actor: str, event_id: str | None = None,
                       request_key: str | None = None) -> dict:
        with self._lock:
            if request_key is not None:
                replay = self.store.lookup_idempotency(request_key)
                if replay is not None:
                    return replay
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            frozen = self._frozen_board(gs, round_no, board)
            if frozen.get("bye"):
                raise DomainError("bad_board", "轮空台不接受弃权录入")
            existing = self._current_outcome(gs, round_no, board)
            if existing is not None:
                raise DomainError("result_exists", "该台已有结果，改变须走更正/申诉")
            if winner not in ("white", "black", "none"):
                raise DomainError("bad_result", "弃权胜方须为 white/black/none")
            if not signature_source:
                raise DomainError("missing_signature", "弃权录入必须给出签字来源 signature_source")
            pw, pb = _points(winner)
            result_map = {"white": "forfeit_win_white", "black": "forfeit_win_black",
                          "none": "double_forfeit_loss"}
            payload = {"station_id": station_id, "group_id": group_id, "round_no": round_no,
                       "board": board, "white": frozen["white"], "black": frozen.get("black"),
                       "result": result_map[winner], "points_white": pw, "points_black": pb,
                       "reason": reason, "signature_source": signature_source}
            ev = self.store.append(
                self._stream("group", station_id, group_id), "forfeit_recorded", payload,
                actor=actor, event_id=event_id, request_key=request_key,
            )
            self._maybe_recalc_confirmed(m, gs, actor)
            return ev

    def correct_result(self, station_id: str, group_id: str, round_no: int, board: str,
                       winner: str, reason: str, *, record_hash: str | None = None,
                       signature_source: str | None = None, actor: str,
                       event_id: str | None = None) -> dict:
        """录入纠错：永不覆盖旧事件，追加 result_corrected，旧结果标失效。"""
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            current = self._current_outcome(gs, round_no, board)
            if current is None:
                raise DomainError("no_result", "该台尚无结果，应直接提交，而非更正")
            pw, pb = _points(winner)
            result = "draw" if winner == "draw" else ("win_white" if winner == "white" else
                                                       ("win_black" if winner == "black" else
                                                        "double_loss"))
            if current.result == result:
                raise DomainError("same_fact", "更正后的结果与当前事实相同，无需更正")
            if not signature_source:
                raise DomainError("missing_signature", "更正必须给出新结果的签字来源")
            if winner != "none" and not record_hash:
                raise DomainError("missing_record_hash", "更正对局胜负必须给出新棋谱哈希")
            payload = {"station_id": station_id, "group_id": group_id, "round_no": round_no,
                       "board": board, "white": current.white, "black": current.black,
                       "result": result, "points_white": pw, "points_black": pb,
                       "old_event_id": current.source_event, "reason": reason,
                       "record_hash": record_hash, "signature_source": signature_source}
            ev = self.store.append(
                self._stream("group", station_id, group_id), "result_corrected", payload,
                actor=actor, event_id=event_id,
            )
            self._maybe_recalc_confirmed(m, gs, actor, cause=ev)
            return ev

    def file_appeal(self, station_id: str, group_id: str, round_no: int, board: str,
                    claim: str, *, actor: str, event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            self._frozen_board(gs, round_no, board)
            appeal_id = "a" + uuid.uuid4().hex[:10]
            return self.store.append(
                self._stream("group", station_id, group_id), "appeal_filed",
                {"station_id": station_id, "group_id": group_id, "round_no": round_no,
                 "board": board, "appeal_id": appeal_id, "claim": claim, "status": "pending"},
                actor=actor, event_id=event_id,
            )

    def decide_appeal(self, station_id: str, group_id: str, appeal_id: str, decision: str,
                      winner: str | None = None, reason: str = "", *, panel: str,
                      record_hash: str | None = None, signature_source: str | None = None,
                      actor: str, event_id: str | None = None) -> dict:
        """申诉裁决：uphold 维持（原结果有效）；amend 修正（新事件替换）。"""
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            appeal = self._find_appeal(gs, appeal_id)
            current = self._current_outcome(gs, appeal["round_no"], appeal["board"])
            if current is None:
                raise DomainError("no_result", "被申诉对局无生效结果")
            if decision == "uphold":
                ev = self.store.append(
                    self._stream("group", station_id, group_id), "appeal_rejected",
                    {"station_id": station_id, "group_id": group_id,
                     "round_no": appeal["round_no"], "board": appeal["board"],
                     "appeal_id": appeal_id, "reason": reason, "panel": panel},
                    actor=actor, event_id=event_id,
                )
                return ev
            if decision != "amend" or winner is None:
                raise DomainError("bad_decision", "改判须给 decision=amend 与 winner")
            if not signature_source:
                raise DomainError("missing_signature", "改判必须给出签字来源")
            if winner != "none" and not record_hash:
                raise DomainError("missing_record_hash", "改判对局胜负必须给出棋谱哈希")
            pw, pb = _points(winner)
            result = "draw" if winner == "draw" else ("win_white" if winner == "white" else
                                                       ("win_black" if winner == "black" else
                                                        "double_loss"))
            payload = {"station_id": station_id, "group_id": group_id,
                       "round_no": appeal["round_no"], "board": appeal["board"],
                       "white": current.white, "black": current.black,
                       "result": result, "points_white": pw, "points_black": pb,
                       "old_event_id": current.source_event, "appeal_id": appeal_id,
                       "reason": reason, "panel": panel,
                       "record_hash": record_hash, "signature_source": signature_source}
            ev = self.store.append(
                self._stream("group", station_id, group_id), "appeal_amended", payload,
                actor=actor, event_id=event_id,
            )
            self._maybe_recalc_confirmed(m, gs, actor, cause=ev)
            return ev

    def withdraw(self, station_id: str, group_id: str, person_id: str, from_round: int,
                 reason: str, *, actor: str, event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            if person_id not in gs.registrations:
                raise DomainError("not_found", "棋手不在本组")
            reg = gs.registrations[person_id]
            if reg.withdrawn_from is not None:
                raise DomainError("already_withdrawn", "该棋手已退赛")
            if not (1 <= from_round <= gs.rules["rounds"] if gs.rules else True):
                raise DomainError("bad_round", "退赛起始轮次非法")
            ev = self.store.append(
                self._stream("group", station_id, group_id), "player_withdrawn",
                {"station_id": station_id, "group_id": group_id, "person_id": person_id,
                 "from_round": from_round, "reason": reason},
                actor=actor, event_id=event_id,
            )
            # 重新投影：已冻结但无结果的台次，退赛方弃权负、对手全胜（裁决来源补录）
            m2 = self._load()
            gs2 = m2.groups[(station_id, group_id)]
            total_rounds = gs2.rules["rounds"] if gs2.rules else 0
            for rnd in range(from_round, total_rounds + 1):
                for b in gs2.pairings.get(rnd, []):
                    if b.get("bye") or person_id not in (b["white"], b.get("black")):
                        continue
                    if self._current_outcome(gs2, rnd, b["board"]) is not None:
                        continue
                    other_wins = "white" if b.get("black") == person_id else "black"
                    pw, pb = _points(other_wins)
                    self.store.append(
                        self._stream("group", station_id, group_id), "forfeit_recorded",
                        {"station_id": station_id, "group_id": group_id, "round_no": rnd,
                         "board": b["board"], "white": b["white"], "black": b.get("black"),
                         "result": f"forfeit_win_{other_wins}", "points_white": pw,
                         "points_black": pb, "reason": f"对手退赛: {reason}",
                         "signature_source": f"withdraw:{ev['event_id']}"},
                        actor=actor,
                    )
            m3 = self._load()
            self._maybe_recalc_confirmed(m3, m3.groups[(station_id, group_id)], actor, cause=ev)
            return ev

    # ---------------------------------------------------------------
    # 命令：确认 / 锁榜
    # ---------------------------------------------------------------

    def confirm_group(self, station_id: str, group_id: str, *, actor: str,
                      event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            self._not_locked(gs)
            if gs.confirmed:
                raise DomainError("already_confirmed", "本组已确认")
            self._require_complete(gs)
            ranked, _ = self._ranked(m, gs)
            chosen, excluded = self._select_quota(m, gs, ranked)
            blocked = {q["person_id"] for q in excluded} | {
                pid for pid, reg in gs.registrations.items() if reg.withdrawn_from is not None
            }
            self._assert_no_tie_on_line(gs, ranked, chosen, blocked)
            fingerprint = _canonical_hash(
                [{"player_id": r["player_id"], "rank_no": r["rank_no"],
                  "score": r["score"], "sos": r["sos"], "cumulative": r["cumulative"]}
                 for r in ranked]
            )
            payload = {"station_id": station_id, "group_id": group_id,
                       "rule_version": gs.rule_version,
                       "qualified": chosen, "excluded_duplicates": excluded,
                       "standings_fingerprint": fingerprint}
            ev = self.store.append(
                self._stream("group", station_id, group_id), "group_confirmed", payload,
                actor=actor, event_id=event_id,
            )
            for q in chosen:
                self._notify(
                    scope=f"{station_id}/{group_id}", kind="qualified",
                    person_id=q["person_id"],
                    message=f"您以第 {q['rank_no']} 名获得总决赛资格（{gs.rule_version}）",
                    actor=actor,
                )
            for ex in excluded:
                self._notify(
                    scope=f"{station_id}/{group_id}", kind="duplicate_excluded",
                    person_id=ex["person_id"],
                    message=f"名次 {ex['rank_no']}，但总决赛席位已由 {ex['occupied_at']} 占用，顺延给下一位",
                    actor=actor,
                )
            return ev

    def lock_finals(self, name: str, *, actor: str, event_id: str | None = None) -> dict:
        with self._lock:
            m = self._load()
            self._require_tournament(m)
            if m.finals is not None:
                raise DomainError("finals_locked", "总决赛名单已锁定")
            unconfirmed = [f"{sid}/{gid}" for (sid, gid), gs in m.groups.items() if not gs.confirmed]
            if unconfirmed:
                raise DomainError("not_all_confirmed", f"以下组别未确认: {unconfirmed}")
            # 唯一事实：一人一席断言
            roster = []
            for pid, seat in m.seats.items():
                roster.append({"person_id": pid, **seat})
            roster.sort(key=lambda x: (x["station_id"], x["group_id"], x["rank"]))
            if len({r["person_id"] for r in roster}) != len(roster):
                raise DomainError("invariant_violation", "检测到一人多席，拒绝锁定")
            roster_hash = _canonical_hash(roster)
            payload = {"finals_id": "F1", "name": name, "roster": roster,
                       "roster_hash": roster_hash,
                       "rule_versions": sorted({r["rule_version"] for r in roster})}
            return self.store.append(
                self._stream("finals", "F1"), "finals_locked", payload,
                actor=actor, event_id=event_id,
            )

    # ---------------------------------------------------------------
    # 资格选择与递补重算
    # ---------------------------------------------------------------

    def _assert_no_tie_on_line(self, gs: GroupState, ranked: list[dict],
                               chosen: list[dict], blocked: set[str] | None = None) -> None:
        """若同一并列组中有人入选、有人既未入选也非退赛/重复占席，则名额跨在并列线上。"""
        blocked = blocked or set()
        chosen_pids = {q["person_id"] for q in chosen}
        groups: dict[str, list[str]] = {}
        for r in ranked:
            groups.setdefault(str(r["rank_no"]), []).append(r["player_id"])
        for r in ranked:
            members = groups[str(r["rank_no"])]
            if len(members) <= 1:
                continue
            has_in = any(p in chosen_pids for p in members)
            has_ambiguous_out = any(
                p not in chosen_pids and p not in blocked for p in members
            )
            if has_in and has_ambiguous_out:
                raise DomainError(
                    "tie_on_quota_line",
                    f"名次 {r['rank']} 的并列者分处晋级线两侧，须先经仲裁/加赛确定次序",
                )

    def _select_quota(self, m: ReadModel, gs: GroupState, ranked: list[dict]):
        """按冻结规则的名额，自上而下跳过退赛者和已在其他站占席者。"""
        quota = gs.rules["quota"]
        chosen, excluded = [], []
        here = f"{gs.station_id}/{gs.group_id}"
        for r in ranked:
            pid = r["player_id"]
            reg = gs.registrations.get(pid)
            if reg and reg.withdrawn_from is not None:
                continue
            occupied = m.seats.get(pid)
            if occupied and f"{occupied['station_id']}/{occupied['group_id']}" != here:
                excluded.append({
                    "person_id": pid, "rank_no": r["rank_no"],
                    "occupied_at": f"{occupied['station_id']}/{occupied['group_id']}",
                })
                continue
            if len(chosen) >= quota:
                break
            chosen.append({
                "person_id": pid, "rank_no": r["rank_no"], "name": reg.name if reg else pid,
                "score": r["score"], "sos": r["sos"], "cumulative": r["cumulative"],
                "rule_version": gs.rule_version,
            })
        return chosen, excluded

    def _maybe_recalc_confirmed(self, m: ReadModel, gs: GroupState, actor: str, cause=None):
        """若结果变化发生在已确认组：只重算这一组，产生递补与通知。"""
        if not gs.confirmed:
            return
        m = self._load()
        gs = m.groups[(gs.station_id, gs.group_id)]
        ranked, _ = self._ranked(m, gs)
        chosen, excluded = self._select_quota(m, gs, ranked)
        old = {q["person_id"]: q for q in gs.qualified}
        new = {q["person_id"]: q for q in chosen}
        promoted = [q for pid, q in new.items() if pid not in old]
        lost = [q for pid, q in old.items() if pid not in new]
        if not promoted and not lost and gs.excluded_duplicates == excluded:
            return
        self.store.append(
            self._stream("group", gs.station_id, gs.group_id), "quota_adjusted",
            {"station_id": gs.station_id, "group_id": gs.group_id,
             "rule_version": gs.rule_version, "qualified": chosen,
             "excluded_duplicates": excluded,
             "cause_event": cause["event_id"] if cause else None,
             "promoted": [q["person_id"] for q in promoted],
             "lost": [q["person_id"] for q in lost]},
            actor=actor,
        )
        for q in promoted:
            self._notify(
                scope=f"{gs.station_id}/{gs.group_id}", kind="promoted",
                person_id=q["person_id"],
                message=(f"因名次变化/退赛递补，您以第 {q['rank_no']} 名取得总决赛资格"
                         f"（{gs.rule_version}）"),
                actor=actor,
            )
        for q in lost:
            self._notify(
                scope=f"{gs.station_id}/{gs.group_id}", kind="seat_lost",
                person_id=q["person_id"],
                message=f"第七轮更正/重算后，您的名次（{q['rank_no']}）不再在晋级线内",
                actor=actor,
            )
            # 该人若在其他已确认组因重复占席被排除，仅通知组委会复核，不擅动别组事实
            for (sid2, gid2), gs2 in m.groups.items():
                if (sid2, gid2) == (gs.station_id, gs.group_id):
                    continue
                if any(e["person_id"] == q["person_id"] for e in gs2.excluded_duplicates):
                    self._notify(
                        scope=f"{sid2}/{gid2}", kind="review_suggested",
                        person_id=q["person_id"],
                        message=(f"{gs.station_id}/{gs.group_id} 重算后 {q['person_id']} 空出席位，"
                                 f"本组曾因重复占席将其排除，请组委会决定是否发起复核递补"),
                        actor=actor,
                    )

    def reconfirm_group(self, station_id: str, group_id: str, *, actor: str,
                        event_id: str | None = None) -> dict:
        """组委会对已确认但收到复核通知的组别重新核算（仍是追加 quota_adjusted）。"""
        with self._lock:
            m = self._load()
            gs = self._group(m, station_id, group_id)
            if not gs.confirmed:
                raise DomainError("not_confirmed", "本组尚未确认，无需复核")
            self._not_locked(gs)
            self._require_complete(gs)
            ranked, _ = self._ranked(m, gs)
            chosen, excluded = self._select_quota(m, gs, ranked)
            old = {q["person_id"]: q for q in gs.qualified}
            promoted = [q for q in chosen if q["person_id"] not in old]
            lost = [q for q in gs.qualified if q["person_id"] not in {c["person_id"] for c in chosen}]
            if not promoted and not lost:
                raise DomainError("no_change", "复核后名单无变化")
            ev = self.store.append(
                self._stream("group", station_id, group_id), "quota_adjusted",
                {"station_id": station_id, "group_id": group_id,
                 "rule_version": gs.rule_version, "qualified": chosen,
                 "excluded_duplicates": excluded,
                 "cause_event": "manual_review",
                 "promoted": [q["person_id"] for q in promoted],
                 "lost": [q["person_id"] for q in lost]},
                actor=actor, event_id=event_id,
            )
            for q in promoted:
                self._notify(
                    scope=f"{station_id}/{group_id}", kind="promoted",
                    person_id=q["person_id"],
                    message=f"组委会复核递补：您以第 {q['rank_no']} 名取得总决赛资格",
                    actor=actor,
                )
            return ev

    # ---------------------------------------------------------------
    # 查询
    # ---------------------------------------------------------------

    def standings(self, station_id: str, group_id: str) -> dict:
        m = self._load()
        gs = self._group(m, station_id, group_id)
        ranked, tabs = self._ranked(m, gs)
        return {
            "station_id": station_id, "group_id": group_id,
            "rule_version": gs.rule_version,
            "tie_breaks": gs.rules["tie_breaks"] if gs.rules else None,
            "confirmed": gs.confirmed,
            "rows": [self._public_row(m, gs, r) for r in ranked],
        }

    def _public_row(self, m: ReadModel, gs: GroupState, r: dict) -> dict:
        reg = gs.registrations.get(r["player_id"])
        seat = m.seats.get(r["player_id"])
        return {
            "rank": r["rank"], "rank_no": r["rank_no"], "tied": r["tied"],
            "player_id": r["player_id"], "name": reg.name if reg else None,
            "score": r["score"], "sos": r["sos"], "cumulative": r["cumulative"],
            "wins": r["wins"], "direct_among_tied": r["direct_among_tied"],
            "withdrawn": bool(reg and reg.withdrawn_from is not None),
            "qualified_here": bool(seat and f"{seat['station_id']}/{seat['group_id']}" ==
                                   f"{gs.station_id}/{gs.group_id}"),
            "duplicate_excluded_here": any(
                e["person_id"] == r["player_id"] for e in gs.excluded_duplicates),
        }

    def explain_rank(self, station_id: str, group_id: str, person_id: str) -> dict:
        """解释为何排在临界名次之前：逐级破同分对比。"""
        m = self._load()
        gs = self._group(m, station_id, group_id)
        ranked, _ = self._ranked(m, gs)
        idx = next((i for i, r in enumerate(ranked) if r["player_id"] == person_id), None)
        if idx is None:
            raise DomainError("not_found", "棋手不在本组排名")
        me, target = ranked[idx], ranked[idx - 1] if idx > 0 else (
            ranked[idx + 1] if idx + 1 < len(ranked) else None)
        direction = "ahead" if idx > 0 else "behind"
        reg = gs.registrations[person_id]
        row_key = {"score": "score", "sos": "sos", "sosos": "sosos",
                   "cumulative": "cumulative", "wins": "wins", "black": "black_wins",
                   "direct": "direct_among_tied", "draw": "draw_pick"}
        comparisons = []
        if target:
            decided = False
            for tb in gs.rules["tie_breaks"]:
                if tb == "birth":
                    # 出生年份属个人敏感信息，只标注其在冻结次序中的位置，不暴露对方年份
                    comparisons.append({
                        "tiebreak": tb, "mine": reg.birth_year, "rivals": None,
                        "decides": False, "private": True,
                    })
                    continue
                key = row_key.get(tb)
                if key is None:
                    continue
                a, b = me.get(key), target.get(key)
                if a is None or b is None:
                    continue
                decides = not decided and abs(float(a) - float(b)) > 1e-9
                comparisons.append({
                    "tiebreak": tb, "mine": a, "rivals": b, "decides": decides,
                })
                if decides:
                    decided = True
        qualified_ranks = {q["rank_no"] for q in gs.qualified}
        return {
            "player_id": person_id, "name": reg.name,
            "rule_version": gs.rule_version, "rules_snapshot": gs.rules,
            "my_rank": me["rank"], "score": me["score"],
            "compared_with": ({"player_id": target["player_id"],
                               "name": gs.registrations[target["player_id"]].name,
                               "rank": target["rank"], "position": direction} if target else None),
            "tiebreak_path": comparisons,
            "on_quota_line": me["rank_no"] in qualified_ranks,
            "quota": gs.rules["quota"],
        }

    def trace(self, person_id: str, viewer_station: str | None = None) -> dict:
        """棋手经过的对局、引用的规则版本与资格结论。

        viewer_station 给定时，跨站信息只显示赛区代码与状态，不含完整身份字段；
        证件号任何视图都不明文返回。
        """
        m = self._load()
        if person_id not in m.persons:
            # 兼容合并前的旧 id：重定向到存续 person
            for pid, entry in m.persons.items():
                if person_id in entry["merged_from"]:
                    person_id = pid
                    break
            else:
                raise DomainError("not_found", "棋手不存在")
        locations = []
        for (sid, gid), gs in m.groups.items():
            reg = gs.registrations.get(person_id)
            if reg is None:
                continue
            games = []
            rounds_count = gs.rules["rounds"] if gs.rules else 0
            for rnd in range(1, rounds_count + 1):
                for b in gs.pairings.get(rnd, []):
                    if person_id not in (b["white"], b.get("black")):
                        continue
                    oc = self._current_outcome(gs, rnd, b["board"])
                    entry = {"round_no": rnd, "board": b["board"],
                             "color": "white" if b["white"] == person_id else "black",
                             "opponent": (b.get("black") if b["white"] == person_id else b["white"]),
                             "bye": bool(b.get("bye"))}
                    if oc:
                        entry.update({
                            "result": oc.result,
                            "points": oc.points_white if oc.white == person_id else oc.points_black,
                            "source_event": oc.source_event,
                            "record_hash": oc.record_hash,
                            "signature_source": oc.signature_source,
                            "late": oc.late,
                            "corrected": bool(oc.correction_of or oc.appeal_ref),
                            "origin_event": oc.correction_of,
                        })
                    games.append(entry)
            seat = next((q for q in gs.qualified if q["person_id"] == person_id), None)
            dup = next((e for e in gs.excluded_duplicates if e["person_id"] == person_id), None)
            loc = {
                "station_id": sid, "group_id": gid,
                "rule_version": gs.rule_version,
                "name": reg.name,
                "cert": {"type": reg.cert_type, "masked": reg.masked_cert},
                "games": games,
                "withdrawn_from": reg.withdrawn_from,
                "status": ("qualified" if seat else "duplicate_excluded" if dup else
                           "withdrawn" if reg.withdrawn_from is not None else "played"),
            }
            if seat:
                loc["qualified_rank"] = seat["rank_no"]
            if dup:
                loc["occupied_at"] = dup["occupied_at"]
            # 跨站视图最小化
            if viewer_station and sid != viewer_station:
                loc = {"station_id": sid, "group_id": gid,
                       "status": loc["status"],
                       "qualified_rank": loc.get("qualified_rank")}
            locations.append(loc)
        return {"person_id": person_id, "locations": locations,
                "merged_from": m.persons.get(person_id, {}).get("merged_from", [])}

    def notifications(self, station_id: str | None = None) -> list[dict]:
        m = self._load()
        if station_id is None:
            return list(m.notifications)
        return [n for n in m.notifications if n["scope"].startswith(f"{station_id}/")]

    def finals_roster(self) -> dict:
        m = self._load()
        if m.finals is None:
            raise DomainError("not_locked", "总决赛尚未锁定")
        roster = []
        for r in m.finals["roster"]:
            reg = None
            for (sid, gid), gs in m.groups.items():
                if sid == r["station_id"] and gid == r["group_id"]:
                    reg = gs.registrations.get(r["person_id"])
            roster.append({**r, "name": reg.name if reg else None})
        return {**m.finals, "roster": roster}

    # ---------------------------------------------------------------
    # 资格审核员视图
    # ---------------------------------------------------------------

    def review_flags(self, include_closed: bool = False) -> list[dict]:
        m = self._load()
        if include_closed:
            return list(m.similar_flags)
        return [f for f in m.similar_flags if f["status"] == "open"]

    def person_index(self) -> dict:
        """审核员的全局索引：每人的证件码数与跨站足迹；不含明文证件。"""
        m = self._load()
        out = {}
        for pid, entry in m.persons.items():
            names = sorted({reg.name for gs in m.groups.values()
                            for reg in gs.registrations.values() if reg.person_id == pid})
            out[pid] = {
                "match_codes": len(entry["codes"]),
                "merged_from": entry["merged_from"],
                "names": names,
                "footprint": m.footprint.get(pid, []),
                "seat": m.seats.get(pid),
            }
        return out

    def unseal(self, seal_id: str, *, actor: str, reason: str) -> dict:
        """审核员拆封完整身份，必留审计记录。"""
        return self.store.unseal_identity(seal_id, actor=actor, reason=reason) or {}

    def seal_audit_log(self) -> list[dict]:
        return self.store.seal_audit_log()

    def event_log(self, station_id: str | None = None, group_id: str | None = None) -> list[dict]:
        m = self._load()
        events = self.store.read_all()
        if station_id is None:
            return events
        out = []
        for ev in events:
            p = ev["payload"]
            if isinstance(p, dict) and p.get("station_id") == station_id:
                if group_id is None or p.get("group_id") in (None, group_id):
                    out.append(ev)
        return out

    def verify(self) -> dict:
        return self.store.verify_chain()

    # ---------------------------------------------------------------
    # 内部工具
    # ---------------------------------------------------------------

    def _ranked(self, m: ReadModel, gs: GroupState):
        self._require_rules(gs)
        rounds_count = gs.rules["rounds"]
        players = list(gs.registrations)
        games: list[GameRecord] = []
        for outcomes in gs.outcomes.values():
            for oc in outcomes:
                if oc.effective():
                    games.append(GameRecord(
                        round_no=oc.round_no, board=oc.board, white=oc.white,
                        black=oc.black or oc.white, result=oc.result,
                        points_white=oc.points_white, points_black=oc.points_black,
                        source_event=oc.source_event,
                    ))
        # 冻结的轮空台若无结果事件，合成轮空胜（大分 1，对手分按虚拟对手折算）
        recorded_byes = {oc.round_no for outcomes in gs.outcomes.values() for oc in outcomes
                         if oc.effective() and oc.black is None}
        for rnd, boards in gs.pairings.items():
            for b in boards:
                if b.get("bye") and rnd not in recorded_byes:
                    games.append(GameRecord(
                        round_no=rnd, board=b["board"], white=b["white"], black=b["white"],
                        result="bye_win", points_white=1.0, points_black=0.0,
                        source_event=f"bye:{gs.station_id}:{gs.group_id}:{rnd}",
                    ))
        tabs = compute_tables(games, players, rounds_count)
        birth = {pid: reg.birth_year for pid, reg in gs.registrations.items()
                 if reg.birth_year} if "birth" in gs.rules["tie_breaks"] else None
        ranked = rank_players(
            tabs, gs.rules["tie_breaks"], birth_years=birth,
            draw_seed=f"{gs.station_id}|{gs.group_id}|{gs.rule_version}",
        )
        return ranked, tabs

    def _provisional_scores(self, gs: GroupState) -> dict[str, float]:
        scores = {p: 0.0 for p in gs.registrations}
        for outcomes in gs.outcomes.values():
            for oc in outcomes:
                if oc.effective():
                    scores[oc.white] = scores.get(oc.white, 0.0) + oc.points_white
                    if oc.black:
                        scores[oc.black] = scores.get(oc.black, 0.0) + oc.points_black
        return scores

    def _current_outcome(self, gs: GroupState, round_no: int, board: str) -> Optional[Outcome]:
        for oc in gs.outcomes.get((round_no, board), []):
            if oc.effective():
                return oc
        return None

    def _frozen_board(self, gs: GroupState, round_no: int, board: str) -> dict:
        if gs.rounds is None:
            raise DomainError("rounds_not_frozen", "轮次尚未冻结")
        for b in gs.pairings.get(round_no, []):
            if b["board"] == board:
                return b
        raise DomainError("board_not_frozen", f"第 {round_no} 轮 {board} 不在冻结配对中")

    def _is_late(self, gs: GroupState, round_no: int) -> bool:
        """后续轮次已出现任何冻结结果即视为迟到窗口。"""
        for (rnd, _board), outcomes in gs.outcomes.items():
            if rnd > round_no and any(o.effective() for o in outcomes):
                return True
        return False

    def _find_appeal(self, gs: GroupState, appeal_id: str) -> dict:
        for ev in self.store.read_stream(self._stream("group", gs.station_id, gs.group_id)):
            if ev["type"] == "appeal_filed" and ev["payload"].get("appeal_id") == appeal_id:
                return ev["payload"]
        raise DomainError("not_found", "申诉不存在")

    def _find_event(self, event_id: str) -> dict:
        for ev in self.store.read_all():
            if ev["event_id"] == event_id:
                return ev
        raise DomainError("not_found", "事件不存在")

    def _require_complete(self, gs: GroupState) -> None:
        if gs.rounds is None:
            raise DomainError("rounds_not_frozen", "轮次未冻结")
        rounds_count = gs.rules["rounds"]
        for rnd in range(1, rounds_count + 1):
            if rnd not in gs.pairings:
                raise DomainError("incomplete", f"第 {rnd} 轮配对未冻结")
        for rnd in range(1, rounds_count + 1):
            for b in gs.pairings[rnd]:
                if b.get("bye"):
                    continue
                if self._current_outcome(gs, rnd, b["board"]) is None:
                    raise DomainError("incomplete", f"第 {rnd} 轮 {b['board']} 缺结果")

    def _notify(self, *, scope: str, kind: str, person_id: str, message: str, actor: str) -> dict:
        return self.store.append(
            self._stream("notifications"), "notification_emitted",
            {"scope": scope, "kind": kind, "person_id": person_id, "message": message},
            actor=actor,
        )

    def _group(self, m: ReadModel, station_id: str, group_id: str) -> GroupState:
        gs = m.groups.get((station_id, group_id))
        if gs is None:
            raise DomainError("not_found", f"赛区/组别 {station_id}/{group_id} 不存在")
        return gs

    def _require_tournament(self, m: ReadModel) -> None:
        if not m.tournament:
            raise DomainError("not_initialized", "请先创建赛事")

    def _require_rules(self, gs: GroupState) -> None:
        if gs.rules is None:
            raise DomainError("rules_not_frozen", "规则尚未冻结")

    def _not_locked(self, gs: GroupState) -> None:
        if gs.finals_locked:
            raise DomainError("finals_locked", "总决赛名单已锁定，事实不可再改变")
