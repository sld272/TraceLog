# memory-v2 实施进度与记忆边界基线

> 本文原为答辩前的施工清单，现收敛为两部分：①实施进度总结；②「记忆边界语义基线」（产品定义，被 [memory-v2-architecture.md](./memory-v2-architecture.md) 引用为权威）。
> 已实现部分的技术说明以 [memory-v2-architecture.md](./memory-v2-architecture.md) 为准；**尚未完成的事项见该文件「已知未完成」一节**（不再在本文重复维护开工前的模块清单）。

## 实施进度（feat/memory-v2，未推送）

已完成并提交：

- **主干闭环**：reconcile 失败语义三态（不丢证据）、reconcile 接后台 job（写模式 flag）、view stale→重综合生命周期、v2 读模式注入画像 + 兜底、reply 三入口替换 legacy soul_memory + 归属。
- **读路**：freshness seam（`units_and_freshness` 真正实现）、chat/comment 真实 query + 最低命中门、命中按对话单元整段召回原文（去重 + 预算 + 智能截断）。
- **检索**：unit 向量 ANN 与关键词混合（语义召回 + 关键词精确；scope 由 SQL 候选保证；索引不可用时退化关键词）。
- **生命周期**：edit/delete 精确 challenge 受影响 unit、持久重判队列、source revision CAS、challenged evidence raw fallback、历史脏 unit 幂等修复。
- **健壮性**：并发 reconcile 的 cursor CAS、有界续跑 continuation job、人格不串戏边界回归测试。
- **可解释**：unit→raw evidence hydration（`unit_detail`，带 current/superseded/deleted 标记）。
- **goal**：`list_goals` 目标线呈现（生命周期走 unit active/retracted）。
- **迁移**：评论归属迁移清理 `(global, thread:*)` 孤立 units/cursors。
- **文档**：权威 v2 架构文档 + README/architecture/database/overview 指针。

两个开关均默认 legacy（零影响）：`MEMORY_V2_WRITE_MODE=reconcile`、`MEMORY_V2_READ_MODE=units|units_and_freshness`。

剩余待办见 [memory-v2-architecture.md](./memory-v2-architecture.md) 「已知未完成」（工作台 UI/编辑、trickle、decay/重组、goaltool、多信号打分、物理遗忘、demo 脚本）。

## 记忆边界语义基线（产品定义，已与代码核对一致）

事件按 `(owner_scope, visibility_scope)` 分桶（`memory_events_service.py`）：

- 原贴：`owner=global` / `visibility=public` —— 用户的公开记忆。
- 评论：`owner=soul:<该 thread 的 soul>` / `visibility=thread:<post_id>` —— 公开人格记忆。
- 私聊：`owner=soul:<name>` / `visibility=private:soul:<name>` —— 私密记忆。

读取边界（`scope_policy` + `_allowed_visibility_sql` 的现有行为）：

- soul 在帖子/评论回复时，**可检索所有公开记忆，含别的人格的公开评论（跨帖、跨人格）**——这是 feature，不是越界。
- soul 可检索**自己**的私密记忆，但通过提示词自主判断公开场景下是否说出（`SOFT`/discretion，记忆照常进 prompt，不过滤、不硬墙）。
- 别的 soul 的私密记忆永不可读（SQL 只 admit `private_self`）。
- `owner_scope` 的正确用途是**归属标签**（"这是用户在帖 X 对 soul_B 说的"），**不是读取过滤条件**（私密隔离除外）。

> 这套定义主动推翻了旧设计文档里"thread 仅当前帖、私密硬墙"的两条硬约束：公开内容跨帖跨人格共享是既定产品决策。给读路做改造时，回归测试需守住「没有意外收紧公开、也没有放开别人的私密」，而非去收紧 thread / owner_scope。
