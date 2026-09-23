# 业余围棋晋级核算

全国业余围棋大赛四个分赛区（名手/名师/名人三组）的赛事资格与核算后端。汇集对局结果、
身份归并、破同分计算、名额递补与总决赛名单锁定，所有业务事实保存在**仅追加事件日志**中。

## 业务原则如何落地

| 需求 | 实现 |
| --- | --- |
| 开赛前冻结组别规则、轮次、配对、破同分次序 | `rules:freeze` / `rounds:freeze` / `pairings:freeze`，冻结后不可改；每次冻结生成不可变 `rule_version`，排名与解释固定引用比赛发生时的版本 |
| 裁判提交胜负/弃权保留棋谱哈希与签字来源 | `record_hash` 与 `signature_source` 为必填（缺其一返回 422）；随结果事件落库，轨迹可查 |
| 迟到结果、申诉裁决、录入纠错只能以新事件修正 | `late_result_submitted`、`appeal_amended/rejected`、`result_corrected` 均追加新事件并引用 `old_event_id`，旧事件标记失效但永不删除 |
| 重新投递任何一轮成绩不改变已确认事实 | 无键重投同一事实返回 `duplicate_fact`（409）；带 `Idempotency-Key` 重放返回首次事件，二者都不产生第二条事实 |
| 识别同一棋手的多种证件表示 | 证件号归一化后计算 HMAC「相关标识」`match_code`（不可逆）；同证同人，两证须资格审核员显式 `identity/merge` |
| 姓名同音 | 仅生成人工复核单（每对人一张），**绝不自动合并**；审核员裁决 distinct/merged |
| 不向其他赛区泄露完整身份 | 完整证件明文只进密封表（静态不明文），拆封必留审计；跨站 `trace` 视图只含赛区、组别、资格状态等最小字段 |
| 禁止一人占用重复晋级位 | 一个 `person_id` 全局最多一席；跨站重复报名者只保留确认在先的席位，后者顺延给下一位，并记录 `excluded_duplicates` |
| 退赛/递补仅重算受影响组 | 只对该组追加 `quota_adjusted`，向相关棋手发 promoted/seat_lost 通知；跨站席位空出时仅向组委会发复核建议，不擅改别组事实 |
| 锁榜后解释每位棋手 | `/trace` 给出经过的每盘棋、结果来源事件、棋谱哈希、签字来源、规则版本；`/explanation` 逐级对比破同分并指出决定项 |
| 锁定后事实不可变 | `/finals/lock` 后一切写命令返回 `finals_locked`；名单含 `roster_hash` |
| 防篡改 | 事件组成全局 SHA-256 哈希链（跨流串联），`/verify` 与 `--check` 重放校验，任何改动/删除/重排都会断链 |

破同分指标按冻结次序逐级取用：`score → sos（对手分）→ sosos → cumulative（累进分）→
direct（直胜）→ wins → black（黑胜局）→ birth（年长优先，解释时不暴露对方年份）→
draw（以冻结规则版本为种子的确定性抽签）`。冻结规则时强制以抽签收尾，保证排名是全序，
临界名次不会因算不平而错占名额；若抽签值极端碰撞且并列跨在晋级线上，确认被拒绝并要求仲裁。

## 运行

```bash
python3 service.py --check                 # 配置自检（带 --db 时连事件链一起校验）
python3 service.py --port 8000 --db gq.db  # 启动；--db 默认 :memory:
python3 -m unittest -v                     # 全部测试
```

仅依赖 Python 3.12 标准库。

## 鉴权

请求头（演示用，生产替换为网关联名）：

- `X-Actor`：操作者标识；`X-Role`：`organizer` / `station_admin` / `referee` / `jury` / `auditor`
- `X-Station`：赛区角色的所属赛区，操作其他赛区返回 403
- 写接口可带 `Idempotency-Key`，同键重放返回首次事件

## 主要接口

```
POST /tournament                         组委会创建赛事
POST /stations                           组委会开站（自动建三个组）
POST /stations/{s}/groups/{g}/registrations           报名（证件密封，返回脱敏）
POST /stations/{s}/groups/{g}/rules:freeze            冻结规则（轮次/名额/破同分次序）
POST /stations/{s}/groups/{g}/rounds:freeze           冻结轮次
GET  /stations/{s}/groups/{g}/rounds/{n}/pairings/next   Swiss 配对建议
POST /stations/{s}/groups/{g}/rounds/{n}/pairings:freeze  冻结配对
POST /stations/{s}/groups/{g}/rounds/{n}/boards/{b}/results          提交胜负（含棋谱哈希/签字）
POST /stations/{s}/groups/{g}/rounds/{n}/boards/{b}/forfeits         弃权
POST /stations/{s}/groups/{g}/rounds/{n}/boards/{b}/results:correct  录入纠错（新事件）
POST /stations/{s}/groups/{g}/rounds/{n}/boards/{b}/appeals          申诉
POST /stations/{s}/groups/{g}/appeals/{id}/decision                  仲裁 uphold/amend
POST /stations/{s}/groups/{g}/players/{p}/withdrawal                 退赛
POST /stations/{s}/groups/{g}/confirmation             赛区确认晋级
POST /stations/{s}/groups/{g}/reconfirmation           组委会复核递补
GET  /stations/{s}/groups/{g}/standings                 排名
GET  /stations/{s}/groups/{g}/players/{p}/explanation   临界名次解释
GET  /persons/{p}/trace?viewer_station={s}              棋手轨迹（跨站最小化）
GET  /review/flags                                      同音/疑似重复复核单
POST /review/flags/{id}/resolve                         审核员裁决
POST /identity/merge                                    显式合并两种证件
POST /seals/{id}/unseal                                 拆封完整身份（留审计）
GET  /audit/seals                                       拆封审计
POST /finals/lock                                       总决赛锁榜
GET  /finals                                            锁定名单
GET  /events?station=&group=                            事件日志
GET  /verify                                            哈希链校验
GET  /notifications?station=                            通知
```

组别路径含中文，客户端需做百分号编码（服务端自动解码）。

## 存储结构（SQLite，仅追加）

- `events`：全局自增 `seq`、流内 `version`、`prev_hash/entry_hash` 哈希链、幂等 `event_id`
- `idempotency`：`Idempotency-Key` → 首次事件
- `seals` / `seal_audit`：身份明文密封与每次拆封留痕
- `meta`：存储版本、身份 HMAC 密钥、密封主密钥

## 代码结构

- `store.py`：仅追加事件存储（哈希链、乐观并发、幂等、PII 密封）
- `tournament/ident.py`：证件归一化、HMAC 相关标识、脱敏、同音键
- `tournament/pairing.py`：Swiss 配对建议
- `tournament/standing.py`：大分/对手分/累进分/直胜/抽签与逐级破同分排名
- `tournament/domain.py`：命令、投影、资格核算、递补、轨迹与解释
- `api.py` / `service.py`：HTTP 接口与启动入口
