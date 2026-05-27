# TraceLog 项目架构设计 v3

本文档是 TraceLog 的技术架构设计，涵盖记忆系统分层、数据存储布局、SOUL/user.md 格式规范、读写流程与反思器设计。

---

## 1. 总体架构

### 1.1 分层模型

TraceLog 当前的记忆系统分为四层，每一层的职责、存储介质、加载时机都独立。Observation 升级后的目标链路是：

```
raw evidence（post / comment / comment_thread / chat_thread / todo / reflection）
    -> observation（即时、短小、可检索、带边界、可追溯）
    -> retrieval context（按权限与上下文预算渐进展开）
    -> deep reflection + consolidation（总结、合并、淘汰、提升）
    -> long-term memory（user.md / soul_memories / reflections）
```

TraceLog 不采用 chatbot 式 session 作为一等叙事单元。公开叙事单元是 `post`，也就是一条公开动态及其多 SOUL 评论和评论线程；私密叙事单元是 `chat_thread`，但私聊边界由 `soul_name` 绝对隔离。

**L1：人格与 SOUL 记忆层 (Persona & Soul Memory Layer)**

存储：`souls/*.md` + `soul_memories/*.md` + `state.db.souls` 表。

- 定义 AI 用什么语气、以哪些"AI 好友"身份和用户互动。
- 每个 SOUL 有独立的相处记忆，只在该 SOUL 被调用时注入。
- 所有 `enabled=1` 的 SOUL 都会对每帖各自生成一条评论。
- 每个被调用的 SOUL 各自走一次 system prompt（人格 + 记忆独立）。

**L2：身份与画像层 (User & Profile Layer)**

存储：`user.md`（用户档案：基本信息 + 成长画像）。

- 由 AI 反思器主动维护全文，用户在前端也可编辑。
- 章节按敏感度（high / normal）分级：高敏章节需更高置信度才允许 AI 自动写入。
- 每会话整体注入 system prompt。

**L3：结构化记忆层 (Structured Memory)**

存储：`state.db` (SQLite) + `chroma_db/`（向量索引；仅 posts，不索引私聊）。

- SQLite 表：`posts` / `posts_fts` / `posts_fts_trigram`。
- SQLite 表：`entities` / `post_entities` / `emotions` / `events` / `relations`。
- SQLite 表：`reflections` / `todos` / `meta` / `souls` / `comments`。
- SQLite 表：`user_md_revisions` / `soul_memory_revisions`。
- SQLite 表：`chat_threads` / `chat_messages`（私聊，独立通道）。
- 关键词查询走 FTS5 双表（unicode61 + trigram）。
- 语义查询走 ChromaDB。
- 混合查询走 RRF 融合。
- 通过工具调用按需查询，不进 system prompt。
- 私聊只按线程顺序加载历史，不进上述检索池。

**反思器 (Reflector)**

运行方式：后台异步，每条 post 写入后 spawn 轻量 LLM agent。

- 轻反思：读最近内容 → 抽取实体/情绪/事件/关系 → 写派生表。
- 深反思：按可配置触发策略聚合 → 生成 reflection + `user.md` 条目级 patch。
- `normal` 章节直落，`high` 章节使用更高阈值谨慎直落。
- Observation 升级后，轻反思会扩展为 Signal Extraction：公开 post 抽完整结构化信号，评论线程与私聊只抽 observation。
- Observation 升级后，深反思之后追加 consolidation：只在同一 visibility boundary 内执行 merge / supersede / promote。

### 1.2 设计原则

1. **会进 system prompt 的 → Markdown 文件**（`souls/*.md`、`soul_memories/*.md`、`user.md`）
   - 理由：每个 SOUL 的人格与相处记忆整体加载，使用 prefix cache 友好
   - 理由：用户能直接看到自己的 AI 记忆，符合数据主权叙事
   - 注意：交互项目默认多 SOUL 同时启用，每条 post 会让所有启用 SOUL 各自跑一次调用，每次调用注入对应人格 + 对应 SOUL 记忆 + 共享的 user.md 与上下文
2. **会被查询/聚合/统计的 → SQLite**（posts、entities、emotions、todos…）
   - 理由：跨条聚合、关联查询、时间趋势必须靠数据库
   - 理由：不会塞进 system prompt，所以不需要可读性
3. **向量检索独立成层**：ChromaDB 不与 SQLite 二选一，两者协同
4. **反思器是 LLM agent 而非定时脚本**：参考 Hermes Agent，每条 post 后异步抽取结构化信号，周期性深反思再更新画像
5. **数据主权 v2**：所有数据本地、可一键导出、可一键备份、可一键删除
6. **Observation 是中层记忆单位**：它不是 raw message，也不是深反思总结；它是即时蒸馏出的交互信号，必须带来源、时间、可见性和证据权限
7. **边界先于智能**：私聊 observation 绝不跨 SOUL；评论线程继承公开 post 语境；公开场景永不召回 `soul_scoped` observation

---

## 2. 存储布局

### 2.1 目录结构

```
workspace/
├── state.db                  # 唯一的 SQLite 数据库
├── chroma_db/                # ChromaDB 向量索引（保留）
│   └── ...
├── user.md                   # 用户档案 + 成长画像（合并）
├── souls/                    # AI 人格库：设定这个 SOUL 是谁
│   ├── 默认.md               # 默认人格
│   ├── 毒舌好友.md
│   └── ...                   # 用户/社区自定义
└── soul_memories/            # SOUL 相处记忆：这个 SOUL 如何理解用户
    ├── 默认.md
    ├── 毒舌好友.md
    └── ...
```

### 2.2 文件 vs 数据库归属总表

| 数据 | 存储位置 | 进 system prompt | 由谁维护 |
| --- | --- | --- | --- |
| SOUL 人格正文 | `souls/<name>.md` | ✓ 该 SOUL 被调用时注入 | 用户编辑 / 默认库分发 |
| SOUL 相处记忆 | `soul_memories/<name>.md` | ✓ 该 SOUL 被调用时注入 | SoulMemoryService + 用户可编辑；由该 SOUL 的评论线程、私聊互动和用户反馈更新 |
| SOUL 启用与排序状态 | `state.db` souls 表 | ✗ | 用户在前端启用/禁用 |
| 用户档案（基本信息 + 成长画像） | `user.md`（章节带 sensitivity 元数据） | ✓ | AI 反思器主动维护，用户可编辑 |
| user.md 内部写入留痕 | `state.db` user_md_revisions 表 | ✗ | ProfileService 每次写入时记录；用于调试和事故恢复，不作为默认前端功能 |
| SOUL 相处记忆历史 | `state.db` soul_memory_revisions 表 | ✗ | SoulMemoryService 每次写入时记录 |
| 帖子原文 | `state.db` posts 表 | 按需检索 | RecordService |
| AI 评论（每帖每 SOUL 一条） | `state.db` comments 表 | 按需引用 | ReplyService |
| 私聊线程与消息 | `state.db` chat_threads / chat_messages 表 | 仅当前线程的消息序列进 prompt | ChatService |
| 评论线程与消息 | `state.db` comment_threads / comment_messages 表 | 仅当前线程的消息序列进 prompt | CommentService |
| 帖子关键词索引 | `state.db` FTS5 双表 | ✗ | trigger 自动同步 |
| 帖子语义向量 | `chroma_db/` | ✗ | RecordService |
| 实体、情绪、事件、关系 | `state.db` 派生表 | ✗ | 反思器 |
| 反思记录 | `state.db` reflections 表 | 按需引用 | 反思器 |
| 待办 | `state.db` todos 表 | 当 TodoTool 开启时，活跃待办注入 prompt | TodoService |
| 元数据（schema_version 等） | `state.db` meta 表 | ✗ | 系统 |

---

## 3. souls/*.md 格式规范

### 3.1 文件命名

- 文件名 = SOUL 名称，支持中文
- 首次启动时，初始化流程至少创建一个内置 SOUL（如 `souls/默认.md`），并默认 `enabled=1`
- 用户自定义示例：`souls/毒舌好友.md`、`souls/林黛玉.md`、`souls/十年后的自己.md`
- SOUL 启用/禁用与排序状态由 `state.db.souls` 表管理；文件存在但表里 `enabled=0` 的 SOUL 不会参与评论

### 3.2 文件格式

每个 SOUL 文件由 YAML frontmatter + Markdown 正文组成：

```markdown
---
name: 毒舌好友
version: 1
description: 直白吐槽型，习惯戳破自我安慰，但底色是关心
created_at: 2026-05-23
author: TraceLog 默认库
tags: [直白, 幽默, 反鸡汤]
---

你是用户最不留情的好友。你看穿 ta 的所有自我安慰和借口，
但你不是冷漠——你是因为太了解 ta 才不允许 ta 骗自己。

## 语气特征
- 短促、直接、带点嘲讽
- 偶尔吐槽但不羞辱
- 用反问代替说教
- 不说"加油""你可以的"这种空话

## 表达习惯
- 经常用"啊"、"嘛"、"得了吧"
- 喜欢戳破矛盾："你昨天不是还说 X 吗？"
- 会调侃但不贬低人格

## 边界
- 用户表达明确的痛苦/低落时，立刻切换共情模式
- 涉及健康、安全、心理危机时，直接给出建议或求助资源
- 不评论用户的外貌、身材、家庭背景
```

### 3.3 soul_memories/*.md 格式

`souls/*.md` 定义"这个 SOUL 是谁"，`soul_memories/*.md` 定义"这个 SOUL 和用户相处后记住了什么"。二者必须分开，避免用户修改人格设定时覆盖关系记忆，也避免反思器把观察写进人格模板。

每个 SOUL 对应一份同名记忆文件：

```markdown
---
schema: tracelog/soul_memory.md@v1
soul: 毒舌好友
updated_at: 2026-05-23T22:00:00+08:00
---

# 毒舌好友的相处记忆

## 对用户的理解
- 用户接受直接反馈，但讨厌空泛鸡汤。 <!-- id: understand-feedback -->
- 用户焦虑时会先把事情想复杂，需要先帮 ta 把问题拆小。 <!-- id: understand-anxiety -->

## 我们之间的互动约定
- 可以吐槽拖延，但不要把吐槽落到人格否定上。 <!-- id: rule-no-shame -->
- 用户明显低落时，先共情，再给行动建议。 <!-- id: rule-low-mood -->

## 私聊沉淀
- 最近一次私聊里，用户更愿意谈比赛压力，而不是泛泛聊学习效率。 <!-- id: chat-el-pressure -->
```

约定：

- `soul_memories/<name>.md` 只在对应 SOUL 被调用时注入 prompt，其他 SOUL 不读取。
- 公开 post 可以影响全局 `user.md`，也可以影响各 SOUL 的相处记忆；私聊默认只影响当前 SOUL 的相处记忆，不直接写入全局 `user.md`。
- SOUL 相处记忆同样使用条目 anchor，写入历史落 `soul_memory_revisions`。第一期可先全文重写，第二期再复用 `ProfileService.apply_patch` 的条目级 patch 机制。
- 用户可以在前端查看和编辑每个 SOUL 的相处记忆；AI 写入需要保留 evidence，私聊 evidence 使用 `chat_message_id`，post evidence 使用 `post_id`。

### 3.4 加载机制

启动时：
1. 扫描 `souls/` 目录，把新文件 upsert 到 `state.db.souls` 表（默认 `enabled=1`）
2. 读取 `souls` 表中所有 `enabled=1` 的记录，按 `sort_order, name` 排序得到启用 SOUL 列表
3. 对每个启用 SOUL，按需读取并解析对应 `souls/<name>.md`（人格段）和 `soul_memories/<name>.md`（相处记忆段）
4. 加载结果可缓存在内存，文件 mtime 变化时重建

发帖时（详见 §5.1）：
- 对启用 SOUL 列表中的每一个，独立组装 system prompt（人格段 + SOUL 记忆段不同，user.md 与检索上下文共享），并发发起一次 LLM 调用
- 每条返回结果落 `comments` 表，前端按 `sort_order` 渲染评论流

启用 / 禁用 / 排序：
- 用户在前端切换开关 → 写入 `souls.enabled`
- 用户拖拽排序 → 批量更新 `souls.sort_order`
- 删除文件 → 启动时检测到 `.md` 缺失，自动把表中记录置 `enabled=0` 并保留历史评论引用
- 新增 SOUL 文件 → 启动扫描或前端"刷新 SOUL 库"按钮触发 upsert，并自动创建空的 `soul_memories/<name>.md`

### 3.5 多 SOUL 评论

每条 post 默认会同时收到多个启用 SOUL 的评论：

- 一篇 post 触发 N 次 LLM 调用（N = `souls.enabled=1` 数量）
- 每次调用使用相同的 user.md + 共享检索上下文，但 persona 段和 SOUL 记忆段不同
- 每条返回作为一行写入 `comments` 表（`is_main=0`），前端按评论流形式展示
- TodoTool 是独立可选工具，只从公开 post 抽取待办，不由 SOUL 回复产出
- 用户可随时把某个 SOUL 的 `enabled` 切到 0；后续 post 不再触发该 SOUL，但历史评论保留可读

成本与延迟控制：
- 第一期内置 2—3 个默认 SOUL，避免一次 5—10 倍 token 消耗
- 多 SOUL 调用并发执行（asyncio / threadpool），用户感知 ≈ 单次调用最慢者
- 首屏可只渲染最先返回的两条，其余通过流式追加

## 4. user.md 格式规范

### 4.1 设计原则

- **整文档统一编辑**：整篇都是一份用户档案，AI 反思器和用户都可以编辑任何条目，任何条目也可以由任一方新增、删除、改写。
- **结构是 H2 章节 + 列表项**：每个 H2（`##`）是一个章节，章节内的条目是 markdown 列表项（一行一条）或一段连续的描述文字。条目就是 patch 的最小单位。
- **章节带敏感度元数据**：每个章节通过 frontmatter 或 HTML 注释挂一个 `sensitivity` 标记（`high` / `normal`），决定 AI 自动落盘前使用普通阈值还是更高阈值。
- **每次写入都进内部留痕**：所有改动（无论 AI 或用户）都落 `user_md_revisions` 表，用于调试和事故恢复；前端默认只展示当前画像。

### 4.2 文件结构

```markdown
---
schema: tracelog/user.md@v1
sensitivity:
  基本信息: high      # AI 改动需更高置信度
  关键身份: high
  身份与现状: normal
  技能与专长: normal
  兴趣与习惯: normal
  关注的核心人际关系: normal
  性格与情绪倾向: normal
  长期目标与当前痛点: normal
---

# 用户档案

## 基本信息
- 姓名：xxx <!-- id: bf-name -->
- 学校：xx大学 <!-- id: bf-school -->
- 入学：20xx-09 <!-- id: bf-enroll -->
- 时区：Asia/Shanghai <!-- id: bf-tz -->
- 主要使用时段：21:00—01:00 <!-- id: bf-active -->

## 关键身份
- 本科生 <!-- id: ki-undergrad -->
- xx学院 20xx 级 <!-- id: ki-school-major -->
- xx社团成员 <!-- id: ki-club -->

## 身份与现状
<!-- id: status-main -->
你正处在大一下学期，刚开始适应密集的课程节奏。

## 技能与专长
- Python 后端开发，熟悉 FastAPI 和 SQLite <!-- id: sk-py -->
- 对 LLM 应用工程有持续深入的兴趣 <!-- id: sk-llm -->
- 文字表达能力强，常用比喻把复杂概念说清楚 <!-- id: sk-writing -->

## 兴趣与习惯
- 喜欢深夜写代码和构思产品 <!-- id: hb-night -->
- 偏好长文记录而非碎片输入 <!-- id: hb-longform -->
- 周末倾向"深度沉思"而非密集社交 <!-- id: hb-solitude -->

## 关注的核心人际关系
- 导师：暑期可能加入研究小组的目标对象 <!-- id: rel-mentor -->

## 性格与情绪倾向
- 自驱力强但容易陷入完美主义 <!-- id: tr-driven -->
- 在信息过载时倾向沉默而不是求助 <!-- id: tr-silent -->
- 接受直接反馈，反感空洞鼓励 <!-- id: tr-feedback -->

## 长期目标与当前痛点
- 长期：以独立开发者身份做出有人用的产品 <!-- id: gl-long -->
- 当前：兼顾比赛、课程、项目，时间分配焦虑 <!-- id: gl-now -->
```

约定：

- 章节标题就是 sensitivity map 的 key；新增章节时同时在 frontmatter 写一行（默认 normal）。
- 同一章节内每条列表项是独立条目；用户在前端可拖拽排序、新增、删除、就地编辑。
- 自由段（连续文字）作为整段视为一条条目处理。
- **每个条目末尾挂一个稳定 anchor**：HTML 注释 `<!-- id: <slug> -->`，由 `ProfileService` 在条目落盘时自动生成（章节短前缀 + 8—12 字符随机串，例如 `sk-py` 或 `tr-9af23c1d`）。anchor 一旦生成不变，即使条目正文被改写也保留同一 id。
- anchor 是 patch 唯一的"匹配键"：渲染层不展示，序列化时保留。用户在前端编辑时由后端补回 anchor，AI 不能自行编造 anchor（必须从读到的当前 user.md 里复制）。

### 4.3 敏感度分级与写入策略

| sensitivity | AI 反思器写入 | 用户前端写入 |
| --- | --- | --- |
| high | 达到更高 evidence/confidence 阈值后直接落盘，记录内部留痕 | 与 normal 一样保存；用户点击保存时统一弹一次保存确认 |
| normal | 达到 evidence/confidence 阈值后直接落盘，记录内部留痕 | 用户点击保存时统一弹一次保存确认 |

章节自动落盘阈值：

| 章节 sensitivity | op 类型 | 最少 evidence 条数 | 最少 confidence | 不达阈值的处理 |
| --- | --- | --- | --- | --- |
| normal | add | 1 | 0.60 | 丢弃，记 reflect_logs |
| normal | update | 1 | 0.65 | 丢弃，记 reflect_logs |
| normal | remove | 1 | 0.85 | 丢弃，记 reflect_logs |
| high | add | 1 | 0.85 | 丢弃，记 reflect_logs |
| high | update | 1 | 0.88 | 丢弃，记 reflect_logs |
| high | remove | 1 | 0.95 | 丢弃，记 reflect_logs |

补充规则：

- 删除条目（remove）一律比 add/update 严格；high 章节整体比 normal 章节更严格。
- 用户前端的写入不受上述阈值约束；用户编辑任意章节后，点击保存时统一弹一次简单保存确认。
- evidence 必须是真实存在的 post_id（深反思跑前先 SELECT 校验），伪造的 evidence 会让整条 patch 跳过。
- 不允许写入“暂无”“待补充”“未知”等无信息条目；空章节保持空白。

### 4.4 条目级 patch 协议

反思器和前端共用同一份 patch schema。匹配键是条目 anchor（§4.2），不是正文字符串。

```json
{
  "section": "技能与专长",
  "ops": [
    {"op": "add",
      "value": "熟悉 ChromaDB 与 FTS5 双轨检索"},
    {"op": "update",
      "anchor": "sk-py",
      "value": "Python 后端开发，熟悉 FastAPI、SQLite 与 ChromaDB"},
    {"op": "remove",
      "anchor": "sk-writing"}
  ],
  "evidence": ["20260520-003", "20260521-001"],
  "confidence": 0.86
}
```

字段说明：

- `op=add`：不需要 anchor，落盘时由 `ProfileService` 自动生成并写回文件；返回值带新 anchor 供调用方记录。
- `op=update` / `op=remove`：必须带 `anchor`，且必须存在于当前 user.md 中；anchor 不存在时整条 patch 跳过（不部分应用）。
- AI 输出的 patch 中所有 anchor 必须来自它本次读到的 user.md；prompt 中明确禁止"生造"。
- 用户在前端编辑后由后端按"原 anchor 不变 + 新 anchor 自动分配"序列化回来，无需用户感知 anchor。

执行：

1. `ProfileService.apply_patch(patch, source="reflector|user")`
2. 解析 sensitivity → 决定使用普通阈值还是高敏阈值
3. 写入文件 + 同步写一行到 `user_md_revisions`

### 4.5 内部写入留痕表

```sql
-- user.md 内部写入留痕：每次落盘的整文件快照 + 触发该次写入的 patch
CREATE TABLE IF NOT EXISTS user_md_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot    TEXT NOT NULL,                -- 写入后的完整 user.md
    patch       TEXT NOT NULL,                -- 触发本次写入的 patch JSON
    source      TEXT NOT NULL,                -- 'reflector' / 'user'
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_md_rev_ts ON user_md_revisions(created_at DESC);

```

这两张表已在 [database.md](./database.md) 中列出；初始化 schema 需要一并建表。

### 4.6 与旧设计的关系

- 旧版 `<!-- USER_FACTS -->` / `<!-- AI_PROFILE -->` marker：**废弃**。新版 `user.md` 直接使用章节 + sensitivity frontmatter。
- 旧版 `flush_profile` 全量重写逻辑：**替换**为 patch 协议。

---

## 5. 写入流程

### 5.1 用户发帖的完整链路

```
用户输入 user_input
    │
    ▼
[1] Retrieval.hybrid_search(user_input, k=3) -> relevant_ids
    │   - 调用方先基于当前输入做 FTS5 + ChromaDB 混合检索
    │   - 检索结果只作为 ContextBuilder 的输入，不由 ContextBuilder 内部发起检索
    │
    ▼
[2] ContextBuilder.build_context(relevant_post_ids=relevant_ids)
    │   - 读 souls 表 enabled=1 的 SOUL 列表（按 sort_order）
    │   - 读 user.md
    │   - 为每个启用 SOUL 读取 soul_memories/<name>.md
    │   - 根据调用方传入的 relevant_post_ids 读取相关历史 post
    │   - 读最近若干条 post（时间近邻）
    │   - Todo 工具开启时读取活跃待办
    │   - 输出：共享上下文 + 启用 SOUL 列表 + 每个 SOUL 的私有记忆
    │
    ▼
[3] RecordService.save_post(user_input)
    │   - 生成 post_id（YYYYMMDD-NNN）
    │   - 写 state.db.posts 表（不再绑定单一 SOUL）
    │   - FTS5 双表通过 trigger 自动同步
    │   - ChromaDB.upsert(id=post_id, document=user_input)
    │
    ▼
[4] TodoTool.run_for_post(post_id)
    │   - 可选开启，默认开启
    │   - 只读取当前公开 post + 当前活跃待办
    │   - 输出 todos_to_upsert / todos_to_delete，并写入 todos.source_post
    │   - 与 SOUL 回复、私聊、评论线程解耦
    │
    ▼
[5] ReplyService.fanout(post_id, user_input, client, model, built_context)
    │   并发对每个启用 SOUL 调用 LLM：
    │   ├─ system prompt = 该 SOUL 的人格段 + 该 SOUL 的相处记忆 + 共享上下文
    │   ├─ 返回 reply
    │   └─ 写一行到 state.db.comments（post_id, soul_name, content）
    │
    ▼
[6] 前端展示评论流
    │   - 按 souls.sort_order 渲染评论卡片
    │   - 流式：先到先显示，最慢者补位
    │
    ▼
[7] Reflector.spawn_async(post_id)  ← 关键：异步，不阻塞前端
        │
        ▼ （在后台线程）
        - 读这条 post + 最近 N 条 post
        - 调用便宜模型（gpt-4o-mini）做"轻反思"
        - 输出 JSON: { entities: [...], emotions: [...], events: [...], importance: 0.7 }
        - 写 state.db.entities / post_entities / emotions / events
        - 更新 posts.importance
        - 可选：根据该 post 下各 SOUL 的评论，异步更新对应 soul_memories/<name>.md
        - 如果达到深反思触发条件 → 同时跑深反思（见 §7）
```

### 5.2 反思器输出 schema

轻反思的强制 JSON 输出格式：

```json
{
  "importance": 0.7,
  "entities": [
    {"type": "person", "name": "小李", "role": "mentioned"},
    {"type": "course", "name": "高数", "role": "subject"}
  ],
  "emotions": [
    {"label": "焦虑", "intensity": 0.6},
    {"label": "疲惫", "intensity": 0.4}
  ],
  "events": [
    {"summary": "高数作业拖到晚上", "category": "study", "ts": "2026-05-23T22:00:00+08:00"},
    {"summary": "和队友讨论比赛想法", "category": "project"}
  ]
}
```

主程序拿到 JSON 后做 upsert 到对应表。失败容忍：抽取失败不影响 post 已落盘。

### 5.3 失败与重试

| 步骤 | 失败处理 |
| --- | --- |
| 写 posts | 整个流程失败，返回错误 |
| 写 FTS5 | trigger 自动处理，正常不会失败 |
| ChromaDB upsert | 记录到 `meta.pending_embedding:<post_id>`，下次启动重试 |
| 单个 SOUL 评论 LLM 调用失败 | 仅丢弃该 SOUL 的评论，其他 SOUL 正常落 comments；前端给该 SOUL 卡片标灰，提供"重试"按钮 |
| 全部 SOUL 评论失败 | 已落盘的 post 不动，前端在评论流位置展示"AI 暂时无法回复，可重试"，不阻塞用户继续发帖 |
| 反思器 | 后台线程吞掉异常，记日志，下次启动可批量补跑 |

### 5.4 私聊流程

私聊是用户与某一个 SOUL 的双人通道，与 post 评论流物理隔离：post 发布后多 SOUL 公开评论，私聊只有用户和这一个 SOUL。

```
用户在线程内发送 chat_message
    │
    ▼
[1] ChatService.append_user_message(thread_id, content)
    │   - 校验 thread 存在且 soul_name 仍 enabled=1（禁用 SOUL 的旧线程只读）
    │   - 写一行到 chat_messages（role=user）
    │   - 更新 chat_threads.last_message_at
    │   - 不写 posts、不写 FTS5、不写 ChromaDB
    │
    ▼
[2] ContextBuilder.build_chat_context(thread_id)
    │   组装层级（按 prefix-cache 友好顺序）：
    │   ① 该 SOUL 的人格段（souls/<soul>.md 正文）
    │   ② 该 SOUL 的相处记忆（soul_memories/<soul>.md）
    │   ③ user.md（与 post 流程共享）
    │   ④ 主记忆引用：
    │      - 用 thread 最近若干轮做 query，对 posts 走 RRF 检索 top-k 原文
    │      - 同时从 comments 拉该 SOUL 自己历史评论里命中的条目
    │      （让 SOUL 在私聊里能引用"用户发过什么 + 我当时怎么评的"）
    │   ⑤ 当前活跃待办（仅 Todo 工具开启时注入）
    │   ⑥ 当前线程的消息序列（最近 N 轮 + token 预算截断；远端老消息可由前一轮 LLM 摘要替换）
    │
    ▼
[3] ChatService.call_chat_reply(soul, context, user_message)
    │   - 单次 LLM 调用，response_format=json_object
    │   - 返回 reply
    │   - 写一行到 chat_messages（role=assistant）
    │
    ▼
[4] 前端展示该消息
        │
        ▼
        - 不触发全局轻反思（私聊不是 post）
        - 不触发 TodoTool（待办只从公开 post 抽取）
        - SOUL 深反思时读取原始私聊，更新当前 SOUL 的相处记忆
        - 不写 ChromaDB
        - 不出现在公开评论流
```

失败处理：

| 步骤 | 失败处理 |
| --- | --- |
| 写 user 消息 | 整个流程失败，提示重发 |
| LLM 调用失败 | user 消息已落盘，assistant 行暂缺；前端提供"重试"按钮再次调用同一 thread |
| SOUL 深反思失败 | 不影响消息落盘；下次深反思可再次处理 |

### 5.5 私聊与公开评论的边界

| 维度 | 公开评论（comments） | 私聊（chat_messages） |
| --- | --- | --- |
| 触发 | 用户发 post → 所有启用 SOUL 各评一条 | 用户在某条线程里给单个 SOUL 发消息 |
| 可见性 | 同帖下所有 SOUL 平权可见 | 仅当前 SOUL + 用户 |
| 进 ChromaDB / FTS5 | ✗（仅 posts 进） | ✗（明确不进，避免污染语义检索） |
| 进反思器 | 评论本身不进全局画像；可进入对应 SOUL 的相处记忆 | 不进全局画像；摘要只进入当前 SOUL 的相处记忆 |
| 待办抽取 | TodoTool 仅从公开 post 抽取 | 不从私聊抽取 |
| SOUL 切换 | 跟 souls.enabled 联动 | SOUL 被 disable 后旧线程只读，无法继续追加 |

Observation 升级后，这个边界进一步固化为检索与证据权限规则：

| 来源 | observation visibility | scope 字段 | evidence access |
| --- | --- | --- | --- |
| 公开 post | `global` | 无 | `all` |
| SOUL 首条公开评论 | `post_visible` | `scope_post_id` | `post_visible` |
| 评论线程消息 | `post_visible` | `scope_post_id` | `post_visible` |
| 私聊消息 | `soul_scoped` | `scope_soul_name` | `source_soul_only` |
| 不应记录内容 | `private_blocked` | 可选 | `none` |

硬约束：

- 私聊是安全屋。私聊 observation 只属于当前 SOUL，不会自动跨 SOUL 共享。
- 评论线程继承公开 post 的语境。同一 post 下的评论线程 observation 可在该 post 语境中被其他 SOUL 使用。
- 公开 post 回复与公开评论回复场景永远不能召回 `soul_scoped` observation，即使当前回复者正是该私聊 SOUL。
- 私聊 evidence 只能由对应 `scope_soul_name` 在私聊场景展开，不能在公开场景展开。

---

## 6. 读取流程：双轨检索与动态融合

### 6.1 检索路由

接到查询时，统一走 FTS5 + ChromaDB 双轨召回，再根据查询特征动态调整融合权重：

- 含具体名词、日期、人名：FTS5 优先。
- FTS5 优先触发条件：`query` 中含 `entities` 表里的名字 / 含 ISO 日期 / 长度 ≤ 6 字。
- 抽象、情绪、状态描述：ChromaDB 优先。
- ChromaDB 优先触发条件：包含"感觉""觉得""为什么""最近""那种"等模糊词。
- 其他 / 默认：双轨等权融合。

融合不是简单平均：单路强命中可以独立保留，双路都命中时再给一致性奖励。

### 6.2 中文 vs 英文：FTS5 双表选择

```python
def fts_search(query: str, k: int = 10) -> list[tuple[str, int]]:
    """
    返回 [(post_id, rank), ...]
    rank 越小越相关（FTS5 原始 rank 是负的 BM25 分，越负越相关）
    """
    if has_cjk(query):
        # 中文走 trigram 表
        sql = """
            SELECT posts.id, rank
            FROM posts_fts_trigram
            JOIN posts ON posts.rowid = posts_fts_trigram.rowid
            WHERE posts_fts_trigram MATCH ?
            ORDER BY rank
            LIMIT ?
        """
    else:
        sql = """
            SELECT posts.id, rank
            FROM posts_fts
            JOIN posts ON posts.rowid = posts_fts.rowid
            WHERE posts_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
    # 注意：中文需要按 2-3 字滑窗包成 phrase 查询
    return db.execute(sql, (sanitize_fts5(query), k)).fetchall()


def has_cjk(s: str) -> bool:
    return any('一' <= c <= '鿿' for c in s)
```

`sanitize_fts5` 必做：去掉 `"()*` 等 FTS5 特殊字符，避免语法错误（Hermes 的 `_sanitize_fts5_query` 是直接参考样本）。

### 6.3 ChromaDB 语义检索

```python
def vector_search(query: str, k: int = 10) -> list[tuple[str, float]]:
    """返回 [(post_id, distance), ...] distance 越小越相关"""
    results = chroma_collection.query(query_texts=[query], n_results=k, include=["distances"])
    return list(zip(results["ids"][0], results.get("distances", [[]])[0]))
```

### 6.4 动态权重融合

FTS5 `rank` 只作为 debug 原始值保留，v1 最终 FTS 分数使用排序位置分；Chroma distance 只在本次返回的候选内部做相对归一化，不设置跨查询绝对阈值。

```python
def hybrid_search_scored(query: str, k: int = 3) -> list[HybridHit]:
    fts_hits = fts_search(query, k=20)
    vector_hits = vector_search(query, k=20)
    fts_weight, vector_weight = infer_query_weights(query)  # 归一到总和 1.0

    for post_id in union(fts_hits, vector_hits):
        fts_part = fts_weight * normalized_fts_position_score(post_id)
        vector_part = vector_weight * normalized_vector_relative_score(post_id)

        score = max(fts_part, vector_part)
        score += 0.20 * min(fts_part, vector_part)  # agreement bonus
        score += exact_phrase_bonus(post_id, query)
        score += token_coverage_bonus(post_id, query)

    return sorted_hits[:k]
```

### 6.5 三因子重排（第二期）

对 RRF 结果再加 recency 和 importance：

```python
def rerank(post_ids: list[str], now_ts: float) -> list[tuple[str, float]]:
    rows = db.execute(
        f"SELECT id, ts, importance FROM posts WHERE id IN ({placeholders(post_ids)})",
        post_ids
    ).fetchall()

    HALF_LIFE_DAYS = 30
    decay = lambda days: 0.5 ** (days / HALF_LIFE_DAYS)

    scored = []
    for row in rows:
        days = (now_ts - parse_ts(row['ts'])) / 86400
        recency = decay(days)
        importance = row['importance'] or 0.5
        relevance = 1.0  # RRF 已排序，简单按位置给分；或保留 RRF 分
        score = 0.5 * relevance + 0.3 * recency + 0.2 * importance
        scored.append((row['id'], score))

    return sorted(scored, key=lambda x: -x[1])
```

第一期不必加，RRF 已足够好。

### 6.6 三层"读取智能"分配

参考 Hermes 的"投资写入、简化读取"哲学，按成本递增分配：

| 场景 | 用什么 | 原因 |
| --- | --- | --- |
| 默认每次发帖 | 共享上下文（user.md + 检索）+ 每个启用 SOUL 各注入一次人格段和相处记忆 | 0 额外检索成本，仅多算 N 次 LLM |
| 私聊每次发消息 | 该 SOUL 人格 + 该 SOUL 相处记忆 + user.md + 当前 thread 历史 + 对 posts 的 RRF 检索 + 该 SOUL 历史评论 | 中成本：检索一次，单次 LLM |
| 当前 post 找相关历史 | ChromaDB top-3（保持现状） | 低成本 |
| 用户追问"我之前是不是说过…" | RRF 双轨混合 | 中成本 |
| Agent 工具调用 `search_memory` | RRF + 三因子重排 | 高成本 |

---

## 7. 反思器设计

### 7.1 三类反思

| 层级 | 触发 | 输入 | 输出 | 频率 |
| --- | --- | --- | --- | --- |
| 轻反思 | 每条 post 写入后 | 当前 post + 最近 5 条 post + user.md「关键身份/关注的核心人际关系」两节作为已知实体词典 | entities / post_entities / emotions / events / posts.importance / relations 增量 | 每帖 |
| 全局深反思 | 可配置触发条件 + 用户手动触发 | 触发范围内所有 post 的轻反思聚合（不读 raw posts 原文） + 当前 user.md | reflection 文档 + user.md 条目级 patch（按章节 sensitivity 选择阈值后直落） + relations 衰减/归一化 | 可配置 |
| SOUL 记忆反思 | 全局深反思触发时，同步检查每个有新增互动的 SOUL | 该 SOUL 的原始私聊消息 + 评论线程消息 + 首条公开评论 + 当前 soul_memories/<name>.md | soul_memories/<name>.md 条目级 patch + soul_memory_revisions | 按 SOUL 独立触发 |

私聊与反思器的关系：

- **轻反思不读私聊**。每次私聊消息发送时不触发全局轻反思，避免噪声进派生表。
- **全局深反思不读私聊**。全局 `user.md` 主要由 post 证据更新，避免私聊里的玩笑、附和或情绪化表达污染共享画像。
- **SOUL 记忆反思直接读该 SOUL 的原始互动**。每个 SOUL 只读取自己的私聊消息、评论线程消息和首条公开评论，再更新对应 `soul_memories/<name>.md`。原始私聊不进 FTS5 / ChromaDB，也不被其他 SOUL 读取。
- **SOUL 深反思写到 reflections 表**（type='soul_deep'，metadata 带 `soul_name`），便于追溯哪次独立反思影响了哪份 SOUL 记忆。

### 7.2 实现思路

```python
import threading

def spawn_light_reflection(post_id: str):
    """异步触发轻反思，不阻塞主流程"""
    t = threading.Thread(
        target=_run_light_reflection,
        args=(post_id,),
        daemon=True,
    )
    t.start()

def _run_light_reflection(post_id: str):
    try:
        post = db.get_post(post_id)
        recent = db.recent_posts(limit=5, exclude=post_id)
        prompt = LIGHT_REFLECT_PROMPT.format(post=post, recent=recent)

        # 用便宜模型
        result = llm_cheap.json_complete(prompt)

        # 写入 SQLite 各派生表
        update_entities(post_id, result["entities"])
        update_emotions(post_id, result["emotions"])
        update_events(post_id, result["events"])
        update_importance(post_id, result["importance"])

    except Exception as e:
        logger.warning(f"Light reflection failed for {post_id}: {e}")
        # 入队等待重试
        db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (f"pending_reflect:{post_id}", "light")
        )
```

### 7.2.1 轻反思 prompt 与输出 schema

轻反思的 system prompt 必须包含以下要素：

```
你是 TraceLog 的轻量反思器。任务是从一条 post 中抽取结构化信号，
供后续检索、可视化与画像更新使用。绝对不要写主观点评，只抽事实。

## 输入
- current_post: { id, ts, content }
- recent_posts: [ { id, ts, content }, ... ]   # 提供时间上下文，不是抽取目标
- known_entities: { person: [...], course: [...], project: [...], ... }
  # 来自 user.md 的「关键身份」/「关注的核心人际关系」段，用作消歧词典：
  # 命中已知实体时直接复用其规范名，避免"小李 / 李同学 / 李 xx"被识别成不同实体

## 输出 JSON（必须严格匹配 schema）
{
  "entities": [
    {
      "type": "person|course|project|place|org|event_topic",
      "name": "规范名（命中 known_entities 则用其规范名）",
      "aliases": ["可选：本帖中实际出现的称呼"],
      "role": "subject|object|mentioned"
    }
  ],
  "emotions": [
    { "label": "焦虑|喜悦|疲惫|兴奋|平静|失落|愤怒|期待|羞愧|无感",
      "intensity": 0.0_to_1.0 }
  ],
  "events": [
    {
      "ts": "事件发生时间 ISO8601；不明则用 post.ts",
      "summary": "一句话事实描述，≤ 30 字",
      "category": "study|social|health|project|life"
    }
  ],
  "relations": [
    {
      "a": "实体名（须在 entities[] 中出现）",
      "b": "实体名（须在 entities[] 中出现）",
      "rel_type": "friend|classmate|teammate|mentor|family|colleague",
      "strength_delta": -0.2_to_+0.2
      # 仅当本帖给出新证据时才输出；正负代表关系被强化/削弱
    }
  ],
  "importance": 0.0_to_1.0
}
```

#### 字段 → [database.md](./database.md) 派生表的写入映射

| 输出字段 | 写入表 / 列 | 说明 |
| --- | --- | --- |
| `entities[]` | `entities` (UNIQUE(type,name) upsert) + `post_entities` | upsert 后取 entity_id 写 post_entities；`first_seen` 仅首次写，`last_seen = post.ts`，`mention_count += 1` |
| `entities[].aliases` | `entities.aliases`（JSON 数组合并去重） | 不覆盖，只追加新别名 |
| `emotions[]` | `emotions` (PK = post_id+label) | 同 post 同 label 取最大 intensity |
| `events[]` | `events`（每条一行） | 不去重；同事件多次出现以 ts 区分 |
| `relations[]` | `relations` 累加 strength | 见下方 §7.2.1 |
| `importance` | `posts.importance` | 直接 UPDATE |

#### importance 打分维度

模型按以下规则给出 0—1 分，每命中一项加分，封顶 1.0：

| 信号 | 加分 |
| --- | --- |
| 含明确决策（"我决定…/不再…/换成…"） | +0.30 |
| 含 deadline / 具体时间承诺 | +0.25 |
| 提到 user.md「关注的核心人际关系」中的人 | +0.20 |
| 强情绪（任一 emotion intensity ≥ 0.7） | +0.15 |
| 转折性事件（结果 / 节点 / 失败 / 突破） | +0.20 |
| 无以上信号的日常碎记 | 基线 0.10 |

实施提醒：评分维度让 LLM 自评后输出，不需要主程序事后核算。但 prompt 里要把上表完整列出，避免不同模型/不同次跑出的分数缺乏统一标尺。

#### relations 维护方

- **轻反思产出 delta**：仅当本帖含明确互动证据时输出 `strength_delta`（一起做事 +0.05—+0.15；冲突或疏离 -0.05—-0.15；阈值由模型自评）。无证据则不输出。
- **深反思做衰减与归一化**：每次深反思跑完后，对 relations.strength 按可配置系数做半衰处理，再裁剪到 [0, 1]；这样一段时间未提及的旧关系会自然下沉。
- 不允许用户手编 relations；用户只能通过编辑 user.md「关注的核心人际关系」章节间接影响轻反思（known_entities 词典）。

### 7.2.2 派生表幂等约定

轻反思以 `post_id` 为天然幂等键。任意一次重跑（重试 / 用户主动重新反思）必须等价于"刚跑了一次"，因此约定：

- `entities` / `post_entities`：先 `DELETE FROM post_entities WHERE post_id=?`，再按本次输出重新写入；`entities` 表的 `mention_count` 用每次差量重算（重跑时先 `mention_count -= 旧 post_entities 行数`，再 `+= 新行数`）。
- `emotions`：先 `DELETE FROM emotions WHERE post_id=?`，再插入新结果。
- `events`：先 `DELETE FROM events WHERE post_id=?`，再插入新结果。
- `posts.importance`：直接 `UPDATE`。
- `relations`：用三元组 `(entity_a, entity_b, rel_type)` upsert；为支持幂等，新增辅助列 `relations_log(post_id, relation_id, delta)`：
  - 重跑时先 `SELECT delta FROM relations_log WHERE post_id=?` 把旧 delta 加回 strength（撤销）
  - `DELETE FROM relations_log WHERE post_id=?`
  - 写入新 delta 并累加到 strength
  - 这样轻反思永远幂等；无 relations_log 则 strength 会越加越偏。
- 同一 post 触发两次轻反思在并发下需要悲观锁（行锁或全表 `BEGIN IMMEDIATE`），避免两个 worker 同时读旧值再各自写。

`relations_log` 已在 [database.md](./database.md) 中列出；实现时需要保持该表与 `relations` 的幂等更新一致。核心字段如下：

```sql
CREATE TABLE IF NOT EXISTS relations_log (
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    delta       REAL NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (post_id, relation_id)
);
```

### 7.3 深反思触发策略

第一期采用最简单的触发：CLI 退出时对上次深反思后的公开记录生成一次深反思；后续前端再提供手动点击"生成深反思"。

后续再把触发策略做成可配置项，例如按记录数量、时间间隔、用户主动请求或后台任务触发。具体频率不写死为固定周期，由产品形态和成本预算决定。

### 7.4 深反思 prompt 模板要点

```
你是 TraceLog 的全局反思引擎。下面是本次触发范围内的所有 post 记录、
情绪标签、事件抽取，以及当前的 user.md（含 sensitivity 元数据）。

## 你的任务
1. 生成一份深反思（500—800 字），包含：
   - 主线事件回顾
   - 情绪与状态趋势
   - 与重要他人的互动
   - 进展、卡点、转折
   - 一条值得用户注意的洞察

2. 对 user.md 的相关章节产出条目级 patch：
   - 每条 patch 限定一个 section，含 add / update / remove 三类 op
   - update / remove 必须使用条目末尾的 anchor（HTML 注释 `<!-- id: ... -->`）作为匹配键，
     anchor 必须从输入 user.md 中原样复制，禁止生造；add 不带 anchor，由后端补全
   - 仅在有充足新证据时改写；如果已有条目可被修正、合并或细化，优先 update 而不是 add
   - 如果已有条目被新证据推翻、过时、重复，或只是占位内容，应输出 remove
   - 不得输出“暂无”“待补充”“未知”等无信息条目；空章节保持空白即可
   - 对 sensitivity=high 的章节（如"基本信息"/"关键身份"）保持极度保守：
     仅在用户在 post 里明确陈述了新事实时才出 patch，否则不动
   - 每个 patch 必须给出 evidence（post_id 列表）和 confidence（0—1）

## 输出 JSON
{
  "reflection_md": "...",
  "patches": [
    {
      "section": "技能与专长",
      "ops": [
        {"op": "add", "value": "熟悉 ChromaDB 与 FTS5 双轨检索"}
      ],
      "evidence": ["20260520-003", "20260521-001"],
      "confidence": 0.86
    },
    {
      "section": "性格与情绪倾向",
      "ops": [
        {"op": "update",
         "anchor": "tr-silent",
         "value": "在信息过载时会先沉默，但近期会主动找队友拆解"}
      ],
      "evidence": ["20260522-002"],
      "confidence": 0.74
    }
  ]
}
```

主程序拿到结果：
1. 写入 `reflections` 表
2. 把 `reflection_md` 单独导出为 Markdown 文件（可选）
3. 对每个 patch 调用 `ProfileService.apply_patch(patch, source="reflector")`：
   - normal 章节满足普通阈值后直接落盘 + 写内部留痕
   - high 章节满足更高 evidence/confidence 阈值后直接落盘；不达阈值则丢弃并记日志

### 7.5 SOUL 记忆反思 prompt 模板要点

```
你是 TraceLog 的 SOUL 记忆反思器。你的任务不是更新全局 user.md，
而是更新某一个 SOUL 与用户之间的相处记忆。

## 输入
- soul_name: 当前 SOUL 名称
- soul_style: souls/<name>.md 的人格摘要
- current_soul_memory: soul_memories/<name>.md
- recent_private_chat_messages: 该 SOUL 最近私聊原始消息
- recent_comment_thread_messages: 该 SOUL 评论线程原始消息
- recent_public_comments: 该 SOUL 对用户 posts 的首条公开评论
- user_hard_facts: user.md 中的基本信息/关键身份，用于避免误认用户

## 写入原则
- 只记录“这个 SOUL 如何理解用户、如何与用户相处更好”。
- 私聊内容默认只影响当前 SOUL，不写入全局 user.md，也不被其他 SOUL 读取。
- 不把一时情绪当成稳定事实；必须区分用户事实、关系约定、互动偏好和短期状态。
- 输出 patch 必须给 evidence：post_id 或 chat_message_id。

## 输出 JSON
{
  "patches": [
    {
      "section": "对用户的理解",
      "ops": [
        {"op": "add", "value": "用户焦虑时更能接受先拆问题、再给建议的回应方式"}
      ],
      "evidence": ["chat:392", "post:20260522-002"],
      "confidence": 0.78
    }
  ]
}
```

主程序拿到结果后，对每个 patch 调用 `SoulMemoryService.apply_patch(soul_name, patch, source="reflector")`，写入 `soul_memories/<name>.md` 并记录 `soul_memory_revisions`。

### 7.6 Observation 与 Consolidation 设计冻结

Observation 是后续阶段要引入的中层记忆单位。它从 raw evidence 中即时提取，供检索、渐进展开、深反思和长期记忆提升使用。第一版 observation 类型冻结为：

```text
preference
correction
convention
decision
insight
pattern
state
relationship
todo_signal
```

Signal Extraction 是轻反思的扩展概念：

- 公开 post：抽取 entities / emotions / events / relations / observations / importance。
- 评论线程：只抽取 observations，并标记为 `post_visible`。
- 私聊线程：只抽取 observations，并标记为 `soul_scoped`。
- 提取不能依赖 `/back` 或 `/quit`。后续实现必须基于 cursor 增量扫描，保证 Ctrl+C、崩溃或掉电后不会丢失待提取来源。

Consolidation 在 deep reflection 之后运行，用于整理 observation，而不是读取所有记忆后自由合并。它只能执行三类操作：

- `merge`：同一边界桶内重复 observation 合并，旧项标记为 `merged`。
- `supersede`：同一边界桶内新事实或新约定覆盖旧 observation，旧项标记为 `superseded`。
- `promote`：稳定、高置信、符合边界的 observation 才能成为 `user.md` 或 `soul_memories/<name>.md` 的 patch 证据。

Consolidation 必须先按 visibility boundary 分桶，再把候选交给 LLM：

- `global` 只能与 `global` consolidate。
- `post_visible` 只能在同一个 `scope_post_id` 内 consolidate。
- `soul_scoped` 只能在同一个 `scope_soul_name` 内 consolidate。
- `soul_scoped` 永不 merge 到 `global` 或 `post_visible`。
- `soul_scoped` 永不作为 `user.md` promotion evidence。
- `private_blocked` 不参与 consolidation。

第一版不做 entity resolution。实体重复、实体冲突、关系矛盾和实体消歧是独立后续阶段，不混入 observation consolidation。
