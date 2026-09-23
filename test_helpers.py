"""赛事资格与核算后端测试夹具：构造一个四赛区可复用的领域世界。"""

import hashlib

from app import Application

SHA = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()


def signature(signee="赵裁判", role="裁判", source="现场签字单"):
    return {"signee": signee, "role": role, "source": source}


DEFAULT_TIEBREAK = ["对手分总和", "累进分", "直接对局", "抽签"]


def freeze_station(app, station_id, *, groups=None, quota=2, rounds=3,
                   tiebreak=None, rules_version="rules-2026-v1",
                   submitted_by="赛区管理员"):
    groups = groups or [{"name": "名手组", "quota": quota}]
    return app.stations.freeze(
        {"station_id": station_id, "rules_version": rules_version,
         "groups": groups, "rounds": rounds,
         "tiebreak": tiebreak or DEFAULT_TIEBREAK},
        submitted_by=submitted_by)


def register(app, station_id, name, doc, *, group="名手组", gender=None,
             birth_date=None, seq=None, submitted_by="赛区管理员"):
    key = f"reg-{station_id}-{group}-{doc}-{seq}" if seq else \
        f"reg-{station_id}-{group}-{doc}"
    ev = app.identity.register(
        station_id=station_id, group=group, name=name, gender=gender,
        birth_date=birth_date,
        documents=[{"type": "身份证", "number": doc}],
        submitted_by=submitted_by, idempotency_key=key)
    return ev.payload["person_id"]


def play_round(app, station_id, round_no, *, decide=None,
               signee="赵裁判", idem_prefix=""):
    """发布并完成一轮。decide(game)->outcome，默认黑胜。"""
    ev = app.stations.publish_pairings(
        station_id, round_no, submitted_by="裁判长",
        idempotency_key=f"pair-{station_id}-{round_no}")
    results = []
    for g in ev.payload["games"]:
        if g["is_bye"]:
            results.append((g, None))
            continue
        outcome = decide(g) if decide else "黑胜"
        key = f"{idem_prefix}res-{station_id}-{g['game_id']}-r{round_no}"
        app.stations.submit_result(
            station_id, g["game_id"], result_type="胜负", outcome=outcome,
            record_hash=SHA(f"{station_id}-{g['game_id']}-r{round_no}"),
            record_kind="棋谱", signature=signature(signee),
            submitted_by=signee, idempotency_key=key)
        results.append((g, outcome))
    return ev.payload["games"], results


def play_all_rounds(app, station_id, rounds, *, decide=None):
    for r in range(1, rounds + 1):
        play_round(app, station_id, r, decide=decide)


def finish_station(app, station_id, *, fed_stations=None):
    app.stations.finalize(station_id, submitted_by="赛区管理员",
                          idempotency_key=f"final-{station_id}")
    if fed_stations is not None:
        app.federation.project_station(
            station_id, submitted_by="总决赛组委会",
            idempotency_key=f"proj-{station_id}")


class World:
    """四赛区 × 名手/名师/名人三组的简化世界，供跨区测试使用。"""

    STATIONS = ["east", "west", "south", "north"]

    def __init__(self, *, store_path=None, quota=1, rounds=1):
        self.app = Application(store_path=store_path)
        self.quota = quota
        self.rounds = rounds

    def setup_station(self, station_id, players, *, group="名手组",
                      decide=None):
        freeze_station(self.app, station_id,
                       groups=[{"name": group, "quota": self.quota}],
                       rounds=self.rounds)
        person_ids = [register(self.app, station_id, name, doc, group=group,
                               seq=idx)
                      for idx, (name, doc) in enumerate(players)]
        self._person_ids = person_ids

        def wrapped(g):
            if decide is not None:
                idx_b = person_ids.index(g["black"]) if g["black"] in person_ids else None
                idx_w = person_ids.index(g["white"]) if g["white"] in person_ids else None
                return decide(g, idx_b, idx_w)
            return "黑胜"

        play_all_rounds(self.app, station_id, self.rounds, decide=wrapped)
        self.app.stations.finalize(
            station_id, submitted_by="赛区管理员",
            idempotency_key=f"final-{station_id}-{group}")
        return person_ids


def winner_outcome(game, winner_pid):
    """生成“指定棋手必胜”的 outcome。"""
    return "黑胜" if game["black"] == winner_pid else "白胜"
