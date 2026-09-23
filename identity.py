"""身份归并与赛区隐私。

要求：
* 识别同一棋手的多种证件表示（身份证 15/18 位升级、多个证件）；
* 姓名同音只作为候选，由资格审核员确认合并，保留证据事件；
* 赛区只能看到本赛区棋手的通行码与掩码信息，看不到完整证件，
  跨赛区去重由总决赛层面在内部完成。
"""

import hashlib
import hmac
import re
import uuid

from events import EventError

# ---- 常见汉字读音表（无第三方依赖时的内置兜底；可注入完整拼音器）----
_PINYIN = {
    "张": "zhang", "王": "wang", "李": "li", "赵": "zhao", "刘": "liu",
    "陈": "chen", "杨": "yang", "黄": "huang", "周": "zhou", "吴": "wu",
    "徐": "xu", "孙": "sun", "胡": "hu", "朱": "zhu", "高": "gao",
    "林": "lin", "何": "he", "郭": "guo", "马": "ma", "罗": "luo",
    "梁": "liang", "宋": "song", "郑": "zheng", "谢": "xie", "韩": "han",
    "唐": "tang", "冯": "feng", "于": "yu", "董": "dong", "萧": "xiao",
    "程": "cheng", "曹": "cao", "袁": "yuan", "邓": "deng", "许": "xu",
    "傅": "fu", "沈": "shen", "曾": "zeng", "彭": "peng", "吕": "lv",
    "苏": "su", "卢": "lu", "蒋": "jiang", "蔡": "cai", "贾": "jia",
    "丁": "ding", "魏": "wei", "薛": "xue", "叶": "ye", "阎": "yan",
    "余": "yu", "潘": "pan", "杜": "du", "戴": "dai", "夏": "xia",
    "钟": "zhong", "汪": "wang", "田": "tian", "任": "ren", "姜": "jiang",
    "范": "fan", "方": "fang", "石": "shi", "姚": "yao", "谭": "tan",
    "伟": "wei", "芳": "fang", "娜": "na", "敏": "min", "静": "jing",
    "丽": "li", "强": "qiang", "磊": "lei", "军": "jun", "洋": "yang",
    "勇": "yong", "艳": "yan", "杰": "jie", "娟": "juan", "涛": "tao",
    "明": "ming", "超": "chao", "秀": "xiu", "英": "ying", "华": "hua",
    "金": "jin", "国": "guo", "春": "chun", "海": "hai", "小": "xiao",
    "建": "jian", "文": "wen", "志": "zhi", "立": "li", "晓": "xiao",
    "思": "si", "嘉": "jia", "子": "zi", "雨": "yu", "泽": "ze",
    "宇": "yu", "浩": "hao", "轩": "xuan", "涵": "han", "博": "bo",
    "威": "wei", "微": "wei", "刚": "gang", "钢": "gang", "冈": "gang",
    "峰": "feng", "锋": "feng", "燕": "yan", "雁": "yan",
    "斌": "bin", "滨": "bin", "毅": "yi", "义": "yi", "益": "yi",
    "平": "ping", "萍": "ping", "兰": "lan", "蓝": "lan",
    "珍": "zhen", "贞": "zhen", "梅": "mei",
    "庆": "qing", "青": "qing", "清": "qing",
    "鸿": "hong", "红": "hong", "宏": "hong", "洪": "hong",
    "云": "yun", "芸": "yun", "龙": "long", "隆": "long",
    "兴": "xing", "星": "xing",
    "鑫": "xin", "新": "xin", "欣": "xin", "心": "xin", "信": "xin",
    "世": "shi", "仕": "shi", "士": "shi",
    "利": "li", "礼": "li", "理": "li", "里": "li",
    "城": "cheng", "诚": "cheng",
    "德": "de", "道": "dao",
    "仁": "ren", "智": "zhi", "致": "zhi",
    "忠": "zhong", "正": "zheng", "政": "zheng",
    "安": "an", "岸": "an", "康": "kang", "慷": "kang",
    "宁": "ning", "凝": "ning",
    "晶": "jing", "京": "jing", "景": "jing", "菁": "jing",
    "俊": "jun", "峻": "jun",
    "哲": "zhe", "喆": "zhe", "语": "yu",
    "玉": "yu", "钰": "yu", "育": "yu", "裕": "yu",
    "元": "yuan", "原": "yuan", "源": "yuan", "圆": "yuan",
    "天": "tian", "添": "tian", "光": "guang", "广": "guang",
    "达": "da", "发": "fa", "法": "fa",
    "宝": "bao",
}

_CN_RE = re.compile(r"[一-鿿]")


def default_romanize(name):
    """内置读音：表内汉字取拼音（无声调），表外字保留原字。

    因此“李刚/李钢”在同表覆盖时归并为同一候选键，
    而未收录字不会制造错误的同音匹配。
    """
    parts = []
    for ch in name:
        if ch in _PINYIN:
            parts.append(_PINYIN[ch])
        elif _CN_RE.match(ch):
            parts.append(ch)
    return "".join(parts).lower()


def canonical_doc_number(doc_type, number):
    """证件号规范化：去空白、大写；身份证同时登记 15 位与 18 位两种形态。"""
    num = re.sub(r"\s+", "", str(number)).upper()
    dtype = str(doc_type).strip()
    if not num:
        raise EventError("证件号不能为空")
    keys = {(dtype, num)}
    if dtype in ("身份证", "居民身份证") and re.fullmatch(r"\d{17}[\dX]", num):
        # 18 位 = 6 位地区 + 19/20 + 6 位出生日期(YYMMDD) + 3 位顺序 + 校验位
        keys.add(("身份证", num[:6] + num[8:17]))
        keys = {("身份证", k[1]) for k in keys}
    elif dtype in ("身份证", "居民身份证") and re.fullmatch(r"\d{15}", num):
        keys = {("身份证", num)}
    return keys


def mask_name(name):
    name = str(name)
    if len(name) <= 1:
        return name
    return name[0] + "*" * (len(name) - 1)


def mask_number(number):
    number = re.sub(r"\s+", "", str(number))
    if len(number) <= 2:
        return "*" * len(number)
    return f"{number[0]}{'*' * (len(number) - 2)}{number[-1]}"


class IdentityService:
    """身份聚合：报名登记、同音候选、审核合并、赛区通行码。"""

    def __init__(self, store, *, master_secret="go-qualification-secret",
                 romanizer=None):
        self.store = store
        self._secret = master_secret.encode("utf-8")
        self._romanize = romanizer or default_romanize
        self.persons = {}          # person_id -> 人员档案
        self.doc_index = {}        # (类型, 证件号) -> person_id（含两种身份证形态）
        self.name_index = {}       # 同音键 -> set(person_id)
        self.parent = {}           # 合并并查集
        store.subscribe(self._apply)

    # ---- 事件折叠 ----
    def _apply(self, event):
        if event.type == "PlayerRegistered":
            pid = event.payload["person_id"]
            person = self.persons.setdefault(pid, {
                "person_id": pid, "name": event.payload["name"],
                "gender": event.payload.get("gender"),
                "birth_date": event.payload.get("birth_date"),
                "doc_keys": set(), "documents": [],
                "registrations": [], "aliases": set(),
            })
            person["registrations"].append({
                "station_id": event.payload["station_id"],
                "group": event.payload["group"],
                "name": event.payload["name"],
                "seq": event.seq,
            })
            if person["name"] != event.payload["name"]:
                person["aliases"].add(event.payload["name"])
            for d in event.payload["documents"]:
                for key in canonical_doc_number(d["type"], d["number"]):
                    person["doc_keys"].add(key)
                    owner = self.doc_index.get(key)
                    if owner and owner != pid:
                        # 同一证件在审核合并前被两次登记：挂候选，仍保持两个根
                        self._union_index(key, pid, owner)
                    else:
                        self.doc_index[key] = pid
                if not any(x["number"] == d["number"] and x["type"] == d["type"]
                           for x in person["documents"]):
                    person["documents"].append(dict(d))
            key = self._name_key(event.payload)
            self.name_index.setdefault(key, set()).add(pid)

        elif event.type == "IdentityMerged":
            survivor = event.payload["survivor_id"]
            merged = event.payload["merged_id"]
            self._link(survivor, merged)
            root = self.resolve(survivor)
            kept = self.persons.get(survivor)
            gone = self.persons.get(merged)
            if kept and gone:
                kept["aliases"].add(gone["name"])
                kept["aliases"].update(
                    r["name"] for r in gone["registrations"])

    def _union_index(self, key, pid, owner):
        """证件撞号：记录到双方别名线索，等待审核合并。"""
        for side in (pid, owner):
            person = self.persons.get(side)
            if person:
                person["aliases"].add(f"证件撞号:{key[0]}:{mask_number(key[1])}")

    def _name_key(self, payload):
        roman = self._romanize(re.sub(r"\s+", "", payload["name"]))
        return (roman, payload.get("gender"), payload.get("birth_date"))

    def resolve(self, pid):
        root = pid
        while self.parent.get(root, root) != root:
            root = self.parent[root]
        cur = pid
        while self.parent.get(cur, cur) != root:
            nxt = self.parent[cur]
            self.parent[cur] = root
            cur = nxt
        return root

    def _link(self, survivor, merged):
        rs, rm = self.resolve(survivor), self.resolve(merged)
        if rs == rm:
            raise EventError("两个身份已经归并过")
        # 合并证件索引
        gone = self.persons.get(rm)
        if gone:
            for key in list(gone["doc_keys"]):
                self.doc_index[key] = rs
        self.parent[rm] = rs

    # ---- 命令 ----
    def register(self, *, station_id, group, name, gender=None,
                 birth_date=None, documents, submitted_by,
                 idempotency_key=None, source=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        # 赛区侧守卫（IdentityService 也持有 stations 引用时由调用方保证；
        # 此处通过可注入的 station_gate 做开赛后禁报）
        gate = getattr(self, "station_gate", None)
        if gate is not None:
            gate(station_id, group)
        name = str(name or "").strip()
        if not name:
            raise EventError("缺少姓名")
        if not documents:
            raise EventError("至少需要一个证件")
        clean_docs = []
        all_keys = set()
        for d in documents:
            keys = canonical_doc_number(d["type"], d["number"])
            all_keys |= keys
            clean_docs.append({"type": str(d["type"]).strip(),
                               "number": re.sub(r"\s+", "", str(d["number"])).upper()})

        owner = None
        for key in all_keys:
            candidate = self.doc_index.get(key)
            if candidate:
                owner = self.resolve(candidate)
                break
        if owner is not None:
            prior = self.persons[owner]
            for reg in prior["registrations"]:
                if reg["station_id"] == station_id:
                    if reg["group"] == group:
                        raise EventError(
                            f"该棋手已在 {station_id} 报名 {group}，禁止同站重复报名")
                    raise EventError(
                        f"该棋手已在 {station_id} 报名 {reg['group']}，"
                        "同一赛区只能报名一个组别")
        pid = owner or f"P{uuid.uuid4().hex[:12]}"
        payload = {"person_id": pid, "station_id": station_id, "group": group,
                   "name": name, "gender": gender, "birth_date": birth_date,
                   "documents": clean_docs,
                   "matched_existing": owner is not None}
        return self.store.append("PlayerRegistered", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by, source=source)

    def homophone_candidates(self, station_id=None):
        """返回“同音同性别同生日但无共同证件”的候选组，供审核员确认。"""
        groups = []
        for key, pids in self.name_index.items():
            roots = {}
            for pid in pids:
                root = self.resolve(pid)
                person = self.persons.get(root)
                if not person:
                    continue
                if station_id is not None and not any(
                        r["station_id"] == station_id
                        for r in person["registrations"]):
                    continue
                roots.setdefault(root, person)
            if len(roots) > 1:
                docs_shared = set.intersection(
                    *(p["doc_keys"] for p in roots.values()))
                if not docs_shared:
                    groups.append({
                        "name_key": key[0], "gender": key[1],
                        "birth_date": key[2],
                        "persons": sorted(roots),
                    })
        return sorted(groups, key=lambda g: (g["name_key"], g["persons"]))

    def confirm_merge(self, survivor_id, merged_id, *, reason,
                      submitted_by, idempotency_key=None):
        prior = self.store.get_by_idempotency(idempotency_key)
        if prior is not None:
            return prior
        if survivor_id not in self.persons:
            raise EventError(f"未知棋手: {survivor_id}")
        if merged_id not in self.persons:
            raise EventError(f"未知棋手: {merged_id}")
        rs, rm = self.resolve(survivor_id), self.resolve(merged_id)
        if rs == rm:
            raise EventError("这两个身份此前已归并")
        # 允许合并的依据：共同证件，或同音候选，或审核员写明理由
        shared = self.persons[rs]["doc_keys"] & self.persons[rm]["doc_keys"]
        same_name_key = any(
            rm in pids for pids in self.name_index.values()
            if rs in pids)
        if not shared and not same_name_key and not reason:
            raise EventError("没有共同证件或同音依据时，必须填写书面归并理由")
        payload = {"survivor_id": rs, "merged_id": rm,
                   "reason": reason or "同音/同证件复核",
                   "evidence": {"shared_document": bool(shared),
                                "homophone": bool(same_name_key)}}
        return self.store.append("IdentityMerged", payload,
                                 idempotency_key=idempotency_key,
                                 submitted_by=submitted_by,
                                 source="资格审核")

    # ---- 查询与隐私 ----
    def person(self, pid):
        root = self.resolve(pid)
        person = self.persons.get(root)
        if not person:
            raise EventError(f"未知棋手: {pid}")
        return person

    def station_person_ids(self, station_id):
        ids = set()
        for person in self.persons.values():
            if self.resolve(person["person_id"]) != person["person_id"]:
                continue
            for reg in person["registrations"]:
                if reg["station_id"] == station_id:
                    ids.add(person["person_id"])
        return ids

    def passcode(self, pid, station_id):
        """赛区通行码：同一棋手在不同赛区得到不同且不可反推的码。"""
        root = self.resolve(pid)
        mac = hmac.new(self._secret,
                       f"station-pass|{root}|{station_id}".encode("utf-8"),
                       hashlib.sha256).hexdigest()[:16]
        return f"GP-{station_id[:2].upper()}-{mac}"

    def station_view(self, station_id):
        """赛区视角：只有通行码、掩码姓名和掩码证件，没有完整身份。"""
        rows = []
        for pid in sorted(self.station_person_ids(station_id)):
            person = self.person(pid)
            rows.append({
                "pass": self.passcode(pid, station_id),
                "name_masked": mask_name(person["name"]),
                "aliases_masked": sorted(mask_name(a) for a in person["aliases"]
                                         if not a.startswith("证件撞号:")),
                "documents_masked": [
                    {"type": d["type"], "number": mask_number(d["number"])}
                    for d in person["documents"]],
                "groups": sorted({r["group"] for r in person["registrations"]
                                  if r["station_id"] == station_id}),
            })
        return rows

    def federation_view(self, pid):
        """总决赛/审核视角的完整身份，仅在非赛区端口开放。"""
        person = self.person(pid)
        return {
            "person_id": person["person_id"],
            "name": person["name"],
            "gender": person["gender"],
            "birth_date": person["birth_date"],
            "documents": [dict(d) for d in person["documents"]],
            "aliases": sorted(a for a in person["aliases"]
                              if not a.startswith("证件撞号:")),
            "registrations": [dict(r) for r in person["registrations"]],
        }
