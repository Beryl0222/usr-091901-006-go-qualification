"""总决赛核算：晋级投影、跨赛区去重、退赛递补、通知出站、
名单锁定与可审计解释。

核算规则（赛区优先序、名额处理原则）同样在开赛前由总决赛
组委会冻结；所有名额变动都落为事件，重算只发生在受影响的
站点/组别，通知也只发给相关人员。锁定后的名单是不可变快照，
解释固定引用比赛时规则版本与事件哈希链。
"""

import hashlib
import json

from events import EventError


class FederationService:
    def __init__(self, store, identity, stations):
        self.store = store
        self.identity = identity
        self.stations = stations
        self.fed_rules = None
        self.projections = {}      # station -> group -> projection
        self.seats = {}            # station -> group -> 席位状态
        self.outbox = []
        self.lock_event = None
        store.subscribe(self._apply)

    # ================= 事件折叠 =================
    def _apply(self, event):
        p = event.payload
        t = event.type
        if t == "FederationRulesFrozen":
            self.fed_rules = dict(p)
        elif t == "PromotionProjected":
            self.projections.setdefault(p["station_id"], {})[p["group"]] = {
                "ordered": p["ordered_person_ids"],
                "rows": p["rows"],
                "quota": p["quota"],
                "rules_version": p["rules_version"],
                "fingerprint": p["fingerprint"],
                "seq": event.seq,
            }
            self.seats.setdefault(p["station_id"], {})[p["group"]] = {
                "occupied": {},   # pid -> {mode, rank, seq, replaces}
                "released": {},   # pid -> {reason, seq}
                "vacancies": [],
            }
            self._reconcile(p["station_id"], p["group"],
                            cause="projection", notify=False)
        elif t == "PromotionRevoked":
            st, grp, pid = p["station_id"], p["group"], p["person_id"]
            seats = self.seats[st][grp]
            seats["occupied"].pop(pid, None)
            seats["released"][pid] = {"reason": p["reason"],
                                      "seq": event.seq,
                                      "mode": "revoke"}
            self._reconcile(st, grp, cause="revoke",
                            replaced_pid=pid)
        elif t == "PromotionDeduplicated":
            st, grp, pid = p["released_station"], p["group"], p["person_id"]
            seats = self.seats[st][grp]
            seats["occupied"].pop(pid, None)
            seats["released"][pid] = {
                "reason": f"跨赛区重复，名额保留给 {p['kept_station']}",
                "seq": event.seq, "mode": "dedup",
                "kept_station": p["kept_station"], "basis": p["basis"]}
            self._reconcile(st, grp, cause="dedup", replaced_pid=pid)
        elif t == "PromotionAdvanced":
            seats = self.seats[p["station_id"]][p["group"]]
            seats["occupied"][p["person_id"]] = {
                "mode": "advanced", "rank": p["from_rank"],
                "seq": event.seq, "replaces": p.get("replaces_person_id"),
                "reason": p["reason"]}
        elif t == "QuotaVacancy":
            seats = self.seats[p["station_id"]][p["group"]]
            key = (p["rank"], p["reason"])
            if not any(v["rank"] == p["rank"] for v in seats["vacancies"]):
                seats["vacancies"].append(
                    {"rank": p["rank"], "reason": p["reason"],
                     "seq": event.seq})
        elif t == "NotificationQueued":
            self.outbox.append(dict(p, seq=event.seq))
        elif t == "FinalRosterLocked":
            self.lock_event = event

    def _reconcile(self, station_id, group, *, cause, replaced_pid=None,
                   notify=True):
        """按本站名次表把席位补齐到 quota；只产出差异事件/通知。

        注意：折叠期间本函数只更新纯内存投影（Advanced 等事件由
        命令侧追加后再 fold），因此 notify=False 用于重建路径。
        """
        proj = self.projections[station_id][group]
        seats = self.seats[station_id][group]
        released = seats["released"]
        eligible = [pid for pid in proj["ordered"]
                    if pid not in released]
        target = eligible[:proj["quota"]]
        if not notify:
            # 重放：席位以后续事件为准，此处仅建立基线
            for pid in target:
                seats["occupied"].setdefault(pid, {
                    "mode": "direct",
                    "rank": proj["ordered"].index(pid) + 1,
                    "seq": proj["seq"], "replaces": None})
            return target
        return target

    # ================= 命令 =================
    def freeze_federation_rules(self, *, expected_stations, zone_priority,
                                submitted_by, idempotency_key=None,
                                dedup_policy="zone_priority_then_rank"):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        if self.fed_rules is not None:
            raise EventError("总决赛规则已经冻结")
        if not expected_stations or len(set(expected_stations)) != len(expected_stations):
            raise EventError("必须给出互不重复的分站清单")
        if list(zone_priority) != list(expected_stations):
            raise EventError("赛区优先序必须覆盖且仅覆盖全部参赛分站")
        payload = {"expected_stations": list(expected_stations),
                   "zone_priority": list(zone_priority),
                   "dedup_policy": dedup_policy,
                   "rules_version": "finals-1.0"}
        return self.store.append("FederationRulesFrozen", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source="总决赛冻结")

    def project_station(self, station_id, *, submitted_by,
                        idempotency_key=None):
        """终赛后把该站各组晋级候选投影为不可变事实，并处理跨区重复。"""
        if not self.stations.finalized.get(station_id):
            raise EventError("该站尚未终赛，不能投影晋级名单")
        results = []
        book = self.stations.stations[station_id]
        standings = self.stations.standings(station_id)
        all_prior = self.projections.get(station_id, {})
        for group in book.group_names:
            ranked = standings[group]
            rows = [{
                "person_id": r["person_id"], "rank": r["rank"],
                "score": r["score"], "eligible": r["eligible"],
                "metrics": r["metrics"], "history": r["history"],
            } for r in ranked]
            ordered = [r["person_id"] for r in ranked if r["eligible"]]
            fingerprint = self._fingerprint(
                station_id, group, rows, book["rules_version"])
            prior = all_prior.get(group)
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise EventError(
                        f"{station_id}/{group} 已终赛但名次指纹发生变化，"
                        "这违背终赛不可变原则")
                results.append(("unchanged", group))
                continue
            quota = book.quota(group)
            payload = {"station_id": station_id, "group": group,
                       "ordered_person_ids": ordered, "rows": rows,
                       "quota": quota,
                       "rules_version": book["rules_version"],
                       "tiebreak": book["tiebreak"],
                       "fingerprint": fingerprint}
            self.store.append("PromotionProjected", payload,
                              idempotency_key=(f"{idempotency_key}:{group}"
                                               if idempotency_key else None),
                              submitted_by=submitted_by, source="晋级投影")
            results.append(("projected", group))
        return results

    def revoke_qualification(self, station_id, group, person_id, *,
                             reason, submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        if self.lock_event is not None:
            raise EventError("总决赛名单已锁定，不能再撤销资格")
        self._require_seat(station_id, group)
        root = self.identity.resolve(person_id)
        seats = self.seats[station_id][group]
        if root not in seats["occupied"]:
            raise EventError("该棋手并未持有此席位")
        if not reason or not str(reason).strip():
            raise EventError("退赛/放弃资格必须登记原因")
        event = self.store.append(
            "PromotionRevoked",
            {"station_id": station_id, "group": group, "person_id": root,
             "reason": reason},
            idempotency_key=idempotency_key,
            submitted_by=submitted_by, source="资格退出")
        self._fill_gap(station_id, group, root, reason,
                       cause_events=[event])
        return event

    def resolve_duplicates(self, *, submitted_by):
        """同一棋手跨区占用多个席位时，只保留优先赛区，其余站内递补。"""
        if self.lock_event is not None:
            raise EventError("名单锁定后不能再调整席位")
        self._require_fed_rules()
        changes = []
        # 递补进来的人本身也可能在别站占位，循环直到没有重复
        while True:
            holders = {}   # person root -> [(station, group)]
            for st, groups in self.seats.items():
                for grp, seats in groups.items():
                    for pid in seats["occupied"]:
                        holders.setdefault(pid, []).append((st, grp))
            duplicated = {pid: claims for pid, claims in holders.items()
                          if len(claims) > 1}
            if not duplicated:
                break
            progress = False
            for pid, claims in duplicated.items():
                priority = self.fed_rules["zone_priority"]
                claims_sorted = sorted(
                    claims, key=lambda c: (priority.index(c[0])
                                           if c[0] in priority else 99,
                                           self.projections[c[0]][c[1]]["seq"]))
                kept_st, kept_grp = claims_sorted[0]
                for st, grp in claims_sorted[1:]:
                    if pid in self.seats[st][grp]["released"]:
                        continue
                    basis = (f"赛区优先序 {self.fed_rules['zone_priority']}；"
                             f"保留 {kept_st}，策略 {self.fed_rules['dedup_policy']}")
                    event = self.store.append(
                        "PromotionDeduplicated",
                        {"person_id": pid, "kept_station": kept_st,
                         "kept_group": kept_grp, "released_station": st,
                         "group": grp, "basis": basis},
                        submitted_by=submitted_by, source="跨区去重")
                    self._fill_gap(st, grp, pid,
                                   f"跨赛区重复晋级去重（保留 {kept_st}）",
                                   cause_events=[event])
                    changes.append(event)
                    progress = True
            if not progress:
                break
        return changes

    def _fill_gap(self, station_id, group, released_pid, reason,
                  *, cause_events):
        """根据释放事件重算一组席位：候补递补或登记空缺，并发通知。

        在事件已追加（fold 完 released）之后调用。
        """
        proj = self.projections[station_id][group]
        seats = self.seats[station_id][group]
        target = [pid for pid in proj["ordered"]
                  if pid not in seats["released"]][:proj["quota"]]
        for rank, pid in enumerate(target, start=1):
            if pid in seats["occupied"]:
                continue
            ev = self.store.append(
                "PromotionAdvanced",
                {"station_id": station_id, "group": group,
                 "person_id": pid, "from_rank": rank,
                 "replaces_person_id": released_pid,
                 "reason": reason,
                 "rules_version": proj["rules_version"]},
                submitted_by="总决赛组委会", source="名额递补")
            self._notify(
                station_id, group, "promotion_advanced",
                [{"kind": "player_station", "person_id": pid,
                  "station_id": station_id},
                 {"kind": "station_admin", "station_id": station_id}],
                {"released_person_id": released_pid, "from_rank": rank,
                 "reason": reason},
                related_event=ev.seq)
        occupied_ranks = len(seats["occupied"])
        if occupied_ranks < proj["quota"]:
            for rank in range(occupied_ranks + 1, proj["quota"] + 1):
                if any(v["rank"] == rank for v in seats["vacancies"]):
                    continue
                ev = self.store.append(
                    "QuotaVacancy",
                    {"station_id": station_id, "group": group,
                     "rank": rank, "reason": f"候补名单不足：{reason}"},
                    submitted_by="总决赛组委会", source="名额空缺")
                self._notify(
                    station_id, group, "quota_vacancy",
                    [{"kind": "federation_reviewers"}],
                    {"rank": rank, "reason": reason},
                    related_event=ev.seq)
        # 被释放者本人通知
        self._notify(
            station_id, group, "promotion_released",
            [{"kind": "player_station", "person_id": released_pid,
              "station_id": station_id}],
            {"reason": reason},
            related_event=cause_events[0].seq)

    def lock_final_roster(self, *, submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        self._require_fed_rules()
        if self.lock_event is not None:
            raise EventError("总决赛名单已经锁定")
        expected = set(self.fed_rules["expected_stations"])
        seen = set(self.projections)
        missing = expected - seen
        if missing:
            raise EventError(f"以下分站尚未完成晋级投影: {sorted(missing)}")
        extra = seen - expected
        if extra:
            raise EventError(f"出现未登记的分站成绩: {sorted(extra)}")

        roster = {}
        total = 0
        for st in self.fed_rules["zone_priority"]:
            for grp, proj in self.projections[st].items():
                seats = self.seats[st][grp]
                if seats["vacancies"]:
                    raise EventError(
                        f"{st}/{grp} 仍有名额空缺，不能锁定")
                roster.setdefault(grp, []).extend(
                    {"person_id": pid, "station_id": st,
                     "mode": info["mode"], "rank": info["rank"],
                     "replaces": info["replaces"],
                     "reason": info.get("reason"),
                     "rules_version": proj["rules_version"],
                     "projection_seq": proj["seq"]}
                    for pid, info in seats["occupied"].items())
                total += len(seats["occupied"])
        roots = [row["person_id"] for rows in roster.values() for row in rows]
        if len(roots) != len(set(roots)):
            dupes = sorted({pid for pid in roots if roots.count(pid) > 1})
            raise EventError(
                f"锁定校验失败：仍有一人占用多个晋级位 {dupes}，"
                "请先执行跨区去重")
        snapshot = {"groups": roster,
                    "rules": self.fed_rules,
                    "tail_hash": self.store.tail_hash}
        snapshot["fingerprint"] = self._fingerprint("finals", "roster",
                                                    snapshot, "finals-1.0")
        event = self.store.append("FinalRosterLocked", snapshot,
                                  idempotency_key=idempotency_key,
                                  submitted_by=submitted_by, source="名单锁定")
        self._notify_all("roster_locked", {"total": total})
        return event

    # ================= 通知 =================
    def _notify(self, station_id, group, template, recipients, vars_,
                related_event=None):
        """通知只携带受影响站点/组别；赛区收件人用通行码寻址。"""
        targets = []
        for r in recipients:
            if r["kind"] == "player_station":
                targets.append({
                    "channel": "station",
                    "station_id": r["station_id"],
                    "pass": self.identity.passcode(r["person_id"],
                                                   r["station_id"]),
                    "person_id": r["person_id"],  # 仅组委会侧可见
                })
            elif r["kind"] == "station_admin":
                targets.append({"channel": "station_admin",
                                "station_id": r["station_id"]})
            elif r["kind"] == "federation_reviewers":
                targets.append({"channel": "federation_reviewers"})
        self.store.append(
            "NotificationQueued",
            {"scope": {"station_id": station_id, "group": group},
             "template": template, "recipients": targets,
             "vars": vars_, "related_event": related_event,
             "status": "queued"},
            submitted_by="系统", source="核算通知")

    def _notify_all(self, template, vars_):
        self.store.append(
            "NotificationQueued",
            {"scope": {"station_id": None, "group": None},
             "template": template, "recipients": [
                 {"channel": "federation_organizers"}],
             "vars": vars_, "status": "queued"},
            submitted_by="系统", source="核算通知")

    # ================= 查询 =================
    def _require_fed_rules(self):
        if self.fed_rules is None:
            raise EventError("总决赛规则尚未冻结")

    def _require_seat(self, station_id, group):
        if station_id not in self.projections or \
                group not in self.projections[station_id]:
            raise EventError(f"{station_id}/{group} 尚未投影晋级名单")

    def promotion_status(self, station_id, group):
        self._require_seat(station_id, group)
        proj = self.projections[station_id][group]
        seats = self.seats[station_id][group]
        return {
            "station_id": station_id, "group": group,
            "quota": proj["quota"], "rules_version": proj["rules_version"],
            "fingerprint": proj["fingerprint"],
            "ordered": proj["ordered"],
            "occupied": {pid: dict(info)
                         for pid, info in seats["occupied"].items()},
            "released": {pid: dict(info)
                         for pid, info in seats["released"].items()},
            "vacancies": list(seats["vacancies"]),
            "locked": self.lock_event is not None,
        }

    def final_roster(self):
        if self.lock_event is None:
            raise EventError("总决赛名单尚未锁定")
        snap = dict(self.lock_event.payload)
        return snap

    def notifications(self, *, station_id=None, group=None, channel=None):
        out = []
        for n in self.outbox:
            scope = n["scope"]
            if station_id and scope["station_id"] not in (station_id, None):
                continue
            if group and scope["group"] not in (group, None):
                continue
            if channel:
                recips = [r for r in n["recipients"]
                          if r["channel"] == channel]
                if not recips:
                    continue
                n = dict(n, recipients=recips)
            out.append(n)
        return out

    # ================= 解释 =================
    def explain(self, person_id):
        """解释一位棋手：经过哪些对局、用哪版规则、为何排在临界名次之前。"""
        root = self.identity.resolve(person_id)
        person = self.identity.person(root)
        participations = []
        seat_found = None
        for st, groups in self.projections.items():
            for grp, proj in groups.items():
                if root not in proj["ordered"]:
                    continue
                row = next(r for r in proj["rows"]
                           if r["person_id"] == root)
                games = self._game_trace(st, grp, root)
                boundary = self._boundary(st, grp, proj, row)
                seats = self.seats[st][grp]
                seat = seats["occupied"].get(root)
                released = seats["released"].get(root)
                if seat:
                    seat_found = (st, grp)
                participations.append({
                    "station_id": st, "group": grp,
                    "rules_version": proj["rules_version"],
                    "tiebreak_order": self.stations.stations[st]["tiebreak"],
                    "rank": row["rank"], "score": row["score"],
                    "metrics": row["metrics"],
                    "history": row["history"],
                    "games": games,
                    "boundary": boundary,
                    "seat": (dict(seat) if seat else None),
                    "released": (dict(released) if released else None),
                    "projection_seq": proj["seq"],
                    "projection_fingerprint": proj["fingerprint"],
                })
        if not participations:
            raise EventError("该棋手没有任何已投影分站的参赛记录")
        locked = None
        if self.lock_event is not None:
            locked = {"seq": self.lock_event.seq,
                      "fingerprint": self.lock_event.payload["fingerprint"],
                      "tail_hash": self.lock_event.payload["tail_hash"]}
        return {
            "person_id": root,
            "name": person["name"],
            "aliases": sorted(a for a in person["aliases"]
                              if not a.startswith("证件撞号:")),
            "participations": participations,
            "qualification": self._qualification_text(
                participations, seat_found),
            "roster_lock": locked,
            "tail_hash": self.store.tail_hash,
        }

    def _game_trace(self, station_id, group, root):
        """逐轮对局及其完整事件链（提交/迟到/纠错/裁决），含棋谱哈希。"""
        trace = []
        pairing = self.stations.pairings.get(station_id, {})
        for rnd in sorted(pairing):
            for g in pairing[rnd]:
                if g["group"] != group:
                    continue
                black = self.identity.resolve(g["black"]) \
                    if g.get("black") else None
                white = self.identity.resolve(g["white"]) \
                    if g.get("white") else None
                if black != root and white != root:
                    continue
                entry = {"round": rnd, "game_id": g["game_id"],
                         "color": ("黑" if black == root else
                                   ("白" if white == root else None)),
                         "opponent": white if black == root else black,
                         "is_bye": bool(g.get("is_bye"))}
                state = self.stations.effective.get(station_id, {}).get(
                    g["game_id"])
                if state is not None:
                    cur = state["current"]
                    entry["current_outcome"] = cur["outcome"]
                    entry["result_type"] = cur["result_type"]
                    entry["record_hash"] = cur["record_hash"]
                    entry["record_kind"] = cur["record_kind"]
                    entry["signature"] = cur["signature"]
                    entry["event_chain"] = self._chain_events(
                        station_id, g["game_id"])
                    if state.get("appeal"):
                        entry["appeal"] = state["appeal"]
                trace.append(entry)
        return trace

    def _chain_events(self, station_id, game_id):
        chain = []
        for e in self.store.events():
            if e.type in ("GameResultSubmitted", "LateResultRecorded",
                          "ResultCorrected", "AppealRuled") and \
                    e.payload.get("station_id") == station_id and \
                    e.payload.get("game_id") == game_id:
                chain.append({"seq": e.seq, "type": e.type, "ts": e.ts,
                              "submitted_by": e.submitted_by,
                              "source": e.source,
                              "hash": e.hash,
                              "outcome": e.payload.get("outcome")
                              or e.payload.get("effective_outcome")})
        return chain

    def _boundary(self, station_id, group, proj, row):
        """临界名次解释：与紧后一名比较，指出破同分在哪一级分出先后。"""
        rows = [r for r in proj["rows"] if r["eligible"]]
        idx = next(i for i, r in enumerate(rows)
                   if r["person_id"] == row["person_id"])
        if idx + 1 >= len(rows):
            return {"relation": "last", "next_person_id": None}
        nxt = rows[idx + 1]
        if row["score"] != nxt["score"]:
            return {"relation": "ahead", "next_person_id": nxt["person_id"],
                    "decided_by": "积分",
                    "detail": {"score": [row["score"], nxt["score"]]}}
        steps = self.stations.stations[station_id]["tiebreak"]
        decided = None
        for step in steps:
            if step == "直接对局":
                a, b = self._head_to_head_pair(station_id, group,
                                               row["person_id"],
                                               nxt["person_id"])
                if a != b:
                    decided = {"decided_by": "直接对局",
                               "detail": {"直接对局(前者,后者)": [a, b]}}
                    break
                continue
            if step == "抽签":
                a, b = row["metrics"][step], nxt["metrics"][step]
                if a != b:
                    decided = {"decided_by": "抽签（种子冻结、结果可复现）",
                               "detail": {"抽签键": [a[:16], b[:16]]}}
                    break
                continue
            a = row["metrics"][step]
            b = nxt["metrics"][step]
            if a != b:
                decided = {"decided_by": step,
                           "detail": {step: [a, b]}}
                break
        info = {"relation": "ahead", "next_person_id": nxt["person_id"]}
        if decided:
            info.update(decided)
        return info

    def _head_to_head_pair(self, station_id, group, pid_a, pid_b):
        """两名棋手直接对弈的累计小分（胜 1 / 和 0.5 / 负 0）。"""
        from standings import score_outcome
        games = self.stations._games_through(station_id, None)
        sa = sb = 0.0
        for g in games:
            if g["group"] != group or g.get("is_bye"):
                continue
            if {g.get("black"), g.get("white")} != {pid_a, pid_b}:
                continue
            x, y = score_outcome(g["outcome"])
            if g["black"] == pid_a:
                sa += x
                sb += y
            else:
                sa += y
                sb += x
        return sa, sb

    def _qualification_text(self, participations, seat_found):
        if seat_found is None:
            for p in participations:
                if p["released"]:
                    return (f"曾在 {p['station_id']}/{p['group']} 获得席位，"
                            f"后因“{p['released']['reason']}”释放")
            return "未获得总决赛席位"
        st, grp = seat_found
        part = next(p for p in participations
                    if p["station_id"] == st and p["group"] == grp)
        seat = part["seat"]
        if seat["mode"] == "direct":
            return (f"在 {st}/{grp} 以第 {seat['rank']} 名直接晋级"
                    f"（规则版本 {part['rules_version']}）")
        return (f"在 {st}/{grp} 从第 {seat['rank']} 名递补晋级，"
                f"原因：{seat['reason']}（规则版本 {part['rules_version']}）")

    @staticmethod
    def _fingerprint(station_id, group, rows, rules_version):
        body = json.dumps(
            {"station_id": station_id, "group": group,
             "rules_version": rules_version, "rows": rows},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str).encode("utf-8")
        return hashlib.sha256(body).hexdigest()
