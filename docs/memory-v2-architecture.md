# Memory v2 架构（已实现状态）

本文件是 memory-v2 **已实现**部分的权威说明，反映代码现状（feat/memory-v2 分支）。
与旧设计文档（memory-v2-design / state-goals-suggestions）冲突时，以本文件为准；
记忆边界（"人格不串戏"）的产品定义以 `memory-v2-defense-roadmap.md` 的「记忆边界语义基线」一节为准。

## 与 legacy 的关系：两个开关，默认零影响

memory-v2 由两个环境变量控制，默认都是 legacy，行为与旧版完全一致：

- `MEMORY_V2_WRITE_MODE=reconcile`：写路用事件驱动的 unit reconcile 替换 legacy 的 light/deep markdown 反思。
- `MEMORY_V2_READ_MODE=units`：回复时从 unit/view 组装记忆；`units_and_freshness` 在此基础上额外注入尚未沉淀的近期原始证据。

两者独立可开。未开启时不读写任何 unit，旧 `user.md` / `soul_memories` 路径不变。

## 三层数据模型

记忆是一条单向 DAG：raw evidence → memory units → views（画像）。下层是上层的唯一来源，上层不持有独立真相，因此不会漂移。

1. **Evidence ledger**（`memory_ingest_events`）：每次对 post / comment / chat 的 create/edit/rerun/delete 都 append 一条不可变事件，冻结内容快照与可见性边界。自增 `id` 同时充当 per-bucket 的消费游标。
2. **Memory units**（`memory_units`）：关于【用户】的结构化信念（identity / preference / goal / state / relationship / insight / freeform），绑定到 evidence 事件版本而非可变业务行。带 confidence（信不信为真）与 importance（值不值得记）两个正交维度。
3. **Views**（`memory_views`）：core 子集的低频综合画像（用户画像 `user_md`、各 SOUL 私聊画像 `soul_private_memory`），作为恒在的身份底色注入。

证据按 `(owner_scope, visibility_scope)` 分桶：原贴 `global/public`、评论 `soul:<name>/thread:<post_id>`、私聊 `soul:<name>/private:soul:<name>`。

## 写路：reconcile

开启 `reconcile` 写模式后，用户每次写入（post/comment/chat）入队一个**去重的全局 reconcile job**（`enqueue_memory_reconcile_once`），后台 worker 领取后：

1. `run_pending_reconcile` 在单次有界 pass 中遍历最多 500 个未消费 bucket，每桶最多消费 200 条事件。
2. 每桶：取游标后的当前版本用户证据 + active/challenged units + pending unit reviews + 墓碑 → LLM 产出 add/retain/confirm/revise/retract ops → 在一个事务内 apply、resolve reviews、推进游标。
3. `refresh_views_after_reconcile` 重综合 stale/missing 的画像（hash 门控，core 集未变则跳过）。
4. `rebuild_expected_docs` 把 active units 同步为 `unit` 向量文档。
5. 若批次上限后仍有未消费证据，当前 job 入队一个去重的 continuation job，由下一轮继续消费。

关键正确性保证：

- **失败不丢证据**：LLM 调用失败（超时/报错/非法 JSON）抛 `ReconcileProducerError`，游标不推进；runner 继续处理其他 bucket，最终汇总失败并让当前 job 进入自动重试。只有成功解析（含合法空结果）才推进游标。
- **有界续跑**：单个 job 不无限占用 worker；超过每桶/每轮上限的积压由去重 continuation job 接管。达到最大重试次数后，失败 job 保留为 `failed`，对应 cursor 仍停在失败证据之前，等待手动重试或后续写入触发新 job。
- **并发安全**：提交事务内对游标做 CAS 重检；若另一 runner 已推进则放弃本次提交，避免重复 unit。
- **版本安全**：只有 source 的最新 revision 能生成或支撑 unit；LLM 调用期间 source 再次 edit/delete 时，source revision CAS 会拒绝旧结果。
- **画像生命周期**：reconcile 提交后 recompute slice + 标记受影响 view 为 stale，由后续重综合刷新；读侧缺失/stale 时有兜底（见下）。

legacy 的 light/deep 反思在 reconcile 写模式下不再入队。

### edit/delete：challenge 与重判

用户 edit/delete 时，业务 mutation 与以下动作在同一事务提交：

1. append 新 revision 事件；
2. 反查引用该 source 任一历史 revision 的 reflected/migrated units；
3. 将 active unit 立即改为 `challenged`，写入 `memory_unit_reconcile_queue`；
4. 将相关画像标 stale。

`challenged` unit 不进入普通检索、画像或 unit 向量文档。Reconcile 必须对每个 pending unit 给出且只给出一个决定：

- `retain`：剩余 evidence 仍完整支持，原样恢复 active；
- `confirm`：最新版本继续支持，绑定当前有效 evidence；
- `revise`：按当前 evidence 更新结论；
- `retract`：当前 evidence 已不支持。

delete 后有效 evidence 已归零时由代码确定性 retract，不调用 LLM；其余 delete 与所有 edit 交给 LLM。Edit 的最新内容同时作为普通新 evidence，可独立 `add` 新 unit。漏答、重复决定、非法 evidence 引用或并发 revision 变化都会使整批回滚，unit 保持 challenged。

帖子支持 `PATCH /posts/{post_id}` 编辑文本。删帖会在级联删除前为所有评论补 delete events；评论删除、chat 编辑及其级联删除同样触发 challenge 和 reconcile。

## 读路：分层注入

开启读模式后，回复 prompt 的记忆块由 `build_memory_section` 分层组装：

1. **[基线认知]**：用户画像（`user_md` view，去除生成头）。缺失/stale 时退化为基于 core units 的确定性模板，再退化为 legacy `user.md`，身份底色绝不消失。
2. **[私聊画像]**（仅私聊场景）：该 SOUL 的 `soul_private_memory` view。
3. **[当前状态]**：近期 active `state` units（recency×importance，7 天窗口）。
4. **[相关记忆]**：与当前 query 相关的 units，作为去噪的主题锚点逐条列出。
5. **[相关对话原文]**：对每个命中 unit，定位其最相关证据所在的对话单元，**整段召回原文**——原贴 + 当前回复 SOUL 在该贴的评论线 + 命中所在 SOUL 的评论线；私聊命中则召回该 SOUL 的近期私聊片段（公开场景标记自审）。多命中同一对话单元去重，套宽松字数预算 + 消息边界智能截断。对话单元的定位用「关键词重叠 → 时效」在该 unit 证据集内选取，不用向量。
6. **[尚未稳定沉淀的原始证据]**（仅 `units_and_freshness`）：合并游标后的近期用户证据与 pending challenged unit 的当前有效 evidence。后者即使早于 cursor 也可作为 raw fallback；被 edit 取代或 delete 的旧版本绝不注入。

公开回复的「SOUL 相处记忆」块在 v2 下完全由上述分层组装，不再注入 legacy 整块 `soul_memory`（旧文件把私聊记忆无判断地带进公开回复）。

### 检索：关键词 + 语义混合

`retrieve_units` 先用 SQL 按 scope/状态/类型圈定候选（scope 边界在此强制），再混合两路相关性：关键词重叠（含中文 2-gram）+ 向量 ANN（`unit` 向量文档）。一个 unit 至少命中一路才注入（最低命中门，避免无关记忆按重要度兜底刷屏）。向量索引不可用或 query 为空时退化为纯关键词。ANN 结果始终与 scope 过滤后的 SQL 候选取交集，不会扩大可见性。

设计分工：**向量只负责"找对主题"——把又长又乱、不适合直接 embedding 的 raw evidence 化简成短而干净的 unit，再对 unit 做 ANN 跨主题召回**。命中主题后，"挑哪条证据"是该 unit 证据子集内的小集合排序（关键词+时效），evidence 自始至终不进 embedding。于是 unit 承担"画像抽象 + 检索锚点"，命中后按**对话单元整段召回** raw evidence 原文作为保真细节（见 [相关对话原文]）。画像（基线认知/私聊画像）继续全量注入 unit 综合，不挂 evidence。

### 边界：人格不串戏

- 公开记忆（原贴 + 所有评论 thread）跨帖、跨人格共享可读。
- 每个 SOUL 的私聊只有该 SOUL 可读；别的 SOUL 的私聊永不返回。
- SOUL 可读到自己的私聊记忆，但在公开场景标记为「私密·谨慎」，由提示词让模型自行判断是否说出（不是硬过滤、不是持久隐私字段）。
- 检索结果带归属（「用户在 X 的评论区」），避免把别人的对话当成对自己说的。

## goal 与可解释

- **goal**：作为 `type=goal` 的 unit 经通用 reconcile 沉淀，进入画像「目标」章节与检索；`list_goals` 读取当前在追踪的目标。生命周期走 unit 状态机：达成/放弃由后续 reconcile retract（active→retracted）。
- **evidence hydration**：`unit_detail(unit_id)` 返回 unit + raw evidence，并标记 `current/superseded/deleted`，支撑「为什么系统这么认为」的可解释与未来工作台下钻。

## 迁移

评论归属迁移（`_migrate_comment_event_ownership`）把 legacy `global` 的用户评论事件改归 `soul:<name>`，并清理迁移前可能残留的 `(global, thread:*)` 孤立 units/cursors，让新 soul bucket 从头 reconcile。global+public 用户画像桶不受影响；幂等。

`memory_v2_rechallenge_v1` 幂等扫描修复旧版本已经越过 edit/delete cursor 的 active units：按 source 最新 revision 补 challenge/review，不回退 cursor；启动时发现 pending reviews 会自动入队 reconcile。

## 已知未完成

按优先级/价值排列，便于后续接手。

**工作台（最高价值，答辩演示用）**

- 前端记忆工作台 UI：画像（view）→ 支撑 unit → raw evidence 三层下钻；展示 view fresh/stale + 手动重综合；展示 challenged/pending 重判与 freshness seam。后端 `unit_detail`（带 evidence 的 `current/superseded/deleted` 标记）已就绪。
- 工作台后端从编辑 md 改为编辑 unit：`memory_review_service` 目前仍只读写 legacy `user.md` / `soul_memories`；需要 `create_unit`（user_authored）/ `update_unit`（reflected→新建 user_authored + supersede）/ `set_profile_policy` / `set_prompt_policy` 等面向用户的 API（`retract_unit` 已存在但仅 reconciler 用）。v2 启用后旧 md 编辑入口应禁用或标只读，避免「保存成功但回复不读」。

**写路/整理增强**

- 每帖增量对账（trickle reconcile）：当前是按 bucket 批量 reconcile + 读路 freshness seam 兜新鲜度；trickle 是 memory-v2-design §3.1 的目标形态，未实现。
- decay / 第三档重组：unit 的 `dormant` 衰减、低频合并压缩、按存活轮数的稳定缓冲，均未实现（`status='dormant'`、`retrieval_count` 列已预留但不写）。
- goal 的显式「达成 vs 放弃」区分：当前用 unit active/retracted 表达「在追踪/不再追踪」；独立 goaltool / 用户确认的建议机制（见 state-goals-suggestions 设计）未实现。
- 用户删除分意图（false/outdated/不进画像/回复禁用）、user_authored 对账免疫的完整 UX：schema 已支持（`retraction_reason`、`source`、`prompt_policy`、`profile_policy`），但缺面向用户的入口。

**读路/检索增强**

- `retrieve_units` 多信号打分（语义+关键词+实体+时间+importance）：当前是关键词重叠 + 向量 ANN 两路混合的 MVP。
- `in_md_slice` 成员在检索中「降权而非硬排除」：当前硬排除（`in_md_slice = 0`）。
- 物理遗忘 / raw scrub：彻底擦除 source/event/unit/vector/view/revision 的高风险流程未实现；当前删除是逻辑层 challenge + retract，证据链保留。

**其他**

- 旧 README/architecture/overview/database 仍主要描述 legacy 模型，已各加 v2 指针；本文件为 v2 权威来源。
- 答辩 demo 数据与脚本。
