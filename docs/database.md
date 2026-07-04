# 数据库设计

SQLite 是业务和 memory-v2 的唯一持久化真相源。

## 业务表

- `posts`、`comments`
- `chat_threads`、`chat_messages`
- `attachments` 与三类 attachment 关系表
- `souls`
- `todos`、`goals`、`suggestions`
- `jobs`、`post_events`
- `vision_cache`

## memory-v2 表

- `memory_ingest_events`：不可变 evidence 版本账本
- `memory_reconcile_cursors`：每个 bucket 的消费游标
- `memory_units`：结构化长期信念
- `memory_unit_evidence`：unit 到 evidence 的可追溯链接
- `memory_unit_links`：跨桶 unit 关系（same_fact/contradicts/context_variant），只链接不合并
- `memory_unit_reconcile_queue`：edit/delete 后的重判任务
- `memory_unit_relink_queue`：用户编辑 unit 后的证据重关联任务
- `memory_reconcile_runs`：每次 bucket 对账记录
- `memory_unit_ops`：unit 操作审计
- `memory_views`、`memory_view_units`：画像缓存与成员

## 向量账本

- `vector_docs`：期望向量文档
- `vector_outbox`：待同步操作
- `vector_index_collections`：collection 同步状态

ChromaDB 可由 SQLite 重建。只有账本确认 ready 的 collection 才参与语义检索。

## 事务不变量

- 业务 mutation 与 evidence event 同事务。
- unit ops、review resolution 和 cursor advance 同事务。
- LLM 调用发生在写事务之外。
- evidence link 不允许跨 owner/visibility bucket。
