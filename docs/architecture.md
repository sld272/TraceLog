# TraceLog 项目架构设计

本文档描述当前 TraceLog 的技术架构：记忆系统分层、数据存储布局、回复上下文、反思器、SOUL 边界和后续扩展方向。

---

## 1. 总体架构

TraceLog 是“向内的 AI 社交媒体”：用户用公开 post、评论线程和私聊表达自己；系统用多个 SOUL 回应，并把长期有价值的线索沉淀进 `user.md` 与 `soul_memories/<name>.md`。

当前主链路刻意保持简单：

```
raw evidence
  -> retrieval context
  -> reply
  -> light reflection structured signals
  -> deep reflection reconcile
  -> long-term memory
```

这里没有可检索中间层记忆。公开回复上下文直接使用 raw related posts；深反思直接读取 raw posts 或 raw thread messages，对长期 Markdown 记忆做 confirm / revise / retract / add。后续如果重新引入中间层，需要重新设计为真正可更新、可删除、可去重、可审计的 memory unit。

## 2. 记忆分层

### 2.1 Persona & Soul Memory

存储：

- `souls/*.md`
- `soul_memories/*.md`
- `state.db.souls`
- `state.db.soul_memory_revisions`

职责：

- `souls/*.md` 定义每个 AI 好友的语气、边界和人格。
- `soul_memories/<name>.md` 记录该 SOUL 对用户的独立理解和互动约定。
- 每个 SOUL 被调用时注入自己的人格与相处记忆。
- SOUL 记忆只由该 SOUL 的评论线程和私聊深反思更新。

### 2.2 User Profile

存储：

- `user.md`
- `state.db.user_md_revisions`

职责：

- 存储用户长期画像、身份、偏好、近期趋势和成长线索。
- 章节按 sensitivity 分级，`high` 章节更保守。
- 全局深反思维护它，用户也可以直接覆盖编辑。
- 全局深反思只读取公开 posts，不读取私聊和评论线程。

### 2.3 Structured Evidence

存储：

- `posts` / `comments`
- `chat_threads` / `chat_messages`
- `comment_threads` / `comment_messages`
- `entities` / `post_entities`
- `emotions`
- `events`
- `relations` / `relations_log`
- `todos`
- `reflections`
- `meta`

职责：

- `posts` 是公开表达和全局反思的主要证据。
- `comments` 是每个 SOUL 对公开 post 的首条回复。
- thread messages 是私聊/评论线程的局部对话证据，只服务当前线程和 SOUL 深反思。
- entities/emotions/events/relations 是轻反思从公开 post 派生的结构化信号。
- todos 只从公开 post 抽取。

## 3. 存储布局

```
workspace/
├── state.db
├── chroma_db/
├── user.md
├── souls/
│   ├── 默认.md
│   └── 毒舌好友.md
└── soul_memories/
    ├── 默认.md
    └── 毒舌好友.md
```

| 数据 | 存储位置 | 进 prompt | 维护者 |
| --- | --- | --- | --- |
| SOUL 人格正文 | `souls/<name>.md` | 该 SOUL 被调用时注入 | 用户 / 默认库 |
| SOUL 相处记忆 | `soul_memories/<name>.md` | 该 SOUL 被调用时注入 | SoulMemoryService / 用户 / SOUL 深反思 |
| 用户档案 | `user.md` | 回复与深反思上下文 | ProfileService / 用户 / 全局深反思 |
| 公开 post | `posts` + FTS5 + ChromaDB | 按检索结果注入 | RecordService |
| 首条 AI 评论 | `comments` | 按相关帖子和 SOUL 注入 | ReplyService |
| 私聊消息 | `chat_messages` | 当前线程消息序列 | ChatService |
| 评论线程消息 | `comment_messages` | 当前线程消息序列 | CommentService |
| 轻反思信号 | entities / emotions / events / relations | 不直接注入 | Reflector |
| 深反思记录 | `reflections` | 不默认注入 | Reflector |
| 待办 | `todos` | TodoTool 开启时注入活跃项 | TodoService |
| revision 审计 | `user_md_revisions` / `soul_memory_revisions` | 不注入 | Profile/SoulMemory service |

## 4. 回复上下文

### 4.1 公开 post 回复

公开 post 回复前先对用户输入做 hybrid search：

```
user input
  -> query rewrite（可选）
  -> FTS5 + ChromaDB hybrid search
  -> related post ids
  -> context_builder.build_context
```

`build_context()` 当前只组装：

- `# 用户档案`
- `# 相关帖子`
- `# 待办事项`

明确不再注入：

- 最近 3 条帖子。
- 中间层记忆摘要。
- 每个 SOUL 的私有相处记忆片段。

SOUL 自己的人格和 `soul_memories/<name>.md` 仍由 reply router 在该 SOUL 调用时注入。

### 4.2 私聊回复

私聊上下文包含：

- 当前 SOUL 人格与相处记忆。
- `user.md`。
- hybrid search 命中的 raw related posts。
- 当前 SOUL 对相关 posts 的历史评论。
- 当前 thread 的最近消息序列。
- 活跃 todos（工具开启时）。

私聊消息不写 posts，不进 FTS/ChromaDB，不触发轻反思，不直接更新全局 `user.md`。

### 4.3 评论线程回复

评论线程上下文包含：

- 当前 SOUL 人格与相处记忆。
- `user.md`。
- 原始 post。
- hybrid search 命中的其他 raw related posts。
- 当前 SOUL 对相关 posts 的历史评论。
- 当前 SOUL 的首条回复。
- 当前 comment thread 的最近消息序列。
- 活跃 todos（工具开启时）。

当前 post 本身会从 related post ids 中排除，避免重复注入。

## 5. 反思器

### 5.1 轻反思

触发：每条公开 post 保存后。

输入：

- 当前 post。
- 当前 post 之前少量公开 posts，作为时间语境。
- 当前 `user.md`。

输出：

- entities / post_entities
- emotions
- events
- relations / relations_log
- posts.importance

轻反思不修改 `user.md`，不修改 `soul_memories`，不写可检索中间层记忆。

### 5.2 全局深反思

触发：CLI 退出或手动触发。

输入：

- 自上次全局深反思以来的新 raw posts。
- 当前 `user.md`。
- 当前 todos。

输出：

- `reflections(type='global_deep')`
- 可选 `user.md` patch
- `user_md_revisions`

核心要求：

- 深反思不是追加日志，而是对账。
- 既有画像被支持时 confirm，通常不写 patch。
- 既有画像被细化时 update。
- 既有画像过时、重复、被推翻或无意义时 remove。
- 确实没有承载位置时 add。
- patch evidence 必须是本次输入中的 post id。

### 5.3 SOUL 深反思

触发：CLI 退出或手动触发。

输入：

- 当前 SOUL 的 persona。
- 当前 `soul_memories/<name>.md`。
- 该 SOUL cursor 之后的 raw chat/comment thread messages。

输出：

- `reflections(type='soul_deep')`
- 可选 `soul_memories/<name>.md` patch
- `soul_memory_revisions`
- `meta.soul_thread_deep_cursor:<soul_name>`

SOUL 深反思只更新当前 SOUL 的相处记忆，不推断其他 SOUL 也知道这些内容。

## 6. Evidence Boundaries

| 来源 | 可进入全局 `user.md` | 可进入 SOUL 记忆 | 可进入检索池 |
| --- | --- | --- | --- |
| 公开 post | 是，经全局深反思 patch gate | 间接可作为公开背景 | 是，FTS5 + ChromaDB |
| 首条 AI 评论 | 否 | 可作为相关评论背景 | 否 |
| 评论线程消息 | 否 | 仅对应 SOUL | 否 |
| 私聊消息 | 否 | 仅对应 SOUL | 否 |
| todos | 可作为全局深反思背景 | 不直接写 SOUL 记忆 | 否 |

这个边界保证：

- 私聊不会污染全局画像。
- 评论线程的局部上下文不会被误当成公开事实。
- SOUL 之间不会自动共享私密理解。
- 公开回复不会执行历史证据中的 prompt injection。

## 7. 模块边界

| 模块 | 职责 |
| --- | --- |
| `core/record_service.py` | 保存公开 post、维护 pending embedding、格式化 post |
| `core/retrieval.py` | FTS5 + ChromaDB hybrid search |
| `core/reply_context.py` | query rewrite + hybrid search 的回复上下文共用 helper |
| `core/context_builder.py` | 公开 post 共享上下文组装 |
| `core/reply_service.py` | 多 SOUL fanout 与首条评论落库 |
| `core/chat_service.py` | 私聊 thread、消息与回复上下文 |
| `core/comment_service.py` | 评论 thread、消息与回复上下文 |
| `core/reflector.py` | 轻反思、全局深反思、SOUL 深反思 |
| `core/profile_service.py` | `user.md` 初始化、解析、patch gate、revision |
| `core/soul_memory_service.py` | SOUL 相处记忆初始化、patch、revision |
| `core/llm/*_router.py` | LLM prompt、JSON parsing、调用边界 |
| `core/db.py` | SQLite 初始化、事务、legacy cleanup、查询 helper |

## 8. 后续中间层记忆方向

当前选择完全移除旧中间层，是为了避免半成品抽取层同时承担检索、权限、反思、清理等过多职责。重新设计时至少要满足：

- 单位清晰：到底是 fact、pattern、preference、relationship、episode 还是 hypothesis。
- 可更新：不是只 append；必须能 merge、revise、retract、archive。
- 证据明确：每个条目能追溯到 raw evidence，并能区分公开、私聊、评论线程边界。
- 生命周期明确：短期信号、长期记忆、趋势观察不能混成一种表。
- 读路径明确：回复、全局深反思、SOUL 深反思分别允许读取哪些层。
- 用户控制明确：用户编辑长期 Markdown 记忆时，系统如何处理冲突和旧条目。

在这层重新设计完成前，回复上下文和深反思都优先使用 raw evidence，牺牲一点 token 成本，换取语义和权限边界更可靠。
