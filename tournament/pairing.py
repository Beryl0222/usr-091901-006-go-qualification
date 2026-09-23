"""瑞士制配对建议（开赛前由裁判长冻结，系统不擅自改配）。

纯函数：输入历史有效对局与报名名单，输出下一轮建议配对。
编排方可采纳后调用 freeze_pairings 冻结；冻结后任何人不能改。
"""

from __future__ import annotations

from collections import defaultdict


def suggest_pairings(players: list[str], *, played: set[tuple[str, str]],
                     scores: dict[str, float], round_no: int) -> list[dict]:
    """按当前大分相邻配对，回避已遇对手；奇数人时最低分者轮空。

    players: 本轮可赛棋手（未退赛）。
    played: 已出现过的无序对。
    """
    ordered = sorted(players, key=lambda p: (-scores.get(p, 0.0), p))
    boards: list[dict] = []
    bye_player = None
    pool = ordered
    if len(pool) % 2 == 1:
        bye_player = ordered[-1]
        pool = ordered[:-1]

    pairs = _pair(list(pool), played)
    for i, (w, b) in enumerate(pairs, start=1):
        boards.append({"board": f"B{i}", "white": w, "black": b})
    if bye_player is not None:
        boards.append({"board": f"BYE{round_no}", "white": bye_player, "black": None, "bye": True})
    return boards


def _pair(pool: list[str], played: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """贪心匹配：分数段内相邻优先，冲突时向下寻找。"""
    used = set()
    result: list[tuple[str, str]] = []

    def met(a, b):
        return (a, b) in played or (b, a) in played

    for i, p in enumerate(pool):
        if p in used:
            continue
        for j in range(i + 1, len(pool)):
            q = pool[j]
            if q in used:
                continue
            if not met(p, q):
                result.append((p, q)); used.add(p); used.add(q); break
        else:
            # 全部撞车（小概率）：放开回避兜底
            for j in range(i + 1, len(pool)):
                q = pool[j]
                if q not in used:
                    result.append((p, q)); used.add(p); used.add(q); break
    return result
