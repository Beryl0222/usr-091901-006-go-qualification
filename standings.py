"""破同分计算与瑞士制配对。

破同分次序来自赛前冻结的 RuleBook，逐级在“仍未分出先后的棋手
簇”内部切分；最后一环固定为可复现抽签（种子在冻结时固定），
因此任意时刻重算都得到同一名次，并能逐项解释临界名次。
"""

import hashlib

from events import EventError

WIN, DRAW, LOSS = 1.0, 0.5, 0.0


def score_outcome(outcome):
    """返回 (黑方得分, 白方得分)；无白方（轮空）时由调用方处理。"""
    table = {
        "黑胜": (WIN, LOSS),
        "白胜": (LOSS, WIN),
        "和棋": (DRAW, DRAW),
        "黑弃权": (LOSS, WIN),    # 黑方弃权，白方胜
        "白弃权": (WIN, LOSS),
        "双方负": (LOSS, LOSS),
    }
    if outcome not in table:
        raise EventError(f"未知对局结果: {outcome}")
    return table[outcome]


def _draw_key(seed, group, pid):
    raw = f"{seed}|{group}|{pid}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_standings(rulebook, group, players, games, *, withdrawn=None,
                      through_round=None):
    """依据冻结规则计算名次。

    games: 已折算为当前效力的对局列表，字段：
      round, game_id, black(pid|None), white(pid|None), outcome,
      is_bye, withdrawn_loss(bool 退赛方未对局判负)
    返回按名次排序的列表，含积分与每一级破同分的取值。
    """
    withdrawn = set(withdrawn or ())
    rounds_total = rulebook["rounds"]
    last_round = min(through_round, rounds_total) if through_round else rounds_total

    played = [g for g in games if g["round"] <= last_round]
    # 每名棋手：每轮得分、对手、胜局对手
    per = {pid: {"rounds": {}, "opponents": [], "beat": []} for pid in players}
    for g in played:
        r = g["round"]
        b, w, out = g.get("black"), g.get("white"), g["outcome"]
        if g.get("is_bye"):
            pid = b or w
            if pid in per:
                per[pid]["rounds"][r] = WIN
            continue
        sb, sw = score_outcome(out)
        if b in per:
            per[b]["rounds"][r] = sb
            if w is not None:
                per[b]["opponents"].append(w)
                if sb == WIN:
                    per[b]["beat"].append(w)
        if w in per:
            per[w]["rounds"][r] = sw
            if b is not None:
                per[w]["opponents"].append(b)
                if sw == WIN:
                    per[w]["beat"].append(b)

    # 退赛棋手未对局轮次按 0 分计入（不额外造对局）
    def score(pid):
        return sum(per[pid]["rounds"].values())

    scores = {pid: score(pid) for pid in players}

    entries = {}
    for pid in players:
        info = per[pid]
        opp_scores = [scores[o] for o in info["opponents"] if o in scores]
        sos = round(sum(opp_scores), 4)
        trimmed = round(sos - (max(opp_scores) + min(opp_scores)), 4) \
            if len(opp_scores) >= 2 else sos
        # 累进分：每轮结束后累计积分之和（轮空轮按已得满分计）
        cumulative = progressive = 0.0
        for r in range(1, last_round + 1):
            cumulative += info["rounds"].get(r, 0.0)
            progressive += cumulative
        won_opp = round(sum(scores[o] for o in info["beat"] if o in scores), 4)
        history = []
        cumulative = 0.0
        for r in range(1, last_round + 1):
            cumulative += info["rounds"].get(r, 0.0)
            history.append(round(cumulative, 1))
        entries[pid] = {
            "person_id": pid, "score": scores[pid],
            "metrics": {
                "对手分总和": sos,
                "对手分削去": trimmed,
                "累进分": round(progressive, 4),
                "所胜对手分": won_opp,
                "抽签": _draw_key(rulebook["draw_seed"], group, pid),
            },
            "opponents": list(info["opponents"]),
            "history": history,
            "withdrawn": pid in withdrawn,
        }

    def head_to_head(cluster):
        """簇内直接对局小分（簇内对战场次的得分）。"""
        members = set(cluster)
        sub = {pid: 0.0 for pid in cluster}
        for g in played:
            b, w = g.get("black"), g.get("white")
            if b in members and w in members:
                sb, sw = score_outcome(g["outcome"])
                sub[b] += sb
                sub[w] += sw
        return sub

    # 逐级破同分：初始簇按积分划分，每级在簇内切分
    order = list(rulebook["tiebreak"])
    clusters = _cluster_by(players, lambda p: entries[p]["score"])
    for step in order:
        next_clusters = []
        for cluster in clusters:
            if len(cluster) <= 1:
                next_clusters.append(cluster)
                continue
            if step == "直接对局":
                sub = head_to_head(cluster)
                parts = _cluster_by(cluster, lambda p: sub[p])
            else:
                parts = _cluster_by(cluster,
                                    lambda p, s=step: entries[p]["metrics"][s])
            next_clusters.extend(parts)
        clusters = next_clusters

    ranked = []
    position = 0
    for cluster in clusters:
        position += len(cluster)
        rank = position - len(cluster) + 1
        for pid in cluster:
            row = dict(entries[pid])
            row["rank"] = rank
            row["eligible"] = not row["withdrawn"]
            ranked.append(row)
    return ranked


def _cluster_by(items, key):
    """按 key 降序把相邻同值项聚成簇。"""
    ordered = sorted(items, key=key, reverse=True)
    clusters, cur, last = [], [], None
    sentinel = object()
    for item in ordered:
        value = key(item)
        if last is not sentinel and value != last:
            clusters.append(cur)
            cur = []
        cur.append(item)
        last = value
    if cur:
        clusters.append(cur)
    return clusters


def swiss_pairings(rulebook, group, players, games, round_no, *,
                   withdrawn=None, previous_pairs=None, colors=None):
    """简化瑞士制：按当前积分排序，上半区对下半区，回避重复对阵，奇数人轮空。

    配对在每轮开赛前发布即冻结；这里只提供确定性生成器，
    裁判长也可显式提交配对表覆盖。调用方需保证 game_id 跨组别唯一，
    赛区服务会为自动生成的编号追加组别前缀。
    """
    if round_no < 1 or round_no > rulebook["rounds"]:
        raise EventError("轮次超出冻结范围")
    standings = compute_standings(rulebook, group, players, games,
                                  withdrawn=withdrawn,
                                  through_round=round_no - 1)
    active = [row["person_id"] for row in standings
              if row["person_id"] not in (withdrawn or set())]
    paired = set()
    for g in games:
        if g["round"] == round_no:
            paired.add(g.get("black"))
            paired.add(g.get("white"))
    active = [p for p in active if p not in paired]

    used = set(previous_pairs or ())
    white_count = dict(colors or {})
    result = []
    pool = list(active)
    bye_assigned = None
    if len(pool) % 2 == 1:
        bye_assigned = pool[-1]
        pool = pool[:-1]
        result.append({"game_id": f"R{round_no}-BYE", "round": round_no,
                       "black": bye_assigned, "white": None, "is_bye": True})

    half = len(pool) // 2
    top, bottom = pool[:half], pool[half:]
    leftovers = []
    for hi in top:
        mate = None
        for candidate in list(bottom):
            pair = tuple(sorted((hi, candidate)))
            if pair not in used:
                mate = candidate
                bottom.remove(candidate)
                break
        if mate is None and bottom:
            mate = bottom.pop(0)
        if mate:
            used.add(tuple(sorted((hi, mate))))
            b, w = _assign_colors(hi, mate, white_count)
            result.append({"game_id": f"R{round_no}-{len(result) + 1}",
                           "round": round_no, "black": b, "white": w,
                           "is_bye": False})
        else:
            leftovers.append(hi)
    if leftovers:  # 极端情况下兜底配对
        for i in range(0, len(leftovers) - 1, 2):
            b, w = _assign_colors(leftovers[i], leftovers[i + 1], white_count)
            result.append({"game_id": f"R{round_no}-X{i // 2 + 1}",
                           "round": round_no, "black": b, "white": w,
                           "is_bye": False})
    return result


def _assign_colors(p1, p2, white_count):
    """返回 (黑, 白)：执白次数少者本轮执白，持平时按 pid 确定性分配。"""
    c1, c2 = white_count.get(p1, 0), white_count.get(p2, 0)
    if c1 < c2:
        return p2, p1
    if c2 < c1:
        return p1, p2
    return (p1, p2) if p1 < p2 else (p2, p1)
