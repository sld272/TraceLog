# TraceLog 项目架构设计 v3

本文档是 TraceLog 的工程级项目架构设计。它以记忆系统为核心，但覆盖的不只是"记忆模块"，还包括产品分层、数据布局、SOUL 体系、私聊边界、反思器、导出、实施清单与前端/API 产品化。

本文档基于以下输入综合得出：

- 当前仓库现状（CLI + Markdown/JSON + ChromaDB）
- Hermes Agent（NousResearch）源码深度阅读
- TraceLog 的产品定位：面向普通学生与青年用户的陪伴型 AI 产品
- 三阶段参赛规划：江苏 AIGC（5/31 前）、EL 交互组（6—7 月）、EL Agent 组（7—9 月）

> 本架构同时承担两个角色：
> 1. 入口层"向内的 AI 社交媒体"的数据底座
> 2. 价值层"AI 成长记忆引擎"的核心实现

### 当前实现状态（2026-05-25）

当前代码已完成 v3 地基的第一步：`schema.sql` 作为唯一 SQLite 初始化脚本；`core/db.py` 负责 `workspace/state.db` 初始化、WAL、外键和 FTS5/trigram 可用性检查；CLI 的 `core/memory.py` 已切到 SQLite 主存储，帖子、待办和 `user.md` revision 都写入 `state.db`；运行 `memory.init_workspace()` 时会通过 `core/soul_service.py` 在被 gitignore 的 `workspace/` 下创建默认 `souls` 与 `soul_memories` 文件，并同步 `souls` / `soul_memory_revisions` 表。发帖写入已抽到 `core/record_service.py`，相关历史检索已接入 FTS5 + ChromaDB 的 RRF hybrid 检索；共享上下文组装已抽到 `core/context_builder.py`；`core/soul_service.py` 已支持 SOUL 同步、列表、新建/编辑、启用/禁用与排序；`core/soul_memory_service.py` 已支持 SOUL 相处记忆读写与 revision 记录；公开评论已由 `core/reply_service.py` 支持多 SOUL 并发生成并写入 `comments`；`core/todo_service.py` 已作为可选 TodoTool 只从公开 post 独立抽取待办并写入 `todos.source_post`；私聊已由 `core/chat_service.py` 支持单 SOUL 线程、消息落库、上下文组装与 LLM 回复，不触发待办抽取；`core/reflector.py` 已支持每条 post 的轻反思抽取、失败重试、CLI 退出时触发全局深反思并写入 `reflections` 表；`core/profile_service.py` 已支持深反思画像 patch，normal 章节自动落盘，high 章节使用更高阈值自动落盘。

尚未完成：私聊摘要沉淀、导出和 Web/API 层。

---

## 1. 总体架构

### 1.1 分层模型

TraceLog 的记忆系统分为四层，每一层的职责、存储介质、加载时机都独立：

**L1：人格与 SOUL 记忆层 (Persona & Soul Memory Layer)**

存储：`souls/*.md` + `soul_memories/*.md` + `state.db.souls` 表。

- 定义 AI 用什么语气、以哪些"AI 好友"身份和用户互动。
- 每个 SOUL 有独立的相处记忆，只在该 SOUL 被调用时注入。
- 交互项目（社交媒体形态）：所有 `enabled=1` 的 SOUL 都会对每帖各自生成一条评论，不存在唯一的"主 SOUL"。
- Agent 项目（第三期）：可在启用集合内指定一个主 SOUL 担当主回复 / 工具调用入口，其他 SOUL 仍可作为旁观评论者。
- 每个被调用的 SOUL 各自走一次 system prompt（人格 + 记忆独立）。

**L2：身份与画像层 (User & Profile Layer)**

存储：`user.md`（用户档案：基本信息 + 成长画像，AI 与用户共同编辑）。

- 上半部分：用户主动填写的硬事实。
- 下半部分：反思器维护的软画像。
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

### 1.2 设计原则

1. **会进 system prompt 的 → Markdown 文件**（`souls/*.md`、`soul_memories/*.md`、`user.md`）
   - 理由：每个 SOUL 的人格与相处记忆整体加载，使用 prefix cache 友好
   - 理由：用户能直接看到自己的 AI 记忆，符合数据主权叙事
   - 注意：交互项目默认多 SOUL 同时启用，每条 post 会让所有启用 SOUL 各自跑一次调用，每次调用注入对应人格 + 对应 SOUL 记忆 + 共享的 user.md 与上下文
2. **会被查询/聚合/统计的 → SQLite**（posts、entities、emotions、todos…）
   - 理由：跨条聚合、关联查询、时间趋势必须靠数据库
   - 理由：不会塞进 system prompt，所以不需要可读性
3. **向量检索独立成层**：ChromaDB 不与 SQLite 二选一，两者协同
4. **反思器是 LLM agent 而非定时脚本**：参考 Hermes background_review，每条 post 后异步抽取结构化信号，周期性深反思再更新画像
5. **数据主权 v2**：所有数据本地、可一键导出、可一键备份、可一键删除

### 1.3 与 Hermes Agent 的关系

| 维度 | Hermes Agent | TraceLog |
| --- | --- | --- |
| 用户群 | 开发者 | 普通学生/青年 |
| L1 人格 | `SOUL.md`（单文件，单一激活） | `souls/*.md` + `soul_memories/*.md`（多文件库，**默认多启用并发评论**；每个 SOUL 有独立相处记忆；Agent 阶段可指定主 SOUL） |
| L2 用户与笔记 | `USER.md` + `MEMORY.md`（分离） | `user.md`（合并，AI 与用户共同编辑，章节按敏感度分级写入） |
| L3 历史 | SQLite + FTS5 + trigram | SQLite + FTS5 + trigram + ChromaDB |
| 语义检索 | 可外挂 provider 插件 | 内置 ChromaDB；不接第三方 memory provider |
| 反思 | `background_review` daemon | 同款思路，更轻 |
| 容量限制 | 字符数硬限 | 软限（用户编辑权交还） |
| Prefix cache | 强约束（frozen snapshot） | 弱约束（中途可更新） |

**关键学习**：把人格和画像放在 .md 文件、用 trigram FTS5 处理中文、用后台 LLM agent 做反思——这三点直接借鉴 Hermes。第三方 memory provider 不纳入 TraceLog 路线；Honcho 式“不同 peer 拥有不同理解”的思路，转化为本地多 SOUL 独立记忆。

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
│   ├── default.md            # 默认人格
│   ├── 毒舌好友.md
│   └── ...                   # 用户/社区自定义
└── soul_memories/            # SOUL 相处记忆：这个 SOUL 如何理解用户
    ├── default.md
    ├── 毒舌好友.md
    └── ...
```

### 2.2 文件 vs 数据库归属总表

| 数据 | 存储位置 | 进 system prompt | 由谁维护 |
| --- | --- | --- | --- |
| SOUL 人格正文 | `souls/<name>.md` | ✓ 每个启用的 SOUL 各注入一次 | 用户编辑 / 默认库分发 |
| SOUL 相处记忆 | `soul_memories/<name>.md` | ✓ 仅该 SOUL 被调用时注入 | SoulMemoryService + 用户可编辑；由该 SOUL 的评论、私聊摘要和用户反馈更新 |
| SOUL 启用与排序状态 | `state.db` souls 表 | ✗ | 用户在前端启用/禁用 |
| 主 SOUL（仅 Agent 项目使用） | `state.db.meta.main_soul` | ✓ 由 Agent 决定何时使用 | 用户/Agent 设置 |
| 用户档案（基本信息 + 成长画像） | `user.md`（章节带 sensitivity 元数据） | ✓ 整体 | 用户与反思器共同维护：normal 章节直接落盘；high 章节由更高阈值约束后直接落盘（详见 §5） |
| user.md 内部写入留痕 | `state.db` user_md_revisions 表 | ✗ | ProfileService 每次写入时记录；用于调试和事故恢复，不作为默认前端功能 |
| SOUL 相处记忆历史 | `state.db` soul_memory_revisions 表 | ✗ | SoulMemoryService 每次写入时记录 |
| 帖子原文 | `state.db` posts 表 | ✗（按需检索） | RecordService |
| AI 评论（每帖每 SOUL 一条） | `state.db` comments 表 | ✗（按需引用） | ReplyService |
| 私聊线程与消息 | `state.db` chat_threads / chat_messages 表 | ✗（仅当前线程的消息序列进 prompt，与 posts/反思物理隔离） | ChatService |
| 帖子关键词索引 | `state.db` FTS5 双表 | ✗ | trigger 自动同步 |
| 帖子语义向量 | `chroma_db/` | ✗ | RecordService |
| 实体、情绪、事件、关系 | `state.db` 派生表 | ✗ | 反思器 |
| 反思记录 | `state.db` reflections 表 | ✗（按需引用） | 反思器 |
| 待办 | `state.db` todos 表 | 部分（活跃待办进 prompt） | TodoService |
| 元数据（main_soul、schema_version） | `state.db` meta 表 | ✗ | 系统 |

---

## 3. SQLite Schema 完整定义

### 3.1 核心 DDL

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 元数据：单 key-value 表，存 schema_version、main_soul（Agent 项目用）等
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Schema 版本，便于后续升级
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
-- 注意：交互项目（社交媒体形态）默认所有 enabled=1 的 SOUL 都参与评论，
-- 因此不再使用单一 active_soul。Agent 项目（第三期）需要主 SOUL 时，
-- 由 Agent 自己写入 meta('main_soul', '<name>')。

-- SOUL 注册表：启用状态、排序、文件指针
CREATE TABLE IF NOT EXISTS souls (
    name        TEXT PRIMARY KEY,             -- SOUL 名称，与 souls/<name>.md 文件对应
    file_path   TEXT NOT NULL,                -- 相对 workspace/ 的路径，如 souls/默认.md
    enabled     INTEGER NOT NULL DEFAULT 1,   -- 1=启用（参与评论），0=禁用
    sort_order  INTEGER NOT NULL DEFAULT 0,   -- 评论展示顺序，越小越靠前
    description TEXT,                         -- 冗余前端展示用，正文以文件 frontmatter 为准
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_souls_enabled ON souls(enabled, sort_order);

-- 帖子主表
CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,            -- 20260523-001
    ts          TEXT NOT NULL,               -- ISO 时间字符串
    content     TEXT NOT NULL,               -- 正文
    importance  REAL DEFAULT 0.5,            -- 反思器打分 0—1
    created_at  REAL NOT NULL,               -- unix timestamp，用于排序
    updated_at  REAL NOT NULL                -- 用于增量重建索引
);

CREATE INDEX IF NOT EXISTS idx_posts_ts          ON posts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_posts_importance  ON posts(importance DESC);

-- AI 评论：一帖多 SOUL，每个启用 SOUL 一条
CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    soul_name   TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
    content     TEXT NOT NULL,               -- 该 SOUL 对此帖的回复正文
    is_main     INTEGER NOT NULL DEFAULT 0,  -- Agent 项目标记主 SOUL 回复，交互项目恒为 0
    metadata    TEXT,                         -- JSON：模型信息、错误状态、备查的待办抽取等
    created_at  REAL NOT NULL,
    UNIQUE(post_id, soul_name)               -- 同一 SOUL 对同一帖只保留最新一条
);

CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_soul ON comments(soul_name, created_at DESC);

-- FTS5 默认（unicode61）：覆盖英文、数字、混排
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    content,
    content='posts',
    content_rowid='rowid'
);

-- FTS5 trigram：覆盖中文子串匹配
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts_trigram USING fts5(
    content,
    tokenize='trigram',
    content='posts',
    content_rowid='rowid'
);

-- 自动同步两张 FTS5 表
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, content)         VALUES (new.rowid, new.content);
    INSERT INTO posts_fts_trigram(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO posts_fts_trigram(posts_fts_trigram, rowid, content)
        VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO posts_fts(rowid, content) VALUES (new.rowid, new.content);

    INSERT INTO posts_fts_trigram(posts_fts_trigram, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO posts_fts_trigram(rowid, content) VALUES (new.rowid, new.content);
END;
```

### 3.2 派生数据表

```sql
-- 实体：人物、课程、项目、地点、组织等
CREATE TABLE IF NOT EXISTS entities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,             -- person / course / project / place / org / event_topic
    name          TEXT NOT NULL,
    aliases       TEXT,                      -- JSON array: ["小李","李同学"]
    first_seen    TEXT,
    last_seen     TEXT,
    mention_count INTEGER DEFAULT 0,
    metadata      TEXT,                      -- JSON: 灵活扩展字段
    UNIQUE(type, name)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_last_seen ON entities(last_seen DESC);

-- 帖子 ↔ 实体多对多
CREATE TABLE IF NOT EXISTS post_entities (
    post_id   TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role      TEXT,                          -- subject / object / mentioned
    PRIMARY KEY (post_id, entity_id, role)
);

CREATE INDEX IF NOT EXISTS idx_pe_entity ON post_entities(entity_id);

-- 情绪打标
CREATE TABLE IF NOT EXISTS emotions (
    post_id   TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    label     TEXT NOT NULL,                 -- 焦虑 / 喜悦 / 疲惫 / 兴奋 / 平静 / 失落 ...
    intensity REAL NOT NULL,                 -- 0—1
    PRIMARY KEY (post_id, label)
);

CREATE INDEX IF NOT EXISTS idx_emotions_label ON emotions(label, intensity DESC);

-- 事件：从帖子中抽取出的客观事件
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id   TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,                 -- 事件发生时间（不一定 = post 时间）
    summary   TEXT NOT NULL,                 -- 一句话事件描述
    category  TEXT,                          -- study / social / health / project / life
    metadata  TEXT                           -- JSON
);

CREATE INDEX IF NOT EXISTS idx_events_ts       ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);

-- 实体之间的关系
CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_a    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel_type    TEXT NOT NULL,               -- friend / classmate / teammate / mentor / family / colleague
    strength    REAL DEFAULT 0.5,            -- 0—1，由轻反思 delta + 深反思衰减共同维护（见 §8.2.1）
    last_seen   TEXT,
    metadata    TEXT,
    UNIQUE(entity_a, entity_b, rel_type)
);

-- 关系强度变更日志：轻反思幂等所需（见 §8.2.2）
CREATE TABLE IF NOT EXISTS relations_log (
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    delta       REAL NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (post_id, relation_id)
);

-- 反思记录
CREATE TABLE IF NOT EXISTS reflections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    type        TEXT NOT NULL,               -- per_post / daily / weekly / monthly / event / soul_chat_digest
    scope_start TEXT,                        -- 反思覆盖的起始时间（per_post 留空）
    scope_end   TEXT,
    content     TEXT NOT NULL,               -- 反思正文（Markdown）
    related_posts TEXT,                      -- JSON: ["20260523-001", ...]
    metadata    TEXT
);

CREATE INDEX IF NOT EXISTS idx_reflections_ts   ON reflections(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_type ON reflections(type);

-- 私聊会话线程：用户与某个 SOUL 的一条独立对话
-- 每个 SOUL 可有多条线程；线程不进 system prompt 的共享段，仅按需加载
CREATE TABLE IF NOT EXISTS chat_threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    soul_name       TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
    title           TEXT,                    -- 用户命名或首条消息自动摘要
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    last_message_at REAL                     -- 用于线程列表排序
);

CREATE INDEX IF NOT EXISTS idx_chat_threads_soul ON chat_threads(soul_name, last_message_at DESC);

-- 私聊消息：一条线程下的逐条消息
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,               -- user / assistant
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages(thread_id, created_at);

-- 待办（迁自 todos.json）
CREATE TABLE IF NOT EXISTS todos (
    id          TEXT PRIMARY KEY,            -- uuid 或自增字符串
    task        TEXT NOT NULL,
    date        TEXT,                        -- YYYY-MM-DD 或 NULL
    start_time  TEXT,                        -- HH:MM 或 NULL
    end_time    TEXT,
    status      TEXT NOT NULL DEFAULT '未完成',  -- 未完成 / 已完成
    source_post         TEXT REFERENCES posts(id) ON DELETE SET NULL,
    source_chat_message INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL, -- 兼容旧版本，新流程不再写
    source_comment_message INTEGER REFERENCES comment_messages(id) ON DELETE SET NULL, -- 兼容旧版本，新流程不再写
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, date);
CREATE INDEX IF NOT EXISTS idx_todos_date   ON todos(date);

-- user.md 内部写入留痕：每次落盘的整文件快照 + 触发该次写入的 patch（详见 §5.5）
CREATE TABLE IF NOT EXISTS user_md_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot    TEXT NOT NULL,
    patch       TEXT NOT NULL,
    source      TEXT NOT NULL,                -- 'reflector' / 'user'
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_md_rev_ts ON user_md_revisions(created_at DESC);

-- SOUL 相处记忆内部写入留痕：每次落盘的整文件快照 + 触发该次写入的摘要/patch
CREATE TABLE IF NOT EXISTS soul_memory_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    soul_name   TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
    snapshot    TEXT NOT NULL,
    patch       TEXT NOT NULL,
    source      TEXT NOT NULL,                -- 'reflector' / 'user'
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_soul_memory_rev_soul_ts
    ON soul_memory_revisions(soul_name, created_at DESC);

```

### 3.3 为什么 FTS5 用 `external content` 模式

`content='posts', content_rowid='rowid'` 这两行让 FTS5 不真的存正文副本，只存 token 化索引。优点：

- 磁盘占用减半
- 正文唯一真相在 `posts.content` 字段
- 自动通过 trigger 保持同步

注意：external content 表删除或更新索引时，不能用普通 `DELETE FROM posts_fts WHERE rowid = ...`。删除旧索引需要向 FTS5 表插入特殊的 `'delete'` 指令，并带上 `old.content`，让 FTS5 知道要从倒排索引里移除哪些旧 token。否则在 post 被编辑或删除后，旧关键词可能残留在搜索结果里。

中文 trigram 表也用同样模式，再省一份。

### 3.4 为什么 chat_messages 不进 FTS5 / ChromaDB

私聊与 posts 是两类不同性质的内容：post 是用户的主动表达，反思器的核心素材；私聊是 SOUL 在某条线程内的语境化对话，含大量寒暄、追问与重复表达。把它们混进同一份索引会污染语义检索：用户在 post 里搜「最近为什么烦」时，最不希望命中的就是十轮"还好吗 / 没事就好"的私聊残片。

因此：

- 私聊检索仅按线程顺序加载（最近 N 条 + token 上限截断），不建语义索引。
- 全局画像反思器只读 `posts` 及其派生表；私聊不直接进入全局 `user.md`。
- SOUL 记忆反思器可读取该 SOUL 的 `chat_messages` 摘要，并把沉淀写入对应的 `soul_memories/<name>.md`。
- 私聊不写 `comments` 表（评论是公开评论流，私聊是单独频道）。

---

## 4. souls/*.md 格式规范

### 4.1 文件命名

- 文件名 = SOUL 名称，支持中文
- 首次启动时，初始化流程至少创建一个内置 SOUL（如 `souls/默认.md`），并默认 `enabled=1`
- 用户自定义示例：`souls/毒舌好友.md`、`souls/林黛玉.md`、`souls/十年后的自己.md`
- SOUL 启用/禁用与排序状态由 `state.db.souls` 表管理；文件存在但表里 `enabled=0` 的 SOUL 不会参与评论
- 交互项目不存在"当前激活的唯一 SOUL"概念；如需让某个 SOUL 在 Agent 项目里担任主回复，写入 `meta.main_soul`

### 4.2 文件格式

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

你是用户最不留情的闺蜜。你看穿 ta 的所有自我安慰和借口，
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

### 4.3 soul_memories/*.md 格式

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

### 4.4 加载机制

启动时：
1. 扫描 `souls/` 目录，把新文件 upsert 到 `state.db.souls` 表（默认 `enabled=1`）
2. 读取 `souls` 表中所有 `enabled=1` 的记录，按 `sort_order, name` 排序得到启用 SOUL 列表
3. 对每个启用 SOUL，按需读取并解析对应 `souls/<name>.md`（人格段）和 `soul_memories/<name>.md`（相处记忆段）
4. 加载结果可缓存在内存，文件 mtime 变化时重建

发帖时（详见 §6.1）：
- 对启用 SOUL 列表中的每一个，独立组装 system prompt（人格段 + SOUL 记忆段不同，user.md 与检索上下文共享），并发发起一次 LLM 调用
- 每条返回结果落 `comments` 表，前端按 `sort_order` 渲染评论流

启用 / 禁用 / 排序：
- 用户在前端切换开关 → 写入 `souls.enabled`
- 用户拖拽排序 → 批量更新 `souls.sort_order`
- 删除文件 → 启动时检测到 `.md` 缺失，自动把表中记录置 `enabled=0` 并保留历史评论引用
- 新增 SOUL 文件 → 启动扫描或前端"刷新 SOUL 库"按钮触发 upsert，并自动创建空的 `soul_memories/<name>.md`

### 4.5 多 SOUL 评论：交互项目的默认行为

交互项目（江苏 AIGC + EL 交互组）以"向内的社交媒体"为产品形态，因此每条 post 默认会同时收到多个启用 SOUL 的评论：

- 一篇 post 触发 N 次 LLM 调用（N = `souls.enabled=1` 数量）
- 每次调用使用相同的 user.md + 共享检索上下文，但 persona 段和 SOUL 记忆段不同
- 每条返回作为一行写入 `comments` 表（`is_main=0`），前端按评论流形式展示
- TodoTool 是独立可选工具，只从公开 post 抽取待办，不由 SOUL 回复产出
- 用户可随时把某个 SOUL 的 `enabled` 切到 0；后续 post 不再触发该 SOUL，但历史评论保留可读

成本与延迟控制：
- 第一期内置 2—3 个默认 SOUL，避免一次 5—10 倍 token 消耗
- 多 SOUL 调用并发执行（asyncio / threadpool），用户感知 ≈ 单次调用最慢者
- 首屏可只渲染最先返回的两条，其余通过流式追加

### 4.6 主 SOUL（Agent 项目第三期使用）

第三期 Agent 项目对外是单一智能体形象、需要主导工具调用，因此引入"主 SOUL"概念：

- 主 SOUL 由 `meta.main_soul` 字段标记，必须指向一个 `souls.enabled=1` 的 SOUL
- Agent 模式下：仅主 SOUL 负责主回复 + 工具调用，其余启用 SOUL 仍可作为旁观评论者写入 `comments`，标记 `is_main=0`
- 主 SOUL 切换由 `switch_soul(name)` 工具完成（详见 §10.3）
- 交互项目不读 `meta.main_soul`；即使被设置过，社交媒体形态下所有 SOUL 仍平权评论

---

## 5. user.md 格式规范

### 5.1 设计原则

- **整文档统一编辑**：不再用双 marker 把 user.md 划成"AI 段 / 用户段"。整篇都是一份用户档案，AI 反思器和用户都可以编辑任何条目，任何条目也可以由任一方新增、删除、改写。
- **结构是 H2 章节 + 列表项**：每个 H2（`##`）是一个章节，章节内的条目是 markdown 列表项（一行一条）或一段连续的描述文字。条目就是 patch 的最小单位。
- **章节带敏感度元数据**：每个章节通过 frontmatter 或 HTML 注释挂一个 `sensitivity` 标记（`high` / `normal`），决定 AI 自动落盘前使用普通阈值还是更高阈值。
- **每次写入都进内部留痕**：所有改动（无论 AI 或用户）都落 `user_md_revisions` 表，用于调试和事故恢复；前端默认只展示当前画像。

### 5.2 文件结构

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
- 学校：南京大学 <!-- id: bf-school -->
- 入学：2025-09 <!-- id: bf-enroll -->
- 时区：Asia/Shanghai <!-- id: bf-tz -->
- 主要使用时段：21:00—01:00 <!-- id: bf-active -->

## 关键身份
- 本科生 <!-- id: ki-undergrad -->
- 信息管理学院 2025 级 <!-- id: ki-school-major -->
- 校园 AI 社团成员 <!-- id: ki-club -->

## 身份与现状
<!-- id: status-main -->
你正处在大一下学期，刚开始适应密集的课程节奏。除了课内学习，你正在
准备 EL 大赛和江苏 AIGC 大赛，并把 TraceLog 作为主要参赛项目。

## 技能与专长
- Python 后端开发，熟悉 FastAPI 和 SQLite <!-- id: sk-py -->
- 对 LLM 应用工程有持续深入的兴趣 <!-- id: sk-llm -->
- 文字表达能力强，常用比喻把复杂概念说清楚 <!-- id: sk-writing -->

## 兴趣与习惯
- 喜欢深夜写代码和构思产品 <!-- id: hb-night -->
- 偏好长文记录而非碎片输入 <!-- id: hb-longform -->
- 周末倾向"深度沉思"而非密集社交 <!-- id: hb-solitude -->

## 关注的核心人际关系
- 队友：本次 EL 大赛同组，分工偏前端 <!-- id: rel-teammate -->
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

### 5.3 敏感度分级与写入策略

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

### 5.4 条目级 patch 协议

反思器和前端共用同一份 patch schema。匹配键是条目 anchor（§5.2），不是正文字符串。

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

### 5.5 内部写入留痕表

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

这两张表已在 §3.2 schema 中列出；初始化 schema 需要一并建表。

### 5.6 与旧设计的关系

- 旧版 `<!-- USER_FACTS -->` / `<!-- AI_PROFILE -->` marker：**废弃**。新版 `user.md` 直接使用章节 + sensitivity frontmatter。
- 旧版 `flush_profile` 全量重写逻辑：**替换**为 patch 协议。

---

## 6. 写入流程

### 6.1 用户发帖的完整链路

```
用户输入 user_input
    │
    ▼
[1] RecordService.save_post(user_input)
    │   - 生成 post_id（YYYYMMDD-NNN）
    │   - 写 state.db.posts 表（不再绑定单一 SOUL）
    │   - FTS5 双表通过 trigger 自动同步
    │   - ChromaDB.upsert(id=post_id, document=user_input)
    │
    ▼
[2] ContextBuilder.build_context(user_input)
    │   - 读 souls 表 enabled=1 的 SOUL 列表（按 sort_order）
    │   - 读 user.md
    │   - 为每个启用 SOUL 读取 soul_memories/<name>.md
    │   - FTS5 + ChromaDB 双轨检索 top-k 相关历史
    │   - 读最近若干条 post（时间近邻）
    │   - Todo 工具开启时读取活跃待办
    │   - 输出：共享上下文 + 启用 SOUL 列表 + 每个 SOUL 的私有记忆
    │
    ▼
[3] ReplyService.fanout(post_id, shared_context, enabled_souls)
    │   并发对每个启用 SOUL 调用 LLM：
    │   ├─ system prompt = 该 SOUL 的人格段 + 该 SOUL 的相处记忆 + 共享上下文
    │   ├─ 返回 reply
    │   └─ 写一行到 state.db.comments（post_id, soul_name, content）
    │
    │   主 SOUL 规则：
    │   - 交互项目：所有评论 is_main=0，全部并列展示
    │   - Agent 项目：仅 meta.main_soul 指向的 SOUL 写 is_main=1，
    │     主流程优先用主 SOUL 的 reply 走工具调用
    │
    ▼
[4] TodoTool.run_for_post(post_id)
    │   - 可选开启，默认开启
    │   - 只读取当前公开 post + 当前活跃待办
    │   - 输出 todos_to_upsert / todos_to_delete，并写入 todos.source_post
    │   - 与 SOUL 回复、私聊、评论线程解耦
    │
    ▼
[5] 前端展示评论流
    │   - 按 souls.sort_order 渲染评论卡片
    │   - 流式：先到先显示，最慢者补位
    │
    ▼
[6] Reflector.spawn_async(post_id)  ← 关键：异步，不阻塞前端
        │
        ▼ （在后台线程）
        - 读这条 post + 最近 N 条 post
        - 调用便宜模型（gpt-4o-mini）做"轻反思"
        - 输出 JSON: { entities: [...], emotions: [...], events: [...], importance: 0.7 }
        - 写 state.db.entities / post_entities / emotions / events
        - 更新 posts.importance
        - 可选：根据该 post 下各 SOUL 的评论，异步更新对应 soul_memories/<name>.md
        - 如果达到深反思触发条件 → 同时跑深反思（见 §8）
```

### 6.2 反思器输出 schema

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

### 6.3 失败与重试

| 步骤 | 失败处理 |
| --- | --- |
| 写 posts | 整个流程失败，返回错误 |
| 写 FTS5 | trigger 自动处理，正常不会失败 |
| ChromaDB upsert | 记录到 `meta.pending_embedding:<post_id>`，下次启动重试 |
| 单个 SOUL 评论 LLM 调用失败 | 仅丢弃该 SOUL 的评论，其他 SOUL 正常落 comments；前端给该 SOUL 卡片标灰，提供"重试"按钮 |
| 全部 SOUL 评论失败 | 已落盘的 post 不动，前端在评论流位置展示"AI 暂时无法回复，可重试"，不阻塞用户继续发帖 |
| 主 SOUL 调用失败（Agent 项目） | 工具调用入口失效，降级为纯评论；提示用户重试或临时切主 SOUL |
| 反思器 | 后台线程吞掉异常，记日志，下次启动可批量补跑 |

### 6.4 私聊流程

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

### 6.5 私聊与公开评论的边界

| 维度 | 公开评论（comments） | 私聊（chat_messages） |
| --- | --- | --- |
| 触发 | 用户发 post → 所有启用 SOUL 各评一条 | 用户在某条线程里给单个 SOUL 发消息 |
| 可见性 | 同帖下所有 SOUL 平权可见 | 仅当前 SOUL + 用户 |
| 进 ChromaDB / FTS5 | ✗（仅 posts 进） | ✗（明确不进，避免污染语义检索） |
| 进反思器 | 评论本身不进全局画像；可进入对应 SOUL 的相处记忆 | 不进全局画像；摘要只进入当前 SOUL 的相处记忆 |
| 待办抽取 | TodoTool 仅从公开 post 抽取 | 不从私聊抽取 |
| SOUL 切换 | 跟 souls.enabled 联动 | SOUL 被 disable 后旧线程只读，无法继续追加 |

---

## 7. 读取流程：双轨检索 + RRF

### 7.1 检索路由

接到查询时，先决定走哪条路或两条都走：

用户 / Agent 发起 `query` 后，`Query Router` 按以下规则选择检索路径：

- 含具体名词、日期、人名：FTS5 优先。
- FTS5 优先触发条件：`query` 中含 `entities` 表里的名字 / 含 ISO 日期 / 长度 ≤ 6 字。
- 抽象、情绪、状态描述：ChromaDB 优先。
- ChromaDB 优先触发条件：包含"感觉""觉得""为什么""最近""那种"等模糊词。
- 其他 / 默认：双轨 + RRF 融合。

第一期可以简化成"全部走双轨 + RRF"，省去 router 实现。

### 7.2 中文 vs 英文：FTS5 双表选择

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

### 7.3 ChromaDB 语义检索

保留现状，无需改动：

```python
def vector_search(query: str, k: int = 10) -> list[tuple[str, float]]:
    """返回 [(post_id, distance), ...] distance 越小越相关"""
    results = chroma_collection.query(query_texts=[query], n_results=k)
    return list(zip(results["ids"][0], results["distances"][0]))
```

### 7.4 RRF 融合

```python
def hybrid_search(query: str, k: int = 5) -> list[str]:
    fts_hits = fts_search(query, k=20)         # [(id, rank), ...]
    vec_hits = vector_search(query, k=20)      # [(id, dist), ...]

    scores: dict[str, float] = {}
    RRF_K = 60  # 论文默认值

    for rank, (post_id, _) in enumerate(fts_hits, start=1):
        scores[post_id] = scores.get(post_id, 0) + 1.0 / (RRF_K + rank)

    for rank, (post_id, _) in enumerate(vec_hits, start=1):
        scores[post_id] = scores.get(post_id, 0) + 1.0 / (RRF_K + rank)

    return [pid for pid, _ in sorted(scores.items(), key=lambda x: -x[1])[:k]]
```

### 7.5 三因子重排（第二期）

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

### 7.6 三层"读取智能"分配

参考 Hermes 的"投资写入、简化读取"哲学，按成本递增分配：

| 场景 | 用什么 | 原因 |
| --- | --- | --- |
| 默认每次发帖 | 共享上下文（user.md + 检索）+ 每个启用 SOUL 各注入一次人格段和相处记忆 | 0 额外检索成本，仅多算 N 次 LLM |
| 私聊每次发消息 | 该 SOUL 人格 + 该 SOUL 相处记忆 + user.md + 当前 thread 历史 + 对 posts 的 RRF 检索 + 该 SOUL 历史评论 | 中成本：检索一次，单次 LLM |
| 当前 post 找相关历史 | ChromaDB top-3（保持现状） | 低成本 |
| 用户追问"我之前是不是说过…" | RRF 双轨混合 | 中成本 |
| Agent 工具调用 `search_memory` | RRF + 三因子重排 | 高成本 |

---

## 8. 反思器（Reflector）设计

### 8.1 三类反思

| 层级 | 触发 | 输入 | 输出 | 频率 |
| --- | --- | --- | --- | --- |
| 轻反思 | 每条 post 写入后 | 当前 post + 最近 5 条 post + user.md「关键身份/关注的核心人际关系」两节作为已知实体词典 | entities / post_entities / emotions / events / posts.importance / relations 增量 | 每帖 |
| 全局深反思 | 可配置触发条件 + 用户手动触发 | 触发范围内所有 post 的轻反思聚合（不读 raw posts 原文） + 当前 user.md | reflection 文档 + user.md 条目级 patch（按章节 sensitivity 选择阈值后直落） + relations 衰减/归一化 | 可配置 |
| SOUL 记忆反思 | 每个 SOUL 的私聊/评论达到阈值 + 用户手动触发 | 该 SOUL 的最近私聊摘要 + 该 SOUL 的历史评论 + 当前 soul_memories/<name>.md + 必要的 user.md 硬事实 | soul_memories/<name>.md 条目级 patch + soul_memory_revisions | 按 SOUL 独立触发 |

私聊与反思器的关系：

- **轻反思不读私聊**。每次私聊消息发送时不触发全局轻反思，避免噪声进派生表。
- **全局深反思不读私聊**。全局 `user.md` 主要由 post 证据更新，避免私聊里的玩笑、附和或情绪化表达污染共享画像。
- **SOUL 记忆反思读私聊摘要而非原文**。每个 SOUL 独立把自己的私聊压缩成 ≤ 500 字摘要，再更新对应 `soul_memories/<name>.md`。原始私聊不进 FTS5 / ChromaDB，也不被其他 SOUL 读取。
- **私聊摘要写到 reflections 表**（type='soul_chat_digest'，metadata 带 `soul_name`），便于追溯哪份摘要影响了哪份 SOUL 记忆。

### 8.2 实现思路（参考 Hermes background_review）

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

### 8.2.1 轻反思 prompt 与输出 schema

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

#### 字段 → §3.2 派生表的写入映射

| 输出字段 | 写入表 / 列 | 说明 |
| --- | --- | --- |
| `entities[]` | `entities` (UNIQUE(type,name) upsert) + `post_entities` | upsert 后取 entity_id 写 post_entities；`first_seen` 仅首次写，`last_seen = post.ts`，`mention_count += 1` |
| `entities[].aliases` | `entities.aliases`（JSON 数组合并去重） | 不覆盖，只追加新别名 |
| `emotions[]` | `emotions` (PK = post_id+label) | 同 post 同 label 取最大 intensity |
| `events[]` | `events`（每条一行） | 不去重；同事件多次出现以 ts 区分 |
| `relations[]` | `relations` 累加 strength | 见下方 §8.2.1.relations |
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

### 8.2.2 派生表幂等约定

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

`relations_log` 已在 §3.2 schema 中列出；实现时需要保持该表与 `relations` 的幂等更新一致。核心字段如下：

```sql
CREATE TABLE IF NOT EXISTS relations_log (
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    delta       REAL NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (post_id, relation_id)
);
```

### 8.3 深反思触发策略

第一期采用最简单的触发：CLI 退出时对上次深反思后的公开记录生成一次深反思；后续前端再提供手动点击"生成深反思"。

后续再把触发策略做成可配置项，例如按记录数量、时间间隔、用户主动请求或后台任务触发。具体频率不写死为固定周期，由产品形态和成本预算决定。

### 8.4 深反思 prompt 模板要点

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

### 8.5 SOUL 记忆反思 prompt 模板要点

```
你是 TraceLog 的 SOUL 记忆反思器。你的任务不是更新全局 user.md，
而是更新某一个 SOUL 与用户之间的相处记忆。

## 输入
- soul_name: 当前 SOUL 名称
- soul_style: souls/<name>.md 的人格摘要
- current_soul_memory: soul_memories/<name>.md
- recent_private_chat_summary: 该 SOUL 最近私聊摘要
- recent_public_comments: 该 SOUL 对用户 posts 的最近评论
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

---

## 9. 数据导出（数据主权 v2 兜底）

第一期必做：

```bash
tracelog export --format=markdown --output=./my-tracelog-backup
```

输出结构：

```
my-tracelog-backup/
├── posts/
│   ├── 20260523-001.md       # 含 frontmatter
│   └── ...
├── reflections/
│   ├── weekly-2026-W21.md
│   └── ...
├── user.md                   # 直接复制
├── souls/                    # 直接复制
│   └── ...
├── soul_memories/            # 直接复制
│   └── ...
├── todos.json                # 从 SQLite 导出
└── entities.json             # 从 SQLite 导出（可选）
```

实现要点：
- 单文件 SQLite 一行命令导出，零依赖
- 导出脚本 100—200 行 Python
- 在 README 和 PPT 上明确写：用户任何时候都能"还原"成 Markdown 文件结构

---

## 10. 三阶段实施清单

### 10.1 第一期：5/23—5/31（江苏 AIGC 报名前）

目标：**给 v3 第一阶段提供技术拆解；5/31 前以 demo 能跑起来 + 至少一个新能力可演示为准，未完成项顺延到第二期**

核心技术项（按优先级推进，不要求全部卡在 5/31 前完成）：

- [x] 建立 `state.db` 与所有表（含 FTS5 双表、`souls` 表、`comments` 表、`user_md_revisions`、`soul_memory_revisions`）
- [x] 实现 `schema.sql` 作为唯一 SQLite 初始化脚本
- [x] 实现 `core/db.py` 封装连接 + WAL + 重试（参考 Hermes `hermes_state.py`）
- [x] 抽出正式 `RecordService.save_post`：双写 SQLite + ChromaDB，ChromaDB 失败时记录 `pending_embedding:<post_id>` 等待补索引
- [x] CLI 相关历史检索接入 FTS5 + ChromaDB + RRF hybrid 检索（由 `core/retrieval.py` 承担）
- [x] `ContextBuilder`：读启用 SOUL 列表 + user.md，组装共享上下文与每个 SOUL 的私有记忆（当前 CLI 只使用共享上下文，SOUL 列表留给下一步 `ReplyService`）
- [x] 运行时初始化 `workspace/souls/默认.md` 和 `workspace/souls/毒舌好友.md`，默认写入 `state.db.souls(enabled=1)`
- [x] 运行时为每个默认 SOUL 创建 `workspace/soul_memories/<name>.md` 空模板
- [x] `ReplyService.fanout`：对启用 SOUL 列表并发调用 LLM，把每条 reply 写入 `comments`
- [x] 启动时扫描 `souls/` 与 `souls` 表 upsert，缺失文件自动 `enabled=0`（由 `SoulService.sync_souls` 承担）
- [x] 抽出完整 `SoulService`：启用/禁用、排序、新建/编辑 SOUL
- [x] `SoulMemoryService`：加载/保存 `soul_memories/<name>.md`，写 `soul_memory_revisions`
- [x] CLI 至少能展示多个 SOUL 的评论流
- [x] TodoTool 从公开 post 独立抽取待办，写入 `todos.source_post`
- [x] `chat_threads` / `chat_messages` 表 + `ChatService`：与单个 SOUL 私聊、按 thread 加载历史
- [x] CLI 私聊命令：`/chat <soul>` 进入线程，`/chat list` 看线程列表
- [x] 私聊检索：用 thread 最近若干轮做 query 对 posts 走 RRF + 拉取该 SOUL 历史评论
- [ ] 私聊摘要进入对应 `soul_memories/<name>.md`，不进入全局 `user.md`
- [x] 私聊不触发 TodoTool；待办工具作为可选工具只处理公开 post
- [x] `ProfileService.apply_patch`：解析 sensitivity 阈值 → 直落或丢弃；写 `user_md_revisions`
- [x] 轻反思最简版（同步实现也可以，第一期不强求异步）：每帖抽取 entities + emotions + events + importance
- [x] 深反思最简版：CLI 退出时触发，生成 reflection 并写入 `reflections`
- [x] 深反思增强：CLI 退出触发，生成 reflection + user.md patch，normal 自动落盘，high 使用更高阈值自动落盘
- [x] 将核心记忆、LLM 路由与向量索引模块完整迁移进 `core/`
- [ ] `tracelog export --format=markdown` 命令
- [ ] FastAPI 后端接口暴露
- [ ] Web 前端最小可用版本：记录、时间线、AI 回复、待办、画像、搜索
- [ ] SOUL 管理页：启用/禁用开关、排序、新建/编辑 SOUL
- [ ] 私聊页：按 SOUL 进入私聊线程，展示消息流
- [ ] 准备 demo 数据集（10—20 条 post 覆盖典型场景）
- [ ] 录制基础演示视频
- [ ] PPT 写"分层记忆架构"页（参考本文 §1.1 的图）

可选（视时间）：

- [ ] 异步轻反思（用 threading）
- [ ] 三因子重排
- [x] 深反思输出多 patch，并接入 ProfileService.apply_patch
- [ ] 前端可视化：情绪曲线、关系图、实体提及频次

不做：

- [ ] 主 SOUL / Agent 模式（第三期再做）
- [ ] 自动定时反思

### 10.2 第二期：6—7 月（EL 交互组）

- [ ] Web 前端体验打磨：动效、移动端适配、加载/错误状态、演示数据切换
- [ ] 画像页增强：展示当前 `user.md` 的所有章节和条目，支持新增、编辑、删除、排序
- [ ] 画像页保存确认：用户完成任意章节修改后，点击保存时弹一次简单确认
- [ ] 画像页 sensitivity 配置：用户可对任一章节调高/调低敏感度
- [ ] 深反思输出多 patch（替代第一期可能存在的全文反思文档简化版）
- [ ] SOUL 管理页增强：查看每个 SOUL 的历史评论
- [ ] SOUL 记忆页：查看/编辑每个 `soul_memories/<name>.md`，展示该 SOUL 的相处记忆历史
- [ ] 多 SOUL 评论流式渲染：评论按返回顺序 / sort_order 渐次出现
- [ ] 私聊页增强：左栏线程列表（按 SOUL 分组，未读数与最近一条预览）+ 引用 post 卡片
- [ ] 私聊新建线程的 UX：在 SOUL 管理页 / 评论卡片右上角"私聊"入口直达
- [ ] SOUL 记忆反思：周期内私聊摘要 → reflections 表（type='soul_chat_digest'）→ 进对应 `soul_memories/<name>.md`
- [ ] 异步轻反思 + 失败重试队列
- [ ] 三因子重排上线
- [ ] 情绪曲线、实体提及频次、关系图可视化
- [ ] 大学生场景模板：课程、DDL、社团、竞赛
- [ ] 移动端适配

### 10.3 第三期：7—9 月（EL Agent 组）

把记忆系统作为 Agent tools 暴露给 Coze（火山引擎扣子）：

- [ ] Tool: `search_memory(query, mode="hybrid|fts|vector")`
- [ ] Tool: `query_entity(name, type)`
- [ ] Tool: `get_emotion_trend(days)`
- [ ] Tool: `list_todos(filter)`
- [ ] Tool: `add_todo(...)` / `update_todo(...)` / `complete_todo(id)`
- [ ] Tool: `generate_reflection(scope="custom")`
- [ ] Tool: `set_main_soul(name)` / `get_main_soul()`：设置或读取 Agent 模式下的主 SOUL（写 `meta.main_soul`）
- [ ] Tool: `list_souls(enabled_only)` / `enable_soul(name)` / `disable_soul(name)`：让 Agent 自助管理评论团队
- [ ] Tool: `list_chat_threads(soul_name?)` / `get_chat_history(thread_id, limit)`：Agent 读私聊
- [ ] Tool: `send_chat_message(thread_id, content)` / `start_chat_thread(soul_name, title?)`：Agent 替用户在私聊里推进话题或开新线程
- [ ] Tool: `add_post(content)` （Agent 也能帮用户记录）
- [ ] Tool: `read_user_md(section?)` / `propose_user_md_patch(patch)`：Agent 读取或对 user.md 提交 patch（沿用 sensitivity 阈值规则）
- [ ] Coze 工作流：成长教练、复盘助手、目标拆解
- [ ] 部署后端 API 到云端供 Coze 调用

---

## 11. 关键设计决策摘要

| 决策 | 选择 | 主要理由 |
| --- | --- | --- |
| posts 存哪 | SQLite，不再保留 Markdown 文件 | 前端编辑路径成立，文件冗余无价值 |
| SOUL 存哪 | `souls/*.md` 文件库 + `souls` 表管理启用/排序 | 文件可分享、可模板化；DB 表负责状态切换不刷文件系统 |
| SOUL 记忆存哪 | `soul_memories/*.md` + `soul_memory_revisions` | 借鉴 Hermes/Honcho 的 peer-specific memory 思路，但完全本地实现；每个 SOUL 根据自己的风格形成不同理解 |
| SOUL 调用模型 | 交互项目默认所有启用 SOUL 并发评论；Agent 项目可指定主 SOUL | 入口层是社交媒体形态，多 AI 好友并列才有"群聊"质感；Agent 形态需要单一智能体出口 |
| 私聊与主记忆 | 私聊独立存 `chat_threads` / `chat_messages`，不写 posts，不进 ChromaDB / FTS5；不直接进全局 `user.md`；SOUL 深反思只沉淀到对应 `soul_memories/<name>.md`；不触发 TodoTool | 私聊噪声大，进检索池、全局画像或待办工具都会污染主流程；但它能塑造某个 SOUL 与用户的关系记忆 |
| 用户档案/画像 | 合并为 `user.md`，AI 与用户共同编辑，章节带 sensitivity；high 章节 AI 改动需更高置信度 | 一个文件心智更清爽；AI 与用户对等编辑权但对硬事实加保险栏 |
| 检索 | FTS5（双 tokenizer）+ ChromaDB + RRF | 中文必须 trigram；语义必须向量 |
| 反思 | 异步 LLM agent，参考 Hermes background_review | 轻反思每帖、深反思按可配置条件触发 |
| 第三方 memory provider | 不接入 | TraceLog 的记忆是产品核心，不能把事实源和画像权交给第三方；只参考其接口思想 |
| 数据主权 | "v2"叙事：本地数据库 + 一键导出 | 失去 .md 编辑性，但导出补回来 |
| 第一期 | SQLite + souls + 双轨检索 + Web 前端最小闭环 + 一键导出 | 报名材料能讲清楚 + demo 跑得起来 |
| 第二期 | 增量 patch + 异步反思 + 前端体验增强 | EL 交互组持续打磨 |
| 第三期 | Tools 暴露给 Coze | EL Agent 组的核心能力 |

---

## 12. 风险与回退

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 5/31 前完成不了 | 江苏 AIGC 报名材料只能讲旧架构 | 报名材料先讲设计 + 部分实现，正式开发期完成 |
| trigram FTS5 性能不达预期 | 中文检索慢 | 限制 query 长度、增加 ChromaDB 比重 |
| 反思器抽取质量差 | 实体/情绪表充满噪声 | 第一期只做 demo 数据，第二期再加人工反馈循环 |
| 异步反思导致并发 bug | post 已落盘但派生数据缺失 | 第一期同步实现，第二期再异步化 |
| ChromaDB 与 SQLite 不一致 | 检索结果偏 | 启动时校验 chroma 计数 vs posts 计数，差异时重建 |
| 用户改 souls/*.md 后状态混乱 | 启用集合与文件不一致 | 启动时扫描 `souls/` 与 `souls` 表对账，缺文件的 SOUL 自动 `enabled=0` |
| 多 SOUL 全启用导致延迟与成本上升 | 单帖触发 N 倍 token | 第一期内置 SOUL 控制在 2—3 个；并发执行；前端流式渲染先到先显示；`enabled` 默认值由用户自行调整 |
| SOUL 记忆被私聊噪声带偏 | 某个 SOUL 对用户的理解变得片面 | 私聊摘要 prompt 强调"短期情绪 vs 稳定偏好"；SOUL 记忆写入保留内部留痕；全局 user.md 不读私聊摘要 |
| 用户与 AI 对同一条目同时编辑 | 后写覆盖前写 | 内部留痕保留写入快照；前端以当前画像为准，用户可直接再次编辑 |
| post 待办重复 | 同一 deadline 被记两次 | TodoTool 只从公开 post 抽取；合并键统一为 (task, date, start_time)，前端展示时显示 `source_post` |

---

## 13. 后续可选扩展

第一—三期之后，TraceLog 还能基于本架构生长出：

- **多 SOUL 圆桌**：在已并发评论基础上加二轮——让 2—3 个 SOUL 互相回应彼此评论，形成对话
- **角色导入市场**：导入用户分享的 SOUL（结合 EL 流量点）
- **关系图谱可视化**：用 networkx 从 entities + relations 构造交互式图
- **目标管理**：在 SQLite 加 `goals` 表，反思器追踪进展
- **跨设备同步**：SQLite + souls/ + soul_memories/ + user.md 打包到 iCloud/Dropbox/Git
- **声音输入**：voice memo 转文字后走标准 post 流程
- **多语言**：trigram FTS5 已经支持中日韩，扩展英中混排只需调整 router
