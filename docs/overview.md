# TraceLog 系统概览

TraceLog 由四条主链组成：

1. **表达链**：公开 post、评论和私聊写入 SQLite。
2. **回应链**：query rewrite → 混合检索 → 上下文组装 → SOUL 回复。
3. **记忆链**：evidence event → reconcile → memory unit → portrait view。
4. **派生链**：Todo/Goal、Vision、向量索引和运行日志。

## 边界模型

记忆使用两个正交维度：

- `owner_scope`：`global` 或 `soul:<name>`
- `visibility_scope`：`public`、`thread:<post_id>`、
  `private:soul:<name>`

公开 post 进入 `global/public`；评论进入对应 SOUL 的 thread bucket；私聊进入对应
SOUL 的 private bucket。数据库边界校验、检索策略和 prompt 组装共同保证不会串人格。

## 真相源

- 原始表达：posts/comments/chat_messages
- evidence 真相：memory_ingest_events
- 长期信念：memory_units
- 画像缓存：memory_views
- 正式目标：goals
- 正式待办：todos
- 向量账本：vector_docs/vector_outbox

SOUL Markdown 只定义人格，不存储用户记忆。
