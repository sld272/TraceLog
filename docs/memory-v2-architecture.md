# memory-v2 全链路

## Evidence

post/comment/chat 的 create/edit/rerun/delete 都追加不可变 event。图片摘要使用
`post_vision` source。每个 source 有独立递增 revision，delete 也保留版本记录。

## Reconcile

runner 找出 cursor 之后仍有 event，或存在 challenged unit 的 buckets。LLM 只接收当前
用户 evidence、当前 units 和 tombstones，并输出 add/retain/confirm/revise/retract。
系统验证 event 属于当前批次且不跨 bucket，然后原子写入 unit、operation、cursor 和
reconcile run。

unit 粒度以「一条可独立回问、可单独验证的事实」为准——宏观综合是画像层的职责，
不在 unit 层做。可被回问的具体事实（分数、日期、数字、决定、人名）不算瞬时琐事，
即使一次性也落 state/episodic unit（importance 0.5）；已有宽泛主题 unit 时新的具体
事实 add 新 unit，而不是只 confirm 到主题 unit 上（confirm 堆积会让事实沉在证据里
无法被检索，还稀释 recall 锚点的选取）。

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
- 相关记忆：scope-filtered unit retrieval，三路召回——unit 内容 FTS、unit 语义 ANN、
  原始证据语义 ANN 反推归属 unit（unit 内容是有损摘要，具体事实常只活在证据文本里；
  有归属的证据仅作检索键，注入走 unit 层，scope/折叠/墓碑/撤回全部照常生效，
  证据分与 unit 语义分 max 合并不重复计分）
- 未沉淀原文：证据通道命中但从未沉淀成任何 unit 的孤儿证据，unit 检索天然够不到，
  原文直接注入 `[未整理成记忆的相关原文]`（相似度降序，条数 + 字符预算，scope 过滤
  照常，同源从最新动态去重，进 引用记忆 面板）；被撤回 unit 的证据仍有关联行、
  relink 重判中（review_pending）的证据沉淀过一次，都不算孤儿——撤回语义不被绕过
- 对话回想：命中 unit 的原始 evidence 周边；证据通道命中的 unit 以真正匹配查询的那条
  证据为锚点，其余 unit 用关键词重合度猜测，两类锚点共用会话级去重
- 最新动态：cursor 之后的近期 user evidence
- SOUL 关系：聚合当前 SOUL 的 thread/private relationship units

注入的画像/状态/相关记忆/原始证据都带代码格式化的相对时间标（"3 天前"），按
`last_confirmed`（证据按 `occurred_at`）计算，并附时间解读规则——模型只解读现成
标签，不做日期运算。写侧对应铁律：unit 内容禁止"今晚/最近"等相对时间词，必须落成
绝对表述，防止多天后读到时失真。

## 跨桶链接（P1：链接，不合并）

桶是隐私底座，consolidate/supersede 拒绝跨桶——但同一事实/矛盾常落在两个桶里。
低频 linker pass 搭 reconcile 尾部的车：最近变动的 units → 跨桶候选（同文精确匹配 +
unit 向量近邻粗筛，喂给 LLM 的桶标签只有"公开/私聊"，人格名不出现）→ 一次批量
LLM 判定 → `memory_unit_links`（same_fact / contradicts / context_variant）。
只记录关系，永不搬内容、永不跨桶改写；一对只保留一个关系，后判替换先判
（回访把 contradicts 改判 context_variant 就是走这条路）。

- `same_fact`：读时折叠——两端同时被检索到时只注入更私密/更新的一条（公开场景私密端
  本来就不可见，折叠自然不生效）。
- `contradicts`：更公开的一侧打 `contested_at` 标。标是**无因**的：该 unit 退出画像
  （画像只能笃定陈述，无法收敛口吻）、检索降权 50%、注入行加「不太确定」标并配规则
  "不要解释或猜测它为什么不确定"——跨桶原因只允许出现在私聊回访里。同桶新证据
  （confirm/revise）自动清标（"新鲜证据回流后矛盾自然消解"）。
- `context_variant`：双面共存，不折叠、不打标——人在公开和私下本来就可以说不一样的话。

## 回访（矛盾的唯一出口）

contested 标只能靠用户亲口给的新证据化解。私聊组装回复时，若本轮检索已自然触及
矛盾话题（相关性门槛：链接任一端在本轮 used units 里），且持有私密端的正是这个
SOUL，就允许搭载一条 [回访] 指令，走阶梯：

- 第一级：只自然问近况（"这方面最近怎么样了"），指令明确禁止提及任何记忆差异——
  新鲜回答经 chat 证据 → reconcile 正常回流，矛盾多半不点破就消解。
- 第二级（上次问过仍不明朗才用）：温和核对现状，顺带给四个轻回答方向（以现在说的
  为准 / 有新变化 / 只是随口一说 / 两边都对，场合不同），铁律禁止"你之前说过…但…"
  式对质。

克制约束：仅私聊、仅话题相关、每条回复至多一条、同一条记忆 3 天内至多问一次
（指令一旦发出就计数，宁少勿烦）、`POST /memory/revisit-policy` 一键关闭。

闭环由 linker 的链接维护完成：任一端 last_confirmed 越过链接时间（回访回答被
reconcile 吸收）或一端死亡（撤回/淡出）时重判该链接——矛盾消解则降级关系并清
contested 标；公开侧新证据清标后私密端仍旧矛盾的，重判会诚实地重新打标。

source edit/delete 会 challenge 所有引用该 source 当前版本的 units。下一轮必须为每个
challenged unit 给出唯一决定。撤回后的 unit 保留 operation history 和 tombstone，
防止错误信念被重复生成。

用户侧的撤回叫「忘记」：只翻 unit 状态并更新画像，原始 post/chat 证据永不删除。
两种原因语义不同——`outdated`（默认）表示"曾经成立、现在过时"，允许新证据重建同一
信念；`false` 表示"从来不成立"，tombstone 永久阻止再生。UI 默认落在 outdated，
因为误标代价不对称：误标过时可逆，误标 false 是静默且永久的封杀。

「忘记 → 找回」是一个原子交付（P5）：`restore_unit` 只对 retracted_by_user 开放，
状态翻回 active 的同一事务里 retraction_reason 清空（prompt tombstone 集合与向量
tombstone 文档都按 status+reason 取数，压制随之解除）、画像成员重算。模型自己的
撤回不可找回——那是 reconciler 的地盘，证据仍支持时它会自己重建。

用户可见状态收敛为 5 个：已记住 / 整理中（pending+challenged，附原因"你最近改了
相关内容，正在重新核对"）/ 收起的（不要提起）/ 已忘记·可找回 / 淡出的。superseded
和模型撤回是内部机制态，永不出卡片；动态流同样过滤它们，同一 reconcile run 的多条
变更聚合成一条摘要（"新记住 2 件事，更新了 1 条"），文案不出现任何内部术语。
源内容编辑/删除前，前端用 `GET /memory/source-impact` 预告"这条内容还支撑着 N 条
记忆"；复核结果通过整理中状态与动态流回流给用户。

tombstone 压制是双保险（P2）：撤回后 reconcile runner 用轻量 LLM 批量把撤回内容
规范化成 `normalized_claim`（主语统一、去修辞、绝对时间、保留否定词），喂 prompt
时以断言替代原文（换措辞也压得住）；false tombstone 的 claim 同时作为声明式向量
文档进索引（`tombstone-<unit_id>`，恢复后随重建自动退场），add 落库前做相似度
兜底拦截（≥0.86 阻断，向量不可用时 fail-open，prompt 压制仍在）。outdated tombstone
不建向量——新证据本就允许它重新成立。claim 同时是 P1 linker 的匹配键。

压制范围与生命周期：用户撤回升为全局压制——用户的意思是"别再记这件事"，不是
"这个桶别记"；跨桶只喂 normalized_claim，私密原文永不进其他桶的 prompt（没有
claim 之前维持桶内，等回填）。模型撤回保持桶内。outdated 墓碑在同桶出现同断言的
active unit 后自动退场（信念合法重建，墓碑功成身退）；false 墓碑不过期，喂 prompt
截断时排在 outdated 前面。

换 embedding 模型跑一次 `scripts/calibrate_embedding_gate.py`：固定标注探针集算
相似度，Youden J 扫出建议阈值并按 embedding 配置哈希存入 meta。分位（P75）门槛
不再做——打分已收敛到五个离散锚点，分位数在离散分布上会跳变，失去意义。
回复延迟先测后调：chat 回复链路逐段计时（web 门控 → 查询改写 → 记忆读取 →
回复 LLM），以 `reply_latency` 事件落日志。`reconcile_bucket` 拆分维持冻结。
