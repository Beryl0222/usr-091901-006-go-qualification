"""赛区聚合：赛前冻结、报名入口、配对冻结、结果、迟到结果、
申诉裁决、录入纠错、退赛与终赛。

所有变更都以独立事件追加：原始结果永不改写，纠错/裁决/迟到
结果分别以 ResultCorrected / AppealRuled / LateResultRecorded
新事件改变“当前效力”，并保留被取代事件的序号链。
"""

from events import EventError
from rules import normalize_rules, RuleBook, VALID_RESULT_TYPES
from standings import compute_standings, swiss_pairings

OUTCOME_BY_TYPE = {
    "胜负": {"黑胜", "白胜", "和棋"},
    "弃权": {"黑弃权", "白弃权", "双方负"},
}
HEX64 = __import__("re").compile(r"^[0-9a-f]{64}$")


class StationService:
    def __init__(self, store, identity):
        self.store = store
        self.identity = identity
        self.stations = {}            # station_id -> RuleBook
        self.entrants = {}            # station -> group -> set(登记 pid)
        self.pairings = {}            # station -> round -> [game]
        self.effective = {}           # station -> game_id -> 对局效力状态
        self.withdrawn = {}           # station -> {pid: {group, from_round}}
        self.projected = set()        # 已投影晋级名单的赛区（赛果冻结）
        self.finals_locked = False
        self.finalized = {}           # station -> 终赛事件
        store.subscribe(self._apply)

    # ================= 事件折叠 =================
    def _apply(self, event):
        p = event.payload
        t = event.type
        if t == "RulesFrozen":
            self.stations[p["station_id"]] = RuleBook(p["rules"])
            self.entrants.setdefault(p["station_id"], {})
            self.pairings.setdefault(p["station_id"], {})
            self.effective.setdefault(p["station_id"], {})
            self.withdrawn.setdefault(p["station_id"], {})
        elif t == "PlayerRegistered":
            groups = self.entrants.setdefault(p["station_id"], {})
            groups.setdefault(p["group"], set()).add(p["person_id"])
        elif t == "PairingsPublished":
            self.pairings.setdefault(p["station_id"], {})[p["round"]] = \
                [dict(g) for g in p["games"]]
        elif t in ("GameResultSubmitted", "LateResultRecorded",
                   "ResultCorrected", "AppealRuled"):
            self._fold_result(event)
        elif t == "PlayerWithdrawn":
            self.withdrawn.setdefault(p["station_id"], {})[p["person_id"]] = {
                "group": p["group"], "from_round": p["from_round"],
                "reason": p["reason"], "seq": event.seq}
        elif t == "StationFinalized":
            self.finalized[p["station_id"]] = event
        elif t == "PromotionProjected":
            self.projected.add(p["station_id"])
        elif t == "FinalRosterLocked":
            self.finals_locked = True

    def _require_result_mutable(self, station_id):
        """赛果可改窗口：终赛后至投影前可追加纠错/迟到/裁决；
        投影后赛果冻结，锁名单后一切成绩变更禁止。"""
        if station_id in self.projected:
            raise EventError(
                "该站晋级名单已投影，赛果冻结；资格变动请走总决赛退赛递补")
        if self.finals_locked:
            raise EventError("总决赛名单已锁定，任何成绩修正都不再生效")

    def _fold_result(self, event):
        p = event.payload
        station = p["station_id"]
        games = self.effective.setdefault(station, {})
        if event.type == "AppealRuled":
            state = games.get(p["game_id"])
            if state is None:
                return  # 无成绩的对局暂不受理
            state["appeal"] = {
                "appeal_id": p["appeal_id"], "action": p["action"],
                "basis": p["basis"], "ruled_by": p["ruled_by"],
                "seq": event.seq,
            }
            if p["action"] == "改判" and p.get("effective_outcome"):
                state["current"] = self._result_view(event, p["effective_outcome"],
                                                     p.get("result_type", "胜负"))
            state["history"].append({"seq": event.seq, "type": event.type})
            return

        view = self._result_view(event, p["outcome"], p["result_type"])
        if event.type == "ResultCorrected":
            prior = games.get(p["game_id"])
            if prior is None:
                raise EventError("纠错必须针对已有成绩的对局")
            if p.get("supersedes_seq") and \
                    p["supersedes_seq"] != prior["current"]["seq"]:
                raise EventError(
                    "纠错基准已过期：该成绩在此之后又被修正，请基于最新事实纠错")
            view["correction_reason"] = p["reason"]
        if event.type == "LateResultRecorded":
            view["late"] = True
            view["late_reason"] = p.get("reason", "")
        state = games.setdefault(p["game_id"], {
            "game_id": p["game_id"], "station_id": station,
            "group": p["group"], "round": p["round"],
            "black": p["black"], "white": p["white"],
            "history": []})
        state["group"], state["round"] = p["group"], p["round"]
        state["black"], state["white"] = p["black"], p["white"]
        state["current"] = view
        state["history"].append({"seq": event.seq, "type": event.type})

    @staticmethod
    def _result_view(event, outcome, result_type):
        p = event.payload
        return {
            "seq": event.seq, "event_type": event.type, "ts": event.ts,
            "outcome": outcome, "result_type": result_type,
            "record_hash": p.get("record_hash"),
            "record_kind": p.get("record_kind"),
            "signature": dict(p.get("signature") or {}),
            "submitted_by": event.submitted_by, "source": event.source,
        }

    # ================= 命令 =================
    def _require_open_station(self, station_id):
        book = self.stations.get(station_id)
        if book is None:
            raise EventError(f"赛区 {station_id} 尚未冻结规则，不能操作")
        return book

    def freeze(self, raw, *, submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        rules = normalize_rules(raw)
        if rules["station_id"] in self.stations:
            raise EventError("该赛区规则已经冻结，禁止二次冻结或修改")
        payload = {"station_id": rules["station_id"], "rules": rules}
        return self.store.append("RulesFrozen", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="赛前冻结")

    def publish_pairings(self, station_id, round_no, *, games=None,
                         submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        book = self._require_open_station(station_id)
        if self.finalized.get(station_id):
            raise EventError("本站已终赛，不能再发布配对")
        if not 1 <= round_no <= book["rounds"]:
            raise EventError("轮次超出冻结范围")
        if round_no in self.pairings.get(station_id, {}):
            raise EventError(f"第 {round_no} 轮配对已冻结，不能更改")
        if round_no > 1 and (round_no - 1) not in self.pairings.get(station_id, {}):
            raise EventError("必须按轮次顺序冻结配对")

        groups_players = self._root_entrants(station_id)
        if games is None:
            published = []
            for group in book.group_names:
                past = self._games_through(station_id, round_no - 1)
                group_games = [g for g in past if g["group"] == group]
                previous_pairs, colors = self._pairing_history(group_games)
                generated = swiss_pairings(
                    book, group,
                    sorted(groups_players.get(group, set())),
                    group_games, round_no,
                    withdrawn=set(self._withdrawn_in(station_id, group)),
                    previous_pairs=previous_pairs, colors=colors)
                for g in generated:
                    g["group"] = group
                    # 编号加组别前缀，避免同站三组对局号相撞
                    g["game_id"] = f"{group}-{g['game_id']}"
                published.extend(generated)
        else:
            existing_ids = {g["game_id"]
                            for rnd_games
                            in self.pairings.get(station_id, {}).values()
                            for g in rnd_games}
            published = []
            for g in games:
                clean = self._validate_pairing(book, station_id, round_no, g,
                                               groups_players)
                if clean["game_id"] in existing_ids:
                    raise EventError(
                        f"对局编号在本站不唯一: {clean['game_id']}")
                existing_ids.add(clean["game_id"])
                published.append(clean)
        payload = {"station_id": station_id, "round": round_no,
                   "games": published, "pairing_policy": book["pairing_policy"],
                   "rules_version": book["rules_version"]}
        return self.store.append("PairingsPublished", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="配对冻结")

    def _validate_pairing(self, book, station_id, round_no, g, groups_players):
        for key in ("game_id", "group", "black"):
            if not g.get(key):
                raise EventError(f"配对缺少字段: {key}")
        if g["group"] not in book.group_names:
            raise EventError(f"配对组别未冻结: {g['group']}")
        white = g.get("white")
        is_bye = bool(g.get("is_bye"))
        members = groups_players.get(g["group"], set())
        if g["black"] not in members:
            raise EventError("配对中的黑方不是本组报名棋手")
        if not is_bye and (white is None or white not in members):
            raise EventError("配对中的白方不是本组报名棋手")
        if not is_bye and g["black"] == white:
            raise EventError("棋手不能与自己对阵")
        return {"game_id": str(g["game_id"]), "group": g["group"],
                "round": round_no, "black": g["black"], "white": white,
                "is_bye": is_bye}

    def submit_result(self, station_id, game_id, *, result_type, outcome,
                      record_hash, record_kind, signature, note=None,
                      submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None and prior.type == "GameResultSubmitted" and \
                prior.payload.get("station_id") == station_id and \
                prior.payload.get("game_id") == game_id:
            # 任何阶段重投同一轮成绩：直接返回首次事实，不做任何校验/变更
            return prior
        book = self._require_open_station(station_id)
        if self.finalized.get(station_id):
            raise EventError("本站已终赛；更正成绩请走纠错/申诉事件")
        self._require_result_mutable(station_id)
        game = self._find_game(station_id, game_id)
        if game.get("is_bye"):
            raise EventError("轮空局无需提交成绩")
        self._validate_result(book, game, result_type, outcome,
                              record_hash, record_kind, signature)
        rnd = game["round"]
        published_rounds = self.pairings.get(station_id, {})
        if any(r > rnd for r in published_rounds):
            raise EventError(
                f"第 {rnd} 轮已关门，迟到成绩须使用迟到结果通道（保留来源与接收人）")
        if game_id in self.effective.get(station_id, {}):
            raise EventError("该对局已有成绩；重投请复用原幂等键，"
                             "更正请走纠错事件")
        payload = self._result_payload(station_id, game, result_type, outcome,
                                       record_hash, record_kind, signature, note)
        return self.store.append("GameResultSubmitted", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="临场成绩")

    def record_late_result(self, station_id, game_id, *, result_type, outcome,
                           record_hash, record_kind, signature, reason,
                           accepted_by, submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        book = self._require_open_station(station_id)
        self._require_result_mutable(station_id)
        game = self._find_game(station_id, game_id)
        if game.get("is_bye"):
            raise EventError("轮空局无需提交成绩")
        self._validate_result(book, game, result_type, outcome,
                              record_hash, record_kind, signature)
        if game_id in self.effective.get(station_id, {}):
            raise EventError("该对局已有有效成绩，迟到通道仅用于补录，"
                             "更改请走纠错事件")
        payload = self._result_payload(station_id, game, result_type, outcome,
                                       record_hash, record_kind, signature, None)
        payload.update({"late": True, "reason": reason,
                        "accepted_by": accepted_by})
        return self.store.append("LateResultRecorded", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="迟到补录")

    def correct_result(self, station_id, game_id, *, result_type, outcome,
                       record_hash, record_kind, signature, reason,
                       submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        self._require_open_station(station_id)
        self._require_result_mutable(station_id)
        game = self._find_game(station_id, game_id)
        if game.get("is_bye"):
            raise EventError("轮空局不能纠错")
        book = self.stations[station_id]
        self._validate_result(book, game, result_type, outcome,
                              record_hash, record_kind, signature)
        if not reason or not str(reason).strip():
            raise EventError("纠错必须附书面原因")
        state = self.effective.get(station_id, {}).get(game_id)
        payload = self._result_payload(station_id, game, result_type, outcome,
                                       record_hash, record_kind, signature, None)
        payload["reason"] = reason
        payload["supersedes_seq"] = state["current"]["seq"] if state else None
        return self.store.append("ResultCorrected", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="录入纠错")

    def rule_appeal(self, station_id, game_id, *, appeal_id, action,
                    basis, effective_outcome=None, result_type=None,
                    evidence_hashes=(), ruled_by, submitted_by,
                    idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        self._require_open_station(station_id)
        self._require_result_mutable(station_id)
        game = self._find_game(station_id, game_id)
        if action not in ("维持", "改判", "重赛"):
            raise EventError("申诉裁决动作必须是 维持/改判/重赛")
        if not basis or not str(basis).strip():
            raise EventError("裁决必须写明依据")
        if action == "改判":
            if not effective_outcome:
                raise EventError("改判必须给出 effective_outcome")
            book = self.stations[station_id]
            valid = OUTCOME_BY_TYPE.get(result_type or "胜负", set())
            if effective_outcome not in (
                    OUTCOME_BY_TYPE["胜负"] | OUTCOME_BY_TYPE["弃权"]):
                raise EventError("改判结果不在认可取值内")
        if game_id not in self.effective.get(station_id, {}):
            raise EventError("尚无成绩的对局不能裁决")
        payload = {"station_id": station_id, "game_id": game_id,
                   "group": game["group"], "round": game["round"],
                   "appeal_id": appeal_id, "action": action, "basis": basis,
                   "effective_outcome": effective_outcome,
                   "result_type": result_type,
                   "evidence_hashes": list(evidence_hashes),
                   "ruled_by": ruled_by}
        return self.store.append("AppealRuled", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="申诉裁决")

    def withdraw(self, station_id, person_id, group, *, reason,
                 from_round=None, submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        book = self._require_open_station(station_id)
        if self.finalized.get(station_id):
            raise EventError("本站已终赛，退赛须由总决赛按放弃资格处理")
        root = self.identity.resolve(person_id)
        members = self._root_entrants(station_id).get(group, set())
        if root not in members:
            raise EventError("该棋手不在本组报名名单中")
        if root in self.withdrawn.get(station_id, {}):
            raise EventError("该棋手已退赛")
        if from_round is None:
            published = self.pairings.get(station_id, {})
            from_round = max(published) + 1 if published else 1
        if not 1 <= from_round <= book["rounds"] + 1:
            raise EventError("退赛生效轮次非法")
        payload = {"station_id": station_id, "person_id": root,
                   "group": group, "reason": reason,
                   "from_round": from_round}
        return self.store.append("PlayerWithdrawn", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="退赛")

    def finalize(self, station_id, *, submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        book = self._require_open_station(station_id)
        if self.finalized.get(station_id):
            raise EventError("本站已经终赛")
        published = self.pairings.get(station_id, {})
        missing = [r for r in range(1, book["rounds"] + 1) if r not in published]
        if missing:
            raise EventError(f"尚有轮次未冻结配对: {missing}")
        unresolved = []
        for rnd in range(1, book["rounds"] + 1):
            for g in published[rnd]:
                if g.get("is_bye"):
                    continue
                state = self.effective.get(station_id, {}).get(g["game_id"])
                if state is None:
                    unresolved.append(g["game_id"])
        if unresolved:
            raise EventError(f"尚有对局无有效成绩: {unresolved}")
        payload = {"station_id": station_id,
                   "rules_version": book["rules_version"],
                   "rounds": book["rounds"]}
        return self.store.append("StationFinalized", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="赛区终赛")

    # ================= 查询/投影 =================
    def _root_entrants(self, station_id):
        out = {}
        for group, pids in self.entrants.get(station_id, {}).items():
            roots = set()
            for pid in pids:
                try:
                    roots.add(self.identity.resolve(pid))
                except EventError:
                    roots.add(pid)
            out[group] = roots
        return out

    def _withdrawn_in(self, station_id, group):
        for pid, info in self.withdrawn.get(station_id, {}).items():
            if info["group"] == group:
                yield pid

    def _find_game(self, station_id, game_id):
        for games in self.pairings.get(station_id, {}).values():
            for g in games:
                if g["game_id"] == game_id:
                    return g
        raise EventError(f"配对表中找不到对局: {game_id}")

    def _pairing_history(self, group_games):
        """从已赛对局提取 (frozenset 对阵) 与执白计数，供瑞士制回避重对。"""
        used, white_count = set(), {}
        for g in group_games:
            if g.get("is_bye"):
                continue
            b, w = g.get("black"), g.get("white")
            if b and w:
                used.add((b, w) if b < w else (w, b))
                white_count[w] = white_count.get(w, 0) + 1
        return used, white_count

    def _games_through(self, station_id, through_round):
        """当前效力对局清单（供破同分与配对），含退赛自动判负。"""
        result = []
        withdrawals = self.withdrawn.get(station_id, {})

        def root(pid):
            if pid is None:
                return None
            try:
                return self.identity.resolve(pid)
            except EventError:
                return pid

        for rnd, games in sorted(self.pairings.get(station_id, {}).items()):
            if through_round is not None and rnd > through_round:
                break
            for g in games:
                black, white = root(g["black"]), root(g["white"])
                state = self.effective.get(station_id, {}).get(g["game_id"])
                if g.get("is_bye"):
                    result.append({"round": rnd, "game_id": g["game_id"],
                                   "black": black, "white": None,
                                   "outcome": None, "is_bye": True,
                                   "group": g["group"]})
                elif state is not None:
                    cur = state["current"]
                    result.append({"round": rnd, "game_id": g["game_id"],
                                   "black": black, "white": white,
                                   "outcome": cur["outcome"],
                                   "is_bye": False, "group": g["group"],
                                   "seq": cur["seq"]})
                elif black in withdrawals or white in withdrawals:
                    wb = withdrawals.get(black)
                    ww = withdrawals.get(white)
                    wb_eff = wb and rnd >= wb["from_round"]
                    ww_eff = ww and rnd >= ww["from_round"]
                    if wb_eff and ww_eff:
                        outcome = "双方负"
                    elif wb_eff:
                        outcome = "黑弃权"
                    else:
                        outcome = "白弃权"
                    result.append({"round": rnd, "game_id": g["game_id"],
                                   "black": black, "white": white,
                                   "outcome": outcome, "is_bye": False,
                                   "group": g["group"],
                                   "generated_from": "withdrawal"})
        return result

    def standings(self, station_id, group=None, *, through_round=None):
        book = self._require_open_station(station_id)
        groups = [group] if group else book.group_names
        all_games = self._games_through(station_id, through_round)
        out = {}
        for grp in groups:
            players = sorted(self._root_entrants(station_id).get(grp, set()))
            games = [g for g in all_games if g["group"] == grp]
            wd = set(self._withdrawn_in(station_id, grp))
            out[grp] = compute_standings(
                book, grp, players, games, withdrawn=wd,
                through_round=through_round)
        return out

    def effective_result(self, station_id, game_id):
        state = self.effective.get(station_id, {}).get(game_id)
        if state is None:
            raise EventError("该对局暂无成绩")
        return {
            "game_id": game_id, "group": state["group"],
            "round": state["round"], "black": state["black"],
            "white": state["white"], "current": dict(state["current"]),
            "history": list(state["history"]),
            "appeal": dict(state["appeal"]) if state.get("appeal") else None,
        }

    # ================= 校验工具 =================
    def _result_payload(self, station_id, game, result_type, outcome,
                        record_hash, record_kind, signature, note):
        return {
            "station_id": station_id, "game_id": game["game_id"],
            "group": game["group"], "round": game["round"],
            "black": game["black"], "white": game["white"],
            "result_type": result_type, "outcome": outcome,
            "record_hash": record_hash, "record_kind": record_kind,
            "signature": {"signee": signature["signee"],
                          "role": signature["role"],
                          "source": signature["source"],
                          "signed_at": signature.get("signed_at")},
            "note": note,
        }

    def _validate_result(self, book, game, result_type, outcome,
                         record_hash, record_kind, signature):
        if result_type not in VALID_RESULT_TYPES:
            raise EventError(f"result_type 必须是 {VALID_RESULT_TYPES}")
        if outcome not in OUTCOME_BY_TYPE[result_type]:
            raise EventError(
                f"{result_type} 成绩的结果取值只能是 "
                f"{sorted(OUTCOME_BY_TYPE[result_type])}")
        if not record_hash or not HEX64.match(str(record_hash)):
            raise EventError("必须提交棋谱/弃权单的 SHA-256 哈希（64 位十六进制）")
        if not record_kind:
            raise EventError("缺少记录类型 record_kind（棋谱/弃权单）")
        sig = signature or {}
        for field in ("signee", "role", "source"):
            if not sig.get(field):
                raise EventError(f"签字来源缺少字段: {field}")
