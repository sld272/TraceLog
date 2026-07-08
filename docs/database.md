# 数据库设计

SQLite 是唯一的持久化真相源；ChromaDB 向量索引随时可以由 SQLite 重建。

## 业务表

日常功能的数据：

- `posts`、`comments`：公开帖与评论
- `chat_threads`、`chat_messages`：私聊
- `attachments` 及三类关系表：图片附件
- `souls`：AI 人格
- `todos`、`goals`、`suggestions`：待办 / 目标 / 建议
- `jobs`、`post_events`：后台任务队列与发帖流水事件
- `vision_cache`：图片理解结果缓存

## 记忆表（memory-v2）

- `memory_ingest_events`：证据账本。每次输入（含编辑、删除）追加一条不可变事件，带版本号，永不修改。
- `memory_reconcile_cursors`：每个桶消费到了哪条证据。
- `memory_units`：长期信念本体。
- `memory_unit_evidence`：unit ↔ 证据的可追溯链接（"这条记忆是从哪几句话来的"）。
- `memory_unit_links`：跨桶 unit 关系（same_fact / contradicts / context_variant），只链接不合并。
- `memory_unit_reconcile_queue`：源内容编辑/删除后待重判的 units。
- `memory_unit_relink_queue`：用户编辑 unit 后待重挂的证据。
- `memory_reconcile_runs`：每次对账的运行记录。
- `memory_unit_ops`：所有 unit 操作的审计日志（谁、何时、改了什么）。
- `memory_views`、`memory_view_units`：画像缓存及其成员。
- `meta`：各维护 pass 的游标和门控时间戳。

## 向量账本

- `vector_docs`：期望存在的向量文档清单
- `vector_outbox`：待同步到 ChromaDB 的操作
- `vector_index_collections`：collection 同步状态

只有账本确认 ready 的 collection 才参与语义检索。

## 事务不变量

这四条是数据一致性的底线，改代码时不能破坏：

1. 业务写入与证据事件在同一事务——证据不会漏记。
2. unit 变更、重判结论和 cursor 推进在同一事务——不会消费了证据却丢了结果。
3. LLM 调用永远发生在写事务之外——慢调用不锁库，失败不留半截数据。
4. 证据链接不允许跨桶——隐私边界在数据库层就守住。
