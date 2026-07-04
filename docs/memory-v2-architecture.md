# memory-v2 全链路

## Evidence

post/comment/chat 的 create/edit/rerun/delete 都追加不可变 event。图片摘要使用
`post_vision` source。每个 source 有独立递增 revision，delete 也保留版本记录。

## Reconcile

runner 找出 cursor 之后仍有 event，或存在 challenged unit 的 buckets。LLM 只接收当前
用户 evidence、当前 units 和 tombstones，并输出 add/retain/confirm/revise/retract。
系统验证 event 属于当前批次且不跨 bucket，然后原子写入 unit、operation、cursor 和
reconcile run。

用户编辑 unit 时，旧 evidence 进入 `review_pending`，独立 relink judge 决定保留或
移除链接。用户创建的 unit 以用户断言本身成立，不会因原始 source 删除而自动消失。

## Portrait

`tier=core`、重要度和置信度达到阈值的 units 才能进入画像。`portrait_policy` 可强制
纳入或排除，`prompt_policy=no_prompt` 会从所有回复 prompt 中禁用该 unit。

准入是多路的（P4）：用户亲述（user_authored / force_include）直通；推断类靠
confidence 过 ENTER/EXIT 滞回，或者靠行为信号（confirm 次数、独立证据数、存活时长
——纯客观计数，换模型不漂）攒够 corroboration 分后走更低的 CONFIRMED_ENTER 低水位。
行为信号只加分、永不做硬门槛（前车之鉴：op-count dwell 门永久挡住一次性明确自述的
身份，已移除）。importance 保持 0.70 硬地板——画像始终是"重要事实"的底座。

打分本身也不再是自由浮点：reconcile prompt 给五个带例子的锚定档位
（0.3/0.5/0.7/0.85/0.95），代码侧再吸附一次，离散档跨模型一致性远好于小数。
检索的语义门也从固定 0.30 地板改为按查询自适应：在相似度序列的最大落差处截断
（头部簇即使整体低于 0.30 也能进），无明显落差回退保守地板，0.20 兜底防垃圾；
有 FTS 关键词佐证的命中走宽门计分，纯语义命中走严门。

## Read model

- 用户基线画像：global/public `user_portrait`
- 当前状态：有时效窗口的 state units
- 相关记忆：scope-filtered unit retrieval
- 对话回想：命中 unit 的原始 evidence 周边
- 最新动态：cursor 之后的近期 user evidence
- SOUL 关系：聚合当前 SOUL 的 thread/private relationship units

注入的画像/状态/相关记忆/原始证据都带代码格式化的相对时间标（"3 天前"），按
`last_confirmed`（证据按 `occurred_at`）计算，并附时间解读规则——模型只解读现成
标签，不做日期运算。写侧对应铁律：unit 内容禁止"今晚/最近"等相对时间词，必须落成
绝对表述，防止多天后读到时失真。

## 删除与修订

source edit/delete 会 challenge 所有引用该 source 当前版本的 units。下一轮必须为每个
challenged unit 给出唯一决定。撤回后的 unit 保留 operation history 和 tombstone，
防止错误信念被重复生成。

用户侧的撤回叫「忘记」：只翻 unit 状态并更新画像，原始 post/chat 证据永不删除。
两种原因语义不同——`outdated`（默认）表示"曾经成立、现在过时"，允许新证据重建同一
信念；`false` 表示"从来不成立"，tombstone 永久阻止再生。UI 默认落在 outdated，
因为误标代价不对称：误标过时可逆，误标 false 是静默且永久的封杀。

tombstone 压制是双保险（P2）：撤回后 reconcile runner 用轻量 LLM 批量把撤回内容
规范化成 `normalized_claim`（主语统一、去修辞、绝对时间、保留否定词），喂 prompt
时以断言替代原文（换措辞也压得住）；false tombstone 的 claim 同时作为声明式向量
文档进索引（`tombstone-<unit_id>`，恢复后随重建自动退场），add 落库前做同桶相似度
兜底拦截（≥0.86 阻断，向量不可用时 fail-open，prompt 压制仍在）。outdated tombstone
不建向量——新证据本就允许它重新成立。claim 同时是 P1 linker 的匹配键。
