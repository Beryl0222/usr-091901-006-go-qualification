# 业余围棋晋级核算

汇集四个分赛区（东/西/南/北）的对局结果、身份归并、破同分计算和总决赛递补依据，覆盖名手、名师、名人三个组别。系统以**仅追加事件溯源**为核心：当前状态全部由事件日志折叠而来，历史不可改写。

## 它如何对应需求

| 需求 | 实现 |
| --- | --- |
| 每站开赛前冻结组别规则、轮次、配对、破同分次序 | `RulesFrozen`（组别名额合计十席、七轮、破同分全序且必须以可复现抽签兜底）；逐轮 `PairingsPublished` 冻结，规则版本随每份配对与每条成绩留存 |
| 裁判提交胜负/弃权保留棋谱哈希与签字来源 | 成绩必须携带 SHA-256 `record_hash`、`record_kind`（棋谱/弃权单）和 `signature`（签字人/角色/渠道）；缺席即拒 |
| 迟到结果、申诉、纠错只能以新事件修正 | `LateResultRecorded` / `AppealRuled` / `ResultCorrected` 独立事件；每条维护 `supersedes_seq` 与事件链，过期基准的纠错会被拒绝 |
| 同一棋手多种证件表示、同音姓名 | 身份证 15/18 位自动等价；同证件自动归并；同音+同性别生日只出**候选**（`/identity/candidates`），须审核员 `IdentityMerged` 确认 |
| 不向其他赛区泄露完整身份 | 赛区侧只有 `/stations/{st}/roster-view`：假名通行码（同一棋手每赛区一个 HMAC 码）+ 掩码姓名/证件；完整身份与 `/federation/*` 仅组委会/审核员可见 |
| 禁止一人占用重复晋级位 | 投影后按冻结优先序 `PromotionDeduplicated` 释放低优先站席位；锁定前若仍有重复占用直接拒绝并提示先去重 |
| 退赛/递补只重算受影响组别、通知相关人 | 释放一个席位只在该站该组按名次补位（`PromotionAdvanced`）或登记 `QuotaVacancy`；`NotificationQueued` 的 scope 精确到站点+组别，按通行码寻址 |
| 锁定后可解释每位棋手 | `/federation/explain/{pid}` 给出逐轮对局（含棋谱哈希与完整事件链）、规则版本、积分/破同分指标、与紧后一名分出先后的具体环节；名单快照含指纹 |
| 重投任何一轮成绩不改变已确认事实 | 所有写命令先查幂等键再做任何校验，同键返回首次事件；投影后赛果冻结，锁定后拒绝一切非幂等成绩变更 |

## 模块

- `events.py` — 哈希链事件日志（JSONL 持久化、篡改检测、幂等表）
- `rules.py` — 赛前规则规范化与校验（三组别、十席、七轮、破同分全序）
- `identity.py` — 身份登记/归并/同音候选/赛区通行码与掩码视图
- `standings.py` — 多级破同分（SOS/削峰/累进分/直接对局/胜对手分/冻结种子抽签）与简化瑞士制配对
- `station.py` — 赛区聚合：冻结、报名、配对、结果、迟到、申诉、纠错、退赛、终赛
- `federation.py` — 总决赛：总决赛规则冻结、投影、跨区去重、递补/空缺、通知、锁定、解释
- `api.py` / `service.py` — HTTP 适配与服务入口（角色令牌见下表）
- `app.py` — 装配（一个日志 + 四服务），支持从 JSONL 完整重放

| 角色（X-Role 令牌） | 对应领域角色 |
| --- | --- |
| `station-admin` | 赛区管理员 |
| `judge` | 裁判 |
| `reviewer` | 资格审核员 |
| `organizer` | 总决赛组委会 |

## 运行

```bash
python3 service.py --check                      # 装配与契约自检
python3 -m unittest discover -v                 # 53 个测试（含四赛区完整剧情）
python3 service.py --port 8000 --store events.jsonl
```

启用 `--store` 后所有事件落 JSONL，重启时校验哈希链并完整重放；尾部哈希一致即状态一致。

## 主要接口

| 方法 | 路径 | 角色 |
| --- | --- | --- |
| POST | `/stations/freeze` | station-admin / organizer |
| POST | `/stations/{st}/register` | station-admin（仅本赛区） |
| POST | `/stations/{st}/rounds/{n}/pairings` | station-admin / judge（仅本赛区） |
| POST | `/stations/{st}/games/{gid}/result` | judge（临场成绩） |
| POST | `/stations/{st}/games/{gid}/late-result` | judge（迟到补录，须 reason/accepted_by） |
| POST | `/stations/{st}/games/{gid}/correction` | station-admin / judge（须书面原因） |
| POST | `/stations/{st}/games/{gid}/appeal` | station-admin（维持/改判/重赛，须依据） |
| POST | `/stations/{st}/withdraw`、`/finalize` | station-admin |
| GET | `/stations/{st}/roster-view` | 本赛区角色（掩码）/ reviewer |
| GET | `/stations/{st}/standings` | 本赛区角色 / reviewer |
| POST | `/identity/merge` | reviewer |
| GET | `/identity/candidates` | reviewer |
| POST | `/admin/freeze-rules` | organizer（赛区清单+优先序） |
| POST | `/federation/project/{st}` | 本赛区管理员 / organizer |
| POST | `/federation/duplicates/resolve` | organizer / reviewer |
| POST | `/federation/revoke` | organizer（放弃资格→站内递补） |
| POST | `/federation/lock` | organizer（四十席唯一才能锁） |
| GET | `/federation/roster`、`/status/{st}/{group}`、`/explain/{pid}` | organizer / reviewer |
| GET | `/federation/notifications` | 按角色限定范围（赛区只见本赛区） |
| GET | `/events`、`/audit` | organizer / reviewer |

所有写请求建议带 `Idempotency-Key`；同键同内容重投返回首次事件，同键异内容 422。

## 关键语义边界

- **终赛 ≠ 冻结**：终赛到投影前的窗口仍可追加迟到/纠错/裁决事件（投影指纹会纳入）；一旦 `PromotionProjected`，该站赛果冻结，资格变动只能走总决赛退赛/递补；`FinalRosterLocked` 后一切成绩变更拒绝。
- **抽签不可操纵**：破同分次序最后一环固定为"抽签"，键由冻结种子+组别+棋手派生，任何时候重算名次一致。
- **空缺不硬凑**：候补名单不足时登记空缺并阻止锁定，而不是把零分/退赛者塞进名单。
- **通知不越界**：西站递补不会通知东赛区；赛区侧通知以通行码而非证件号寻址。
