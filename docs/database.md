# 数据库设计

SQLite 是本地业务与记忆的持久化真相源；embedding 向量同样存在 SQLite 里（`vector_index_items` 的 BLOB 列），丢失时可随时重嵌重建。日程是例外：Exchange / Outlook 是真相源，SQLite 中的 `schedule_events` 只是 Graph 读取缓存。

## 业务表

日常功能的数据：

- `posts`、`comments`：公开帖与评论
- `chat_threads`、`chat_messages`：私聊
- `attachments` 及三类关系表：图片附件
- `souls`：AI 人格
- `goals`、`suggestions`：目标与目标建议；`goals.schedule_expectation` 保存可空的每周期望 JSON
- `schedule_events`：Microsoft Graph 日程的本地只读缓存
- `goal_schedule_links`：TraceLog 目标与 Graph 事件的本地链接
- `jobs`、`post_events`：后台任务队列与发帖流水事件
- `vision_cache`：图片理解结果缓存

## 日程表

`schedule_events` 以 Graph event id 为主键：

- `subject`、`body_preview`、`location`、`web_link`：显示内容与 Outlook 深链。
- `start_ts`、`end_ts`：UTC epoch 秒，用于范围查询和排序。
- `start_local`、`end_local`：`Asia/Shanghai` 的原始本地日期时间；`all_day` 标记全天事件。
- `series_master_id`、`is_cancelled`、`change_key`：保留 Graph 事件状态。
- `synced_at`：该缓存行最近一次写入时间；`idx_schedule_events_start` 加速按开始时间读取。

`goal_schedule_links` 以 `(goal_id, event_id)` 为联合主键，并记录 `created_at`。它是 TraceLog 本地领域关系：远端删除 / 取消事件、写穿删除、全量缓存重建时由服务层清理失效链接。

`goals.schedule_expectation` 是可空 JSON，当前只接受 `{"period":"week","target":3,"label":"每周 3 次"}` 这类周目标。周进度由链接事件实时计算，不另存汇总值。

## `meta` 中的 Graph 状态

- `graph.client_id`：Entra Application (client) ID。
- `graph.delta_link`：下一次 calendarView delta query 的游标链接。
- `graph.last_sync_at`：最近一次成功同步的 epoch 秒。
- `graph.window_start`、`graph.window_end`：delta 缓存窗口边界。

OAuth token 不进入 `meta`，只存在权限为 `0600` 的 `workspace/graph_token_cache.json`。退出登录会保留 client ID，清除其他 `graph.*` 同步状态和日程缓存。

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
- `vector_outbox`：待执行的向量嵌入 / 删除操作
- `vector_index_collections`：collection 同步状态
- `vector_index_items`：每个 collection 内已索引的文档及其向量（`dim` + L2 归一化 float32 `embedding` BLOB）；查询用 numpy 精确余弦

只有账本确认 ready 的 collection 才参与语义检索。

## 事务不变量

这四条是数据一致性的底线，改代码时不能破坏：

1. 业务写入与证据事件在同一事务——证据不会漏记。
2. unit 变更、重判结论和 cursor 推进在同一事务——不会消费了证据却丢了结果。
3. LLM 调用永远发生在写事务之外——慢调用不锁库，失败不留半截数据。
4. 证据链接不允许跨桶——隐私边界在数据库层就守住。
