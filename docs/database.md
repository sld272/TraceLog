# TraceLog 数据库设计

本文档描述当前 `schema.sql` 的主要数据结构、索引策略和初始化约束。SQLite 是 TraceLog 的事实源；ChromaDB 保存统一检索池中的 post、comment 和 chat message 向量索引。

## 1. 初始化与版本

`core.db.init_db()` 是唯一数据库初始化入口：

- 开启 WAL 与 foreign keys。
- 执行 `schema.sql`。
- 校验 FTS5 trigram tokenizer 可用。
- 在 `meta.schema_version` 写入当前版本 `1`。

项目尚未发布，不保留旧 schema 兼容。开发期旧数据如果不符合当前 schema，直接删除 `workspace/state.db` 和 `workspace/chroma_db/` 后重新初始化。

## 2. 核心表分组

### 2.1 公开记录

- `posts`：公开 post 原文，字段包括 `id`、`ts`、`content`、`importance`、`created_at`、`updated_at`。
- `posts_fts`：unicode61 tokenizer，用于英文、数字和一般文本关键词检索。
- `posts_fts_trigram`：trigram tokenizer，用于中文模糊检索。
- `comments`：post 下某个 SOUL 的扁平评论会话流。`seq=0` 是首评，`seq>0` 是用户追问和 SOUL 回复。
- `attachments`：本地图片附件元信息，包括路径、MIME、尺寸、大小、sha256 和链接时间。
- `post_attachments` / `comment_attachments` / `chat_message_attachments`：把附件分别挂到公开 post、评论消息和私聊消息。

公开 post 是全局检索、轻反思、TodoTool 和全局深反思的主要证据来源。

### 2.2 评论会话与私聊

- `comments(post_id, soul_name, seq)`：绑定到某条 post 与某个 SOUL 的后续评论会话，不再有独立 thread 容器。
- `chat_threads` / `chat_messages`：某个 SOUL 的一对一私聊。

评论会话和私聊消息不进入 FTS5，但会进入 ChromaDB 统一检索池。私聊消息只允许当前 SOUL 检索；公开评论对话可以作为所有 SOUL 的公开背景，但 prompt 中必须标注说话人。

图片附件当前不进入 FTS5 或 ChromaDB；未启用识图时，只在 LLM 上下文中保留“有图片但不能查看内容”的边界提示。

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
- `meta`：schema version、pending vector docs、pending light reflection、deep reflection cursor 等系统状态。

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

这使同一个 SOUL 的私聊消息与评论追问消息可以用各自自增 id 独立推进，失败时不会丢失待处理消息。`comment_message_id` 指向 `comments.id` 中 `seq>0` 的记录。

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

## 4. 检索池边界

FTS5 仍只索引公开 post，确保关键词检索代表“用户公开写过什么”。ChromaDB 是统一语义检索池，保存 post、公开评论会话和私聊消息。

当前规则：

- 公开 post：进入 SQLite、FTS5、ChromaDB，可被公开回复、私聊、评论追问和全局深反思读取。
- 公开评论会话：进入 SQLite 和 ChromaDB，可被私聊/评论追问作为公开背景检索；不进入全局 `user.md` 深反思。
- 私聊：进入 SQLite 和 ChromaDB，只允许当前 SOUL 检索；不进入公开 post 回复和全局 `user.md` 深反思。
- SOUL 记忆只由该 SOUL 自己的私聊和评论追问消息更新。

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

SOUL 深反思按 SOUL 独立读取 cursor 之后的 raw chat/comment messages，生成：

- `reflections(type='soul_deep')`
- 可能的 `soul_memories/<name>.md` patch
- `soul_memory_revisions`
- `meta.soul_thread_deep_cursor:<soul_name>`

patch evidence 必须是本次输入中的 `chat_message:<id>` 或 `comment_message:<id>`。

## 6. 删除与级联

Schema 使用外键和 trigger 保持主要数据一致性：

- 删除 SOUL 会级联删除该 SOUL 的 comments、chat threads 和 SOUL memory revisions。
- 删除 post 会级联删除 comments、post attachment links、todo source 关联和 FTS 索引；附件文件本身仍由孤儿附件清理逻辑处理。
- FTS trigger 负责公开 post 正文索引同步。

私聊和评论追问消息本身不会被自动提升为全局画像；只有 SOUL 深反思成功并通过 patch gate 后，才会写入对应 SOUL 的长期 Markdown 记忆。
