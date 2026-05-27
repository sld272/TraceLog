# TraceLog 数据库 Schema 参考

本文档是 TraceLog 的 SQLite 数据库 Schema 完整定义，涵盖所有表结构、FTS5 全文索引、触发器与设计说明。

## 1. 核心 DDL

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 元数据：单 key-value 表，存 schema_version 等
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Schema 版本，便于后续升级
INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '2');
-- 注意：交互项目（社交媒体形态）默认所有 enabled=1 的 SOUL 都参与评论，
-- 因此不再使用单一 active_soul。交互项目默认所有 enabled=1 的 SOUL 都平权参与评论。

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
    is_main     INTEGER NOT NULL DEFAULT 0,
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

## 2. 派生数据表

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
    strength    REAL DEFAULT 0.5,            -- 0—1，由轻反思 delta + 深反思衰减共同维护
    last_seen   TEXT,
    metadata    TEXT,
    UNIQUE(entity_a, entity_b, rel_type)
);

-- 关系强度变更日志：轻反思幂等所需
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
    type        TEXT NOT NULL,               -- global_deep / soul_deep / per_post / daily / weekly / monthly / event
    scope_start TEXT,                        -- 反思覆盖的起始时间（per_post 留空）
    scope_end   TEXT,
    content     TEXT NOT NULL,               -- 反思正文（Markdown）
    related_posts TEXT,                      -- JSON: ["20260523-001", ...]
    metadata    TEXT
);

CREATE INDEX IF NOT EXISTS idx_reflections_ts   ON reflections(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_type ON reflections(type);

-- 私聊会话线程：用户与某个 SOUL 的一条独立对话
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

-- 待办
CREATE TABLE IF NOT EXISTS todos (
    id          TEXT PRIMARY KEY,            -- uuid 或自增字符串
    task        TEXT NOT NULL,
    date        TEXT,                        -- YYYY-MM-DD 或 NULL
    start_time  TEXT,                        -- HH:MM 或 NULL
    end_time    TEXT,
    status      TEXT NOT NULL DEFAULT '未完成',  -- 未完成 / 已完成
    source_post TEXT REFERENCES posts(id) ON DELETE SET NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, date);
CREATE INDEX IF NOT EXISTS idx_todos_date   ON todos(date);

-- user.md 内部写入留痕：每次落盘的整文件快照 + 触发该次写入的 patch
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

## 3. 为什么 FTS5 用 external content 模式

`content='posts', content_rowid='rowid'` 这两行让 FTS5 不真的存正文副本，只存 token 化索引。优点：

- 磁盘占用减半
- 正文唯一真相在 `posts.content` 字段
- 自动通过 trigger 保持同步

注意：external content 表删除或更新索引时，不能用普通 `DELETE FROM posts_fts WHERE rowid = ...`。删除旧索引需要向 FTS5 表插入特殊的 `'delete'` 指令，并带上 `old.content`，让 FTS5 知道要从倒排索引里移除哪些旧 token。否则在 post 被编辑或删除后，旧关键词可能残留在搜索结果里。

中文 trigram 表也用同样模式，再省一份。

## 4. 为什么 chat_messages 不进 FTS5 / ChromaDB

私聊与 posts 是两类不同性质的内容：post 是用户的主动表达，反思器的核心素材；私聊是 SOUL 在某条线程内的语境化对话，含大量寒暄、追问与重复表达。把它们混进同一份索引会污染语义检索：用户在 post 里搜"最近为什么烦"时，最不希望命中的就是十轮"还好吗 / 没事就好"的私聊残片。

因此：

- 私聊检索仅按线程顺序加载（最近 N 条 + token 上限截断），不建语义索引。
- 全局画像反思器只读 `posts` 及其派生表；私聊不直接进入全局 `user.md`。
- SOUL 记忆反思器可读取该 SOUL 的 `chat_messages` 摘要，并把沉淀写入对应的 `soul_memories/<name>.md`。
- 私聊不写 `comments` 表（评论是公开评论流，私聊是单独频道）。

## 5. Observation 数据底座

本节描述当前已落入 `schema.sql` 的 Observation 数据底座。它提供存储、边界过滤、FTS、游标和证据清理能力；公开 post 轻反思已经会写入 `global` observation，评论线程与私聊也会通过 cursor 增量写入 `post_visible` / `soul_scoped` observation。Memory Retrieval v1 已接入 narrative 层召回；Progressive Disclosure 和 Consolidation 仍是后续阶段。

Observation 是 raw evidence 与深反思之间的中层记忆单位。它只保存可检索、可过滤、可审计的信号；具体原始证据统一写入 `observation_sources`。

### 5.1 概念表

当前 Schema 包含四类 Observation 表：

- `observations`：中层记忆主表，保存 narrative、时间、状态和权限边界。
- `observation_sources`：证据链表，保存具体来源类型、来源 ID、摘录和展开权限。
- `observations_fts`：observation 标题、摘要和 narrative 的 FTS5 trigram 索引。
- `observation_cursors`：按来源增量提取 observation 的游标，避免崩溃或 Ctrl+C 后丢失待提取内容。

`observations` 只保存检索与权限边界字段，不保存所有具体来源 ID：

```sql
CREATE TABLE IF NOT EXISTS observations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    type              TEXT NOT NULL,
    title             TEXT NOT NULL,
    summary           TEXT,
    narrative         TEXT NOT NULL,
    source_channel    TEXT NOT NULL,
    visibility_scope  TEXT NOT NULL,
    scope_post_id     TEXT REFERENCES posts(id) ON DELETE CASCADE,
    scope_soul_name   TEXT REFERENCES souls(name) ON DELETE CASCADE,
    importance        REAL NOT NULL DEFAULT 0.5,
    confidence        REAL NOT NULL DEFAULT 0.5,
    status            TEXT NOT NULL DEFAULT 'active',
    merged_into       INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    superseded_by     INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    observed_at       REAL NOT NULL,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    metadata          TEXT
);
```

实际 schema 通过 `CHECK` 约束冻结 observation type、source channel、visibility、status、importance/confidence 范围和 scope 必填规则：`post_visible` 必须有 `scope_post_id`，`soul_scoped` 必须有 `scope_soul_name`，`global` 不能带 `scope_soul_name`。

建议索引：

```sql
CREATE INDEX IF NOT EXISTS idx_observations_status_time
    ON observations(status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_post_scope
    ON observations(visibility_scope, scope_post_id, status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_soul_scope
    ON observations(visibility_scope, scope_soul_name, status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_type_time
    ON observations(type, observed_at DESC);
```

`observations_fts` 使用 external content + `tokenize='trigram'`。只有 `status='active'` 且 `visibility_scope!='private_blocked'` 的 observation 会被 trigger 写入 FTS；状态变成 `merged`、`superseded` 或 `archived` 时会被 trigger 从 FTS 删除。

`observation_sources` 保存具体证据来源。`source_id` 是多态 ID，因此不能依赖 SQLite foreign key 自动跨表级联：

```sql
CREATE TABLE IF NOT EXISTS observation_sources (
    observation_id  INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    source_type     TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    excerpt         TEXT,
    evidence_access TEXT NOT NULL,
    created_at      REAL NOT NULL,
    metadata        TEXT,
    PRIMARY KEY (observation_id, source_type, source_id)
);
```

实际 schema 通过 `CHECK` 约束冻结 `source_type` 和 `evidence_access` 枚举。由于 `source_id` 是多态 ID，SQLite 不能用单个 foreign key 自动级联到多张表；schema 已为 posts、comments、comment_messages、chat_messages、todos、reflections 设计 source cleanup triggers，`ObservationService.cleanup_orphan_observations()` 也提供启动时清理兜底，防止删除原文后残留无法访问的僵尸证据。

`observation_cursors` 用于 crash-safe 增量提取：

```sql
CREATE TABLE IF NOT EXISTS observation_cursors (
    source_kind      TEXT NOT NULL,
    source_key       TEXT NOT NULL,
    cursor_value     TEXT NOT NULL,
    updated_at       REAL NOT NULL,
    metadata         TEXT,
    PRIMARY KEY (source_kind, source_key)
);
```

游标推进必须与 observation 写入在同一个写事务提交。涉及 observation 写入、状态变更、consolidation、cursor advance 的事务使用 `core.db.immediate_transaction()`，避免并发下 deferred transaction 锁升级造成 `SQLITE_BUSY`。

当前实际使用的 cursor key：

| source_kind | source_key | cursor_value |
| --- | --- | --- |
| `chat_thread` | `chat_threads.id` 字符串 | 已处理的最大 `chat_messages.id` |
| `comment_thread` | `comment_threads.id` 字符串 | 已处理的最大 `comment_messages.id` |

线程 observation 提取成功后，即使本批次没有生成 observation，也会推进 cursor，避免无意义消息反复消耗 LLM。若 LLM 返回无效 JSON 或写入失败，cursor 不推进，下次按同一批次重试。

### 5.2 冻结枚举

Observation 类型：

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

可见性：

```text
global
post_visible
soul_scoped
private_blocked
```

证据展开权限：

```text
all
post_visible
source_soul_only
none
```

状态：

```text
active
merged
superseded
archived
```

### 5.3 来源边界规则

| 来源 | `visibility_scope` | scope 字段 | `evidence_access` |
| --- | --- | --- | --- |
| 公开 post | `global` | 无 | `all` |
| SOUL 首条公开评论 | `post_visible` | `scope_post_id` | `post_visible` |
| 评论线程消息 | `post_visible` | `scope_post_id` | `post_visible` |
| 私聊消息 | `soul_scoped` | `scope_soul_name` | `source_soul_only` |
| 不应记录内容 | `private_blocked` | 可选 | `none` |

私聊边界不用 `visible_to_souls` JSON 数组表达。第一版私聊 observation 绝对不跨 SOUL，因此 `soul_scoped` 使用可索引的 `scope_soul_name TEXT REFERENCES souls(name)`。

公开 post observation 的边界不由 LLM 决定。轻反思 parser 只接受 `type`、`title`、`summary`、`narrative`、`importance`、`confidence`，写入时由系统固定补齐 `source_channel='post'`、`visibility_scope='global'`、`source_type='post'`、`evidence_access='all'`。同一 post 轻反思重跑时会按 `source_type='post' + source_id=post_id` 替换旧 observation，避免重复堆积。

评论线程与私聊 observation 的边界同样不由 LLM 决定。线程提取 parser 只接受 `type`、`title`、`summary`、`narrative`、`importance`、`confidence`、`source_message_ids`。`source_message_ids` 必须来自本批次 user messages；assistant messages 只作为理解语境，不作为证据源。写入时系统固定补齐：

- 私聊：`source_channel='chat'`、`visibility_scope='soul_scoped'`、`scope_soul_name=<thread.soul_name>`、`source_type='chat_message'`、`evidence_access='source_soul_only'`
- 评论线程：`source_channel='comment_thread'`、`visibility_scope='post_visible'`、`scope_post_id=<thread.post_id>`、`source_type='comment_message'`、`evidence_access='post_visible'`

### 5.4 检索与向量化策略

Observation v1 使用：

- `observations_fts` 对 observation 标题、摘要、narrative 做关键词检索。
- 现有 post ChromaDB 召回 `post_id`，再通过 `observation_sources` 反查这些 post 关联的 `active` observations，作为间接语义召回。

第一版不对所有 observation 建向量索引。若后续引入 observation 向量索引，默认只允许 `global` / `post_visible` observation 向量化；`soul_scoped` 默认不向量化，除非未来引入本地 embedding 并另行设计；`private_blocked` 永不向量化。

FTS 查询必须 join `observations` 并先过滤：

- `status = 'active'`
- 当前场景允许的 `visibility_scope`
- 当前 `scope_post_id` 或 `scope_soul_name`

公开 post 回复和公开评论回复场景永远不能召回 `soul_scoped` observation，即使当前回复 SOUL 与 `scope_soul_name` 相同。

当前 Memory Retrieval v1 的召回矩阵：

| 场景 | FTS 允许范围 | 间接 post 语义范围 |
| --- | --- | --- |
| 公开 post 回复 | `global` | `global` |
| 私聊 | `global` + 当前 `scope_soul_name` 的 `soul_scoped` | `global` |
| 评论线程 | `global` + 当前 `scope_post_id` 的 `post_visible` | `global` |

v1 只把 observation id、type、title、summary、narrative 和 scope label 注入 `# 相关记忆`，不展开 `observation_sources.excerpt` 或任何 raw evidence。
