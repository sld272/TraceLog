# TraceLog 项目架构设计

本文档描述当前 TraceLog 的技术架构：记忆系统分层、数据存储布局、回复上下文、反思器、SOUL 边界和后续扩展方向。

---

## 1. 总体架构

TraceLog 是“向内的 AI 社交媒体”：用户用公开 post、post 下的 SOUL 评论对话和私聊表达自己；系统用多个 SOUL 回应，并把长期有价值的线索沉淀进 `user.md` 与 `soul_memories/<name>.md`。

当前主链路刻意保持简单：

```
raw evidence
  -> retrieval context
  -> reply
  -> light reflection structured signals
  -> deep reflection reconcile
  -> long-term memory
```

这里没有可检索中间层记忆。公开回复上下文直接使用 raw related posts；私聊和评论对话使用统一检索池命中的 raw evidence；深反思直接读取 raw posts 或 raw SOUL interaction messages，对长期 Markdown 记忆做 confirm / revise / retract / add。后续如果重新引入中间层，需要重新设计为真正可更新、可删除、可去重、可审计的 memory unit。

## 2. 记忆分层

### 2.1 Soul & Soul Memory

存储：

- `souls/*.md`
- `soul_memories/*.md`
- `state.db.souls`
- `state.db.soul_memory_revisions`

职责：

- `souls/*.md` 定义每个 AI 好友的语气、边界和人格。
- `soul_memories/<name>.md` 记录该 SOUL 对用户的独立理解和互动约定。
- 每个 SOUL 被调用时注入自己的人格与相处记忆。
- SOUL 记忆只由该 SOUL 的评论对话和私聊深反思更新。

### 2.2 User Profile

存储：

- `user.md`
- `state.db.user_md_revisions`

职责：

- 存储用户长期画像、身份、偏好、长期目标和当前状态。
- 章节按 sensitivity 分级：`high` 更保守，`low` 用于「当前状态与关注」这类需要快进快删的短期上下文。
- 全局深反思维护它，用户也可以直接覆盖编辑。
- 全局深反思只读取公开 posts，不读取私聊和评论对话。

### 2.3 Structured Evidence

存储：

- `posts` / `comments`
- `chat_threads` / `chat_messages`
- `attachments` 及 `post_attachments` / `comment_attachments` / `chat_message_attachments`
- `entities` / `post_entities`
- `emotions`
- `events`
- `relations` / `relations_log`
- `todos`
- `reflections`
- `meta`

职责：

- `posts` 是公开表达和全局反思的主要证据。
- `comments` 是 post 下按 `(post_id, soul_name, seq)` 分组的扁平评论会话流；`seq=0` 是 SOUL 首评，后续 `seq>0` 是用户追问和 SOUL 回复。
- `chat_messages` 是私聊的局部对话证据；`comments(seq>0)` 和 `chat_messages` 共同服务当前对话和 SOUL 深反思。
- `attachments` 保存本地图片元信息；关联表把图片挂到公开 post、评论消息或私聊消息。原始图片文件只进入本地存储和 UI 展示，不进入 FTS5/ChromaDB，也不会把二进制内容直接塞进普通文本 prompt。
- `vision_cache` 保存图片理解摘要；公开 post 的可用图片摘要会作为 `post_vision` 向量文档进入 ChromaDB，私聊/评论中的当前图片摘要会进入本轮回复上下文。
- entities/emotions/events/relations 是轻反思从公开 post 派生的结构化信号。
- todos 只从公开 post 抽取。

## 3. 存储布局

```
workspace/
├── state.db
├── chroma_db/
├── user.md
├── attachments/
│   └── images/
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
| 评论会话 | `comments` | 私聊/评论追问中作为相关记忆注入 | ReplyService / CommentService |
| 私聊消息 | `chat_messages` | 当前线程消息序列 | ChatService |
| 图片附件 | `attachments` + attachment link tables | UI 展示；可选识图后注入摘要 | AttachmentService / VisionService |
| 图片摘要 | `vision_cache` + `post_vision` 向量文档 | 作为客观视觉摘要注入回复与反思上下文 | VisionService / RecordService |
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
- `# 网页搜索结果`（仅在网页搜索开启且 gate 判断需要时）

明确不再注入：

- 最近 3 条帖子。
- 中间层记忆摘要。
- 每个 SOUL 的私有相处记忆片段。

SOUL 自己的人格和 `soul_memories/<name>.md` 仍由 reply router 在该 SOUL 调用时注入。

### 4.2 私聊回复

私聊上下文包含：

- 当前 SOUL 人格与相处记忆。
- `user.md`。
- unified retrieval 命中的相关记忆：公开 posts、公开评论对话、当前 SOUL 的私聊片段。
- 当前 thread 的最近消息序列。
- 活跃 todos（工具开启时）。
- 可选网页搜索结果。
- 如果当前消息带图片且识图可用，消息文本会追加图片理解摘要；未启用或配置不可用时，只追加“有图但不能查看内容”的边界提示，避免模型假装看图。

私聊消息不写 posts，不进 FTS5，不触发轻反思，不直接更新全局 `user.md`；但会进入 ChromaDB 统一检索池，并且只允许当前 SOUL 检索自己的私聊。

### 4.3 评论对话回复

评论对话使用 `(post_id, soul_name)` 作为会话键，没有独立 `comment_threads` 表。上下文包含：

- 当前 SOUL 人格与相处记忆。
- `user.md`。
- 原始 post。
- unified retrieval 命中的相关记忆：公开 posts、公开评论对话、当前 SOUL 的私聊片段。
- 当前 SOUL 的首条回复。
- 当前评论会话的最近追问/回复消息序列。
- 活跃 todos（工具开启时）。
- 可选网页搜索结果。
- 如果原 post 或当前追问带图片且已有图片摘要，会作为客观视觉摘要注入；未启用或配置不可用时，只追加“有图但不能查看内容”的边界提示，避免模型假装看图。

当前 post 本身会从 related post ids 中排除，避免重复注入。

### 4.4 SOUL 有边界即兴

SOUL 是虚拟好友，不是事实复读机。回复时允许使用比喻、场景感、小剧场和幽默想象来营造陪伴氛围；但用户身份、经历、偏好、人际关系、过去对话、共同回忆和现实事件必须有上下文证据支撑。证据不足时只能用“听起来像”“我猜”“可能是”这类推测语气，不得说成确定事实。

这条边界同样约束长期记忆：SOUL 自己生成的玩笑、比喻和想象场景不能沉淀为 `soul_memories/<name>.md` 中的用户事实。TraceLog 可以让好友“演气氛”，但不能让模型替用户“造人生”；事实陈述和长期记忆仍然遵循 raw-evidence-first。

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

触发：CLI 退出、API 手动触发或 Web 反思入口。

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
- 「当前状态与关注」应保持精简，已解决或过时的短期状态要积极 remove。

### 5.3 SOUL 深反思

触发：CLI 退出、API 手动触发或 Web 反思入口。

输入：

- 当前 SOUL 的 soul Markdown。
- 当前 `soul_memories/<name>.md`。
- 该 SOUL cursor 之后的 raw chat/comment messages。

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
| 首条 AI 评论 | 否 | 可作为相关评论背景 | 是，ChromaDB |
| 评论追问消息 | 否 | 仅对应 SOUL | 是，ChromaDB |
| 私聊消息 | 否 | 仅对应 SOUL | 是，仅当前 SOUL 可检索 |
| 图片摘要 | 随所属公开 post 可作为证据，经全局深反思 patch gate | 随所属 SOUL 互动可作为背景 | 公开 post 图片摘要进入 ChromaDB；私聊/评论图片当前只注入本轮上下文 |
| 网页搜索结果 | 否 | 否 | 否 |
| todos | 可作为全局深反思背景 | 不直接写 SOUL 记忆 | 否 |

这个边界保证：

- 私聊不会污染全局画像。
- 评论对话的局部上下文不会被误当成公开事实。
- SOUL 之间不会自动共享私密理解。
- 公开回复不会执行历史证据中的 prompt injection。
- 图片摘要和网页搜索结果都是辅助证据，不是用户新指令；网页资料不写入长期记忆。

## 7. 模块边界

| 模块 | 职责 |
| --- | --- |
| `core/record_service.py` | 保存公开 post、维护 pending vector doc、格式化 post |
| `core/retrieval.py` | FTS5 + ChromaDB unified retrieval |
| `core/reply_context.py` | query rewrite + hybrid search 的回复上下文共用 helper |
| `core/context_builder.py` | 公开 post 共享上下文组装 |
| `core/reply_service.py` | 多 SOUL fanout 与首条评论落库 |
| `core/chat_service.py` | 私聊 thread、消息与回复上下文 |
| `core/comment_service.py` | `(post_id, soul_name)` 评论会话、消息与回复上下文 |
| `core/vision_service.py` | 图片理解摘要、缓存和上下文注入 |
| `core/web_search_gate.py` / `core/web_search_service.py` | 搜索决策、DuckDuckGo/Tavily 搜索和搜索结果上下文 |
| `core/app_services/job_service.py` / `core/app_services/api_runtime.py` | API 后台 jobs、worker 和孤儿附件清理 |
| `core/app_services/public_post_pipeline.py` | Web/API 公开 post 后台 pipeline |
| `core/reflector.py` | 轻反思、全局深反思、SOUL 深反思 |
| `core/profile_service.py` | `user.md` 初始化、解析、patch gate、revision |
| `core/soul_memory_service.py` | SOUL 相处记忆初始化、patch、revision |
| `core/memory_review_service.py` | 用户覆盖编辑 `user.md` / `soul_memories` 与 revision 读取 |
| `core/llm/*_router.py` | LLM prompt、JSON parsing、调用边界 |
| `core/db.py` | SQLite 初始化、事务、legacy cleanup、查询 helper |

## 8. 后续中间层记忆方向

当前选择完全移除旧中间层，是为了避免半成品抽取层同时承担检索、权限、反思、清理等过多职责。重新设计时至少要满足：

- 单位清晰：到底是 fact、pattern、preference、relationship、episode 还是 hypothesis。
- 可更新：不是只 append；必须能 merge、revise、retract、archive。
- 证据明确：每个条目能追溯到 raw evidence，并能区分公开、私聊、评论对话边界。
- 生命周期明确：短期信号、长期记忆、趋势观察不能混成一种表。
- 读路径明确：回复、全局深反思、SOUL 深反思分别允许读取哪些层。
- 用户控制明确：用户编辑长期 Markdown 记忆时，系统如何处理冲突和旧条目。

在这层重新设计完成前，回复上下文和深反思都优先使用 raw evidence，牺牲一点 token 成本，换取语义和权限边界更可靠。
