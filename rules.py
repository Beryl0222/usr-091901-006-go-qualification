"""规则版本与赛前冻结。

每站开赛前必须冻结：组别规则（每站十个名额的分配）、轮次表、
逐轮配对、破同分次序。冻结后该站对局全程引用同一规则版本，
赛后任何规则修订只对后续赛事生效——“晋级解释固定使用比赛
发生时的规则”。
"""

import copy
import re

from events import EventError

# 破同分次序的可选环节；freeze 时必须显式给出全序，不允许赛后再改。
TIEBREAK_CHOICES = (
    "对手分总和",      # SOS
    "对手分削去",      # 去掉最高最低对手后的 SOS
    "累进分",          # 每轮后积分累计之和
    "直接对局",        # 同分者之间的对赛结果
    "所胜对手分",      # SOSOS / 胜局对手分之和
    "抽签",            # 终局兜底，种子由冻结配置固定，可复现
)

VALID_RESULT_TYPES = ("胜负", "弃权")
VALID_GROUPS = ("名手组", "名师组", "名人组")
_GROUP_RE = re.compile(r"^(名手组|名师组|名人组)$")


def normalize_rules(raw):
    """校验并规范化一份规则冻结输入。"""
    if not isinstance(raw, dict):
        raise EventError("规则配置必须是对象")
    station_id = str(raw.get("station_id") or "").strip()
    if not station_id:
        raise EventError("缺少 station_id")

    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise EventError("至少要冻结一个组别")
    total_quota = 0
    norm_groups = []
    seen = set()
    for g in groups:
        name = g.get("name")
        if not _GROUP_RE.match(name or ""):
            raise EventError(f"未知组别: {name!r}，仅支持 {VALID_GROUPS}")
        if name in seen:
            raise EventError(f"组别重复: {name}")
        seen.add(name)
        quota = g.get("quota")
        if not isinstance(quota, int) or quota <= 0:
            raise EventError(f"{name} 名额必须为正整数")
        total_quota += quota
        norm_groups.append({"name": name, "quota": quota})

    rounds = raw.get("rounds")
    if not isinstance(rounds, int) or not 1 <= rounds <= 20:
        raise EventError("轮次数必须在 1..20 之间")

    tiebreak = raw.get("tiebreak")
    if not isinstance(tiebreak, list) or not tiebreak:
        raise EventError("必须显式冻结破同分次序")
    if len(tiebreak) != len(set(tiebreak)):
        raise EventError("破同分次序存在重复环节")
    for step in tiebreak:
        if step not in TIEBREAK_CHOICES:
            raise EventError(f"未知破同分环节: {step}")
    if tiebreak[-1] != "抽签":
        raise EventError("破同分次序必须以“抽签”兜底，保证名次全序")

    pairing_policy = str(raw.get("pairing_policy") or "瑞士制").strip()
    rules_version = str(raw.get("rules_version") or "").strip()
    if not rules_version:
        raise EventError("缺少 rules_version")
    komi = raw.get("komi", 7.5)
    if not isinstance(komi, (int, float)):
        raise EventError("贴目必须是数值")
    draw_seed = str(raw.get("draw_seed") or
                    f"{station_id}:{rules_version}").strip()

    return {
        "station_id": station_id,
        "rules_version": rules_version,
        "groups": norm_groups,
        "total_quota": total_quota,
        "rounds": rounds,
        "tiebreak": list(tiebreak),
        "pairing_policy": pairing_policy,
        "komi": float(komi),
        "draw_seed": draw_seed,
        "notes": str(raw.get("notes") or ""),
    }


class RuleBook:
    """某一站冻结后不可变的规则快照。"""

    __slots__ = ("data",)

    def __init__(self, data):
        self.data = copy.deepcopy(data)

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def group_names(self):
        return [g["name"] for g in self.data["groups"]]

    def quota(self, group):
        for g in self.data["groups"]:
            if g["name"] == group:
                return g["quota"]
        raise EventError(f"本站未开设组别: {group}")

    def to_public(self):
        """可对外发布的规则内容（本身不含任何身份信息）。"""
        return copy.deepcopy(self.data)
