# TraceLog 数据库设计

本文档描述当前 `schema.sql` 的主要数据结构、索引策略和迁移约束。SQLite 是 TraceLog 的事实源；ChromaDB 只保存公开 posts 的语义向量索引。

## 1. 初始化与版本

`core.db.init_db()` 是唯一数据库初始化入口：

- 开启 WAL 与 foreign keys。
- 执行 `schema.sql`。
- 校验 FTS5 trigram tokenizer 可用。
- 清理旧版中间层表和旧 cursor/meta key。
- 在 `meta.schema_version` 写入当前版本 `3`。

旧版中间层已经从当前 schema 中移除。新 workspace 不会创建这些表；已有 workspace 在下一次 `init_db()` 时会删除旧表、旧 trigger 和旧 meta cursor。

## 2. 核心表分组

### 2.1 公开记录

- `posts`：公开 post 原文，字段包括 `id`、`ts`、`content`、`importance`、`created_at`、`updated_at`。
- `posts_fts`：unicode61 tokenizer，用于英文、数字和一般文本关键词检索。
- `posts_fts_trigram`：trigram tokenizer，用于中文模糊检索。
- `comments`：每个启用 SOUL 对公开 post 的首条评论。

公开 post 是全局检索、轻反思、TodoTool 和全局深反思的主要证据来源。

### 2.2 评论线程与私聊

- `comment_threads` / `comment_messages`：绑定到某条 post 与某个 SOUL 的后续评论线程。
- `chat_threads` / `chat_messages`：某个 SOUL 的一对一私聊。

线程消息不进入 FTS5 / ChromaDB。它们只在当前线程回复时按顺序加载，并在 SOUL 深反思时作为该 SOUL 的 raw evidence 读取。

### 2.3 派生结构化信号

- `entities`
- `post_entities`
- `emotions`
- `events`
- `relations`
- `relations_log`

这些表由轻反思维护，只来自公开 posts。轻反思重跑同一 post 时会先撤销旧派生行，再写入本次解析结果，避免重复累计。

### 2.4 长期记忆与审计

- `reflections`：保存全局深反思与 SOUL 深反思正文，`related_posts` 字段保存本次证据 id 列表的 JSON。
- `user_md_revisions`：保存每次 `user.md` 写入的完整快照、patch、source 和时间。
- `soul_memory_revisions`：保存每次 `soul_memories/<name>.md` 写入的完整快照、patch、source 和时间。

用户手动覆盖长期记忆时同样写 revision：`user.md` 使用 `source='user'` 与 `{"op":"overwrite_user_memory"}`；`soul_memories/<name>.md` 使用 `source='user'` 与 `{"op":"overwrite_soul_memory"}`。用户写入不经过 AI patch gate。

### 2.5 SOUL、待办与系统状态

- `souls`：SOUL 文件路径、启用状态、排序、描述。
- `todos`：公开 post 抽取出的待办。
- `meta`：schema version、pending embedding、pending light reflection、deep reflection cursor 等系统状态。

### 2.6 API 后台任务与事件

- `jobs`：API 公开发帖 pipeline 的后台任务状态，字段包括 `type`、`status`、`payload_json`、`attempts`、`error` 与执行时间戳。
- `post_events`：面向前端和 SSE 的公开 post 事件流，记录 post 创建、embedding、SOUL 回复、TodoTool、轻反思和深反思触发状态。

`jobs.status` 当前使用 `pending` / `running` / `succeeded` / `failed` / `cancelled`。第一版 API worker 单并发领取 pending job，SOUL fanout 内部仍可并发调用多个 SOUL。
P1 以后手动全局/SOUL 深反思也复用 `jobs`，但不一定产生 `post_events`，因为它们不绑定单条公开 post。

当前 SOUL 深反思 cursor 使用 `meta.soul_thread_deep_cursor:<soul_name>`，value 是 JSON：

```json
{
  "chat_message_id": 12,
  "comment_message_id": 8
}
```

这使同一个 SOUL 的私聊消息与评论线程消息可以用各自自增 id 独立推进，失败时不会丢失待处理消息。

## 3. FTS5 external content

`posts_fts` 与 `posts_fts_trigram` 都使用 external content 模式：

```sql
content='posts', content_rowid='rowid'
```

这样 FTS 表只保存倒排索引，不保存正文副本。优点：

- 磁盘占用更小。
- 正文唯一真相在 `posts.content`。
- 通过 trigger 自动同步 insert / update / delete。

删除或更新 external content 索引时，trigger 必须向 FTS 表插入特殊的 `'delete'` 指令，并带上旧正文；不能把 FTS 表当普通表直接 `DELETE`。

## 4. 为什么线程消息不进检索池

私聊与评论线程包含大量寒暄、追问、局部上下文和 SOUL 自身话术。如果混入公开 post 的 FTS/ChromaDB，会污染“我过去发过什么”的检索语义。

当前规则：

- 公开 post：进入 SQLite、FTS5、ChromaDB，可被回复上下文和全局深反思检索/读取。
- 评论线程和私聊：只保留在线程表中；回复时读取当前线程消息；SOUL 深反思读取该 SOUL 的 raw thread messages。
- 全局 `user.md` 深反思不读取私聊或评论线程。
- SOUL 记忆只由该 SOUL 自己的线程消息更新。

## 5. 反思相关持久化

### 5.1 轻反思

轻反思输入：

- 当前 post。
- 当前 post 之前少量公开 posts 作为时间语境。
- 当前 `user.md` 作为已知画像背景。

轻反思输出只写派生结构化表：

- entities / post_entities
- emotions
- events
- relations / relations_log
- posts.importance

轻反思不写长期 Markdown 记忆，也不写中间层记忆。

### 5.2 全局深反思

全局深反思读取自上次全局反思以来的新 posts，连同当前 `user.md` 和 todos，生成：

- `reflections(type='global_deep')`
- 可能的 `user.md` patch
- `user_md_revisions`

profile patch 的 evidence 必须是本次输入中真实存在的 post id。`profile_service` 按 user.md frontmatter 中的 sensitivity 控制阈值：高敏章节更保守，`当前状态与关注` 可用 low 门槛更积极地增删。

### 5.3 SOUL 深反思

SOUL 深反思按 SOUL 独立读取 cursor 之后的 raw thread messages，生成：

- `reflections(type='soul_deep')`
- 可能的 `soul_memories/<name>.md` patch
- `soul_memory_revisions`
- `meta.soul_thread_deep_cursor:<soul_name>`

patch evidence 必须是本次输入中的 `chat_message:<id>` 或 `comment_message:<id>`。

## 6. 删除与级联

Schema 使用外键和 trigger 保持主要数据一致性：

- 删除 SOUL 会级联删除该 SOUL 的 comments、chat/comment threads 和 SOUL memory revisions。
- 删除 post 会级联删除 comments、comment_threads、todo source 关联和 FTS 索引。
- FTS trigger 负责公开 post 正文索引同步。

线程消息本身不会被自动提升为全局画像；只有深反思成功并通过 patch gate 后，才会写入长期 Markdown 记忆。
