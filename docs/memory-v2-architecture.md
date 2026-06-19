# Memory v2 架构（已实现状态）

本文件是 memory-v2 **已实现**部分的权威说明，反映代码现状（feat/memory-v2 分支）。
与旧设计文档（memory-v2-design / mvp-design / state-goals-suggestions）冲突时，以本文件为准；
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

1. `run_pending_reconcile` 遍历每个有未消费证据的 bucket，逐桶 reconcile。
2. 每桶：取游标后的用户证据 + 桶内 active units + 墓碑 → LLM 产出 add/confirm/revise/retract ops → 在一个事务内 apply + 推进游标。
3. `refresh_views_after_reconcile` 重综合 stale/missing 的画像（hash 门控，core 集未变则跳过）。
4. `rebuild_expected_docs` 把 active units 同步为 `unit` 向量文档。

关键正确性保证：

- **失败不丢证据**：LLM 调用失败（超时/报错/非法 JSON）抛 `ReconcileProducerError`，游标不推进，该批证据下次重试。只有成功解析（含合法空结果）才推进游标。
- **并发安全**：提交事务内对游标做 CAS 重检；若另一 runner 已推进则放弃本次提交，避免重复 unit。
- **画像生命周期**：reconcile 提交后 recompute slice + 标记受影响 view 为 stale，由后续重综合刷新；读侧缺失/stale 时有兜底（见下）。

legacy 的 light/deep 反思在 reconcile 写模式下不再入队。

## 读路：分层注入

开启读模式后，回复 prompt 的记忆块由 `build_memory_section` 分层组装：

1. **[基线认知]**：用户画像（`user_md` view，去除生成头）。缺失/stale 时退化为基于 core units 的确定性模板，再退化为 legacy `user.md`，身份底色绝不消失。
2. **[私聊画像]**（仅私聊场景）：该 SOUL 的 `soul_private_memory` view。
3. **[当前状态]**：近期 active `state` units（recency×importance，7 天窗口）。
4. **[相关记忆]**：与当前 query 相关的 units，带归属标注与 discretion 标记。
5. **[最近动态·尚未整理]**（仅 `units_and_freshness`）：游标之后、尚未被 reconcile 消费的近期用户证据，按 3 天窗口 + 事件/字数预算注入，带截断标记。

公开回复的「SOUL 相处记忆」块在 v2 下完全由上述分层组装，不再注入 legacy 整块 `soul_memory`（旧文件把私聊记忆无判断地带进公开回复）。

### 检索：关键词 + 语义混合

`retrieve_units` 先用 SQL 按 scope/状态/类型圈定候选（scope 边界在此强制），再混合两路相关性：关键词重叠（含中文 2-gram）+ 向量 ANN（`unit` 向量文档）。一个 unit 至少命中一路才注入（最低命中门，避免无关记忆按重要度兜底刷屏）。向量索引不可用或 query 为空时退化为纯关键词。ANN 结果始终与 scope 过滤后的 SQL 候选取交集，不会扩大可见性。

### 边界：人格不串戏

- 公开记忆（原贴 + 所有评论 thread）跨帖、跨人格共享可读。
- 每个 SOUL 的私聊只有该 SOUL 可读；别的 SOUL 的私聊永不返回。
- SOUL 可读到自己的私聊记忆，但在公开场景标记为「私密·谨慎」，由提示词让模型自行判断是否说出（不是硬过滤、不是持久隐私字段）。
- 检索结果带归属（「用户在 X 的评论区」），避免把别人的对话当成对自己说的。

## goal 与可解释

- **goal**：作为 `type=goal` 的 unit 经通用 reconcile 沉淀，进入画像「目标」章节与检索；`list_goals` 读取当前在追踪的目标。生命周期走 unit 状态机：达成/放弃由后续 reconcile retract（active→retracted）。
- **evidence hydration**：`unit_detail(unit_id)` 返回 unit + 其依据的 raw evidence，支撑「为什么系统这么认为」的可解释与未来工作台的下钻。

## 迁移

评论归属迁移（`_migrate_comment_event_ownership`）把 legacy `global` 的用户评论事件改归 `soul:<name>`，并清理迁移前可能残留的 `(global, thread:*)` 孤立 units/cursors，让新 soul bucket 从头 reconcile。global+public 用户画像桶不受影响；幂等。

## 已知未完成

- 前端记忆工作台 UI（unit 编辑、三层下钻、生命周期控制）；后端 hydration（`unit_detail`）已就绪。
- goal 的显式「达成 vs 放弃」区分（当前用 active/retracted 表达「在追踪/不再追踪」）。
- 旧 README/architecture/overview 仍主要描述 legacy 模型；本文件为 v2 权威来源。
