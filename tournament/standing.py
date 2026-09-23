"""积分与破同分计算。

破同分次序在赛区/组别冻结时写入规则快照，排名解释始终引用该版本。
支持的破同分指标（按次序取用）：
- score        大分（胜 1 / 和 0.5 / 负 0，弃权负 0）
- sos         对手分（对手大分之和；轮空对手按虚拟胜处理）
- sosos       对手的对手分
- cumulative  累进分（每轮后大分之和）
- direct      直胜（同分并列者间对局结果）
- wins        胜局数（含弃权胜）
- black       黑方胜局数
- birth       出生年份靠前者列前（来自报名快照，不参与公开解释）
- draw        抽签（种子为冻结时刻+排名键，可复核）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

WIN = 1.0
DRAW = 0.5
LOSS = 0.0

DEFAULT_TIEBREAKS = ("sos", "cumulative", "direct", "wins", "draw")

RESULT_POINTS = {
    "win": WIN, "loss": LOSS, "draw": DRAW,
    "bye_win": WIN,
    "forfeit_win": WIN, "forfeit_loss": LOSS,
    "referee_win": WIN, "referee_loss": LOSS,
}


@dataclass
class GameRecord:
    round_no: int
    board: str
    white: str
    black: str
    result: str  # 见 RESULT_POINTS
    points_white: float
    points_black: float
    source_event: str
    effective: bool = True
    correction_of: Optional[str] = None
    appeal: bool = False


@dataclass
class PlayerTab:
    pid: str
    games: dict[int, GameRecord] = field(default_factory=dict)
    score: float = 0.0
    sos: float = 0.0
    sosos: float = 0.0
    cumulative: float = 0.0
    wins: float = 0.0
    black_wins: float = 0.0


def compute_tables(games: list[GameRecord], players: list[str], rounds: int) -> dict[str, PlayerTab]:
    """只统计 effective 对局（被更正替换的旧结果不记分）。"""
    tabs = {p: PlayerTab(pid=p) for p in players}
    current: dict[tuple[str, int], GameRecord] = {}
    for g in games:
        if not g.effective:
            continue
        current[(g.white, g.round_no)] = g
        current[(g.black, g.round_no)] = g
        tabs.setdefault(g.white, PlayerTab(pid=g.white))
        tabs.setdefault(g.black, PlayerTab(pid=g.black))

    by_round: dict[int, list[GameRecord]] = {}
    seen: set[tuple[int, str]] = set()
    for g in current.values():
        key = (g.round_no, g.board)
        if key in seen:
            continue  # 同一盘棋分别以白方/黑方键各出现一次
        seen.add(key)
        by_round.setdefault(g.round_no, []).append(g)

    running = {p: 0.0 for p in tabs}
    for rnd in range(1, rounds + 1):
        for g in by_round.get(rnd, []):
            tabs[g.white].games[rnd] = g
            tabs[g.black].games[rnd] = g
            tabs[g.white].score += g.points_white
            tabs[g.black].score += g.points_black
            if g.points_white >= WIN:
                tabs[g.white].wins += 1
            if g.points_black >= WIN:
                tabs[g.black].wins += 1
            if g.points_black >= WIN:
                tabs[g.black].black_wins += 1
        for p in tabs:
            running[p] = tabs[p].score
            tabs[p].cumulative += running[p]

    # 对手分：真实对手的大分；轮空（bye_win，对手是自己）按虚拟全胜对手
    for p, tab in tabs.items():
        sos = 0.0
        for g in tab.games.values():
            opp = g.black if g.white == p else g.white
            if opp == p:  # 轮空
                sos += _bye_opponent_score(g.round_no, rounds)
            else:
                sos += tabs[opp].score
        tab.sos = sos
    for p, tab in tabs.items():
        tab.sosos = sum(
            tabs[(g.black if g.white == p else g.white)].sos
            for g in tab.games.values()
            if (g.black if g.white == p else g.white) != p
        )
    return tabs


def _bye_opponent_score(round_no: int, rounds: int) -> float:
    """轮空按该轮虚拟对手全胜折算（Swiss 惯例：后半程轮空吃亏小）。"""
    return float(max(0, rounds - round_no))


def direct_comparison(order: list[str], tabs: dict[str, PlayerTab]) -> dict[str, float]:
    """并列者之间相互对局的累计得分（每盘在各自主记录中只出现一次）。"""
    tied = set(order)
    out = {p: 0.0 for p in order}
    for p in order:
        for g in tabs[p].games.values():
            opp = g.black if g.white == p else g.white
            if opp in tied:
                out[p] += g.points_white if g.white == p else g.points_black
    return out


def rank_players(
    tabs: dict[str, PlayerTab],
    tiebreaks: list[str],
    *,
    birth_years: Optional[dict[str, int]] = None,
    draw_seed: str = "",
) -> list[dict]:
    """返回带名次与每级破同分取值的完整排名。"""
    pids = list(tabs)

    def metric_value(pid: str, tb: str) -> float:
        if tb == "score":
            return tabs[pid].score
        if tb == "sos":
            return tabs[pid].sos
        if tb == "sosos":
            return tabs[pid].sosos
        if tb == "cumulative":
            return tabs[pid].cumulative
        if tb == "wins":
            return tabs[pid].wins
        if tb == "black":
            return tabs[pid].black_wins
        if tb == "birth":
            return -float((birth_years or {}).get(pid, 9999))
        if tb == "draw":
            return _draw_pick(draw_seed, pid)
        return 0.0  # direct 在分桶时单独计算

    ranked: list[dict] = []
    # 逐级分桶
    tb_levels = [tb for tb in tiebreaks if tb != "score"]

    def split(bucket: list[str], level_idx: int) -> list[list[str]]:
        if len(bucket) <= 1 or level_idx >= len(tb_levels):
            return [bucket]
        tb = tb_levels[level_idx]
        if tb == "direct":
            dc = direct_comparison(bucket, tabs)
            keyed = [(dc[p], p) for p in bucket]
        elif tb == "draw":
            keyed = [(_draw_pick(draw_seed, p), p) for p in bucket]
        else:
            keyed = [(metric_value(p, tb), p) for p in bucket]
        keyed.sort(key=lambda x: x[0], reverse=True)

        groups: list[list[str]] = []
        group_vals: list[float] = []
        for val, p in keyed:
            if groups and _tie_equal(tb, val, group_vals[-1]):
                groups[-1].append(p)
            else:
                groups.append([p]); group_vals.append(val)
        result: list[list[str]] = []
        for g in groups:
            result.extend(split(g, level_idx + 1))
        return result

    ordered: list[str] = []
    # 顶层按大分分桶
    by_score: dict[float, list[str]] = {}
    for p in pids:
        by_score.setdefault(tabs[p].score, []).append(p)
    final_groups: list[tuple[float, list[str]]] = []
    for score in sorted(by_score, reverse=True):
        bucket = by_score[score]
        for g in split(bucket, 0):
            final_groups.append((score, g))
            ordered.extend(g)

    place = 0
    for score, g in final_groups:
        start = place + 1
        place += len(g)
        tied = len(g) > 1
        dc = direct_comparison(g, tabs)
        for p in g:
            t = tabs[p]
            ranked.append({
                "rank": str(start) if not tied else f"{start}-{start + len(g) - 1}",
                "rank_no": start,
                "tied": tied,
                "player_id": p,
                "score": t.score,
                "sos": _round1(t.sos),
                "sosos": _round1(t.sosos),
                "cumulative": _round1(t.cumulative),
                "wins": t.wins,
                "black_wins": t.black_wins,
                "direct_among_tied": _round1(dc[p]),
                "draw_pick": _draw_pick(draw_seed, p) if "draw" in tiebreaks else None,
                "tiebreak_order": tiebreaks,
            })
    return ranked


def _draw_pick(seed: str, pid: str) -> float:
    h = hashlib.sha256(f"{seed}|{pid}".encode("utf-8")).hexdigest()
    return int(h[:12], 16) / float(16 ** 12)


def _tie_equal(tb: str, a: float, b: float) -> bool:
    if tb == "draw":
        return a == b  # 抽签碰撞时保持并列，由下一级（理论上没有）或人工裁决
    return abs(a - b) < 1e-9


def _round1(x: float) -> float:
    return round(x + 1e-9, 1)
