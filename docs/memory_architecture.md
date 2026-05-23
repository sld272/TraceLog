# TraceLog 记忆系统架构设计 v3

本文档是 TraceLog 记忆系统的工程级技术设计，基于以下输入综合得出：

- 当前仓库现状（CLI + Markdown/JSON + ChromaDB）
- Hermes Agent（NousResearch）源码深度阅读
- TraceLog 的产品定位：面向普通学生与青年用户的陪伴型 AI 产品
- 三阶段参赛规划：江苏 AIGC（5/31 前）、EL 交互组（6—7 月）、EL Agent 组（7—9 月）

> 本架构同时承担两个角色：
> 1. 入口层"向内的 AI 社交媒体"的数据底座
> 2. 价值层"AI 成长记忆引擎"的核心实现

---

## 1. 总体架构

### 1.1 分层模型

TraceLog 的记忆系统分为四层，每一层的职责、存储介质、加载时机都独立：

```
┌─────────────────────────────────────────────────────────────┐
│  L1：人格层 (Persona Layer)                                   │
│  souls/*.md                                                   │
│  → 定义 AI 用什么语气和用户对话                                │
│  → 每会话整体注入 system prompt                                │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  L2：身份与画像层 (User & Profile Layer)                       │
│  user.md（合并了"用户档案"和"成长画像"两类内容）                 │
│  → 上半部分：用户主动填写的硬事实                                │
│  → 下半部分：反思器维护的软画像                                 │
│  → 每会话整体注入 system prompt                                │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  L3：结构化记忆层 (Structured Memory)                          │
│  state.db (SQLite)                                            │
│    posts / posts_fts / posts_fts_trigram                      │
│    entities / post_entities / emotions / events / relations   │
│    reflections / todos / meta                                 │
│  + chroma_db/ (向量索引)                                       │
│  → 关键词查询走 FTS5 双表（unicode61 + trigram）               │
│  → 语义查询走 ChromaDB                                         │
│  → 混合查询走 RRF 融合                                         │
│  → 通过工具调用按需查询，不进 system prompt                     │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  反思器 (Reflector) — 后台异步                                 │
│  每条 post 写入后 spawn 轻量 LLM agent                         │
│  读最近内容 → 抽取实体/情绪/事件 → 增量更新 user.md             │
│  周期任务（每周/每月）→ 生成 reflection 写入 reflections 表     │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 设计原则

1. **会进 system prompt 的 → Markdown 文件**（`souls/*.md`、`user.md`）
   - 理由：每会话整体加载，使用 prefix cache 友好
   - 理由：用户能直接看到自己的 AI 记忆，符合数据主权叙事
2. **会被查询/聚合/统计的 → SQLite**（posts、entities、emotions、todos…）
   - 理由：跨条聚合、关联查询、时间趋势必须靠数据库
   - 理由：不会塞进 system prompt，所以不需要可读性
3. **向量检索独立成层**：ChromaDB 不与 SQLite 二选一，两者协同
4. **反思器是 LLM agent 而非定时脚本**：参考 Hermes background_review，每条 post 后异步触发，让 AI 自己决定更新什么
5. **数据主权 v2**：所有数据本地、可一键导出、可一键备份、可一键删除

### 1.3 与 Hermes Agent 的关系

| 维度 | Hermes Agent | TraceLog |
| --- | --- | --- |
| 用户群 | 开发者 | 普通学生/青年 |
| L1 人格 | `SOUL.md`（单文件） | `souls/*.md`（多文件库） |
| L2 用户与笔记 | `USER.md` + `MEMORY.md`（分离） | `user.md`（合并） |
| L3 历史 | SQLite + FTS5 + trigram | SQLite + FTS5 + trigram + ChromaDB |
| 语义检索 | 外挂 provider 插件 | 内置 ChromaDB |
| 反思 | `background_review` daemon | 同款思路，更轻 |
| 容量限制 | 字符数硬限 | 软限（用户编辑权交还） |
| Prefix cache | 强约束（frozen snapshot） | 弱约束（中途可更新） |

**关键学习**：把人格和画像放在 .md 文件、用 trigram FTS5 处理中文、用后台 LLM agent 做反思——这三点直接借鉴 Hermes，已被生产环境验证。

---

## 2. 存储布局

### 2.1 目录结构

```
workspace/
├── state.db                  # 唯一的 SQLite 数据库
├── chroma_db/                # ChromaDB 向量索引（保留）
│   └── ...
├── user.md                   # 用户档案 + 成长画像（合并）
└── souls/                    # AI 人格库
    ├── default.md            # 默认人格
    ├── 毒舌闺蜜.md
    └── ...                   # 用户/社区自定义
```

### 2.2 文件 vs 数据库归属总表

| 数据 | 存储位置 | 进 system prompt | 由谁维护 |
| --- | --- | --- | --- |
| 当前激活的 SOUL | `souls/<active>.md` | ✓ 整体 | 用户切换 |
| 用户基本档案（姓名/时区/学校） | `user.md` 上半 | ✓ 整体 | 用户填写 / 前端表单 |
| 用户成长画像 | `user.md` 下半 | ✓ 整体 | 反思器维护 |
| 帖子原文 | `state.db` posts 表 | ✗（按需检索） | RecordService |
| 帖子关键词索引 | `state.db` FTS5 双表 | ✗ | trigger 自动同步 |
| 帖子语义向量 | `chroma_db/` | ✗ | RecordService |
| 实体、情绪、事件、关系 | `state.db` 派生表 | ✗ | 反思器 |
| 反思记录 | `state.db` reflections 表 | ✗（按需引用） | 反思器 |
| 待办 | `state.db` todos 表 | 部分（活跃待办进 prompt） | TodoService |
| 元数据（active_soul、schema_version） | `state.db` meta 表 | ✗ | 系统 |

---

## 3. SQLite Schema 完整定义

### 3.1 核心 DDL

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 元数据：单 key-value 表，存 active_soul、schema_version 等
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Schema 版本，便于后续迁移
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO meta(key, value) VALUES ('active_soul', 'default');

-- 帖子主表
CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,            -- 20260523-001
    ts          TEXT NOT NULL,               -- ISO 时间字符串
    content     TEXT NOT NULL,               -- 正文
    soul_used   TEXT,                        -- 当时激活的 SOUL 名
    importance  REAL DEFAULT 0.5,            -- 反思器打分 0—1
    reply       TEXT,                        -- AI 回复（可选保存）
    created_at  REAL NOT NULL,               -- unix timestamp，用于排序
    updated_at  REAL NOT NULL                -- 用于增量重建索引
);

CREATE INDEX IF NOT EXISTS idx_posts_ts          ON posts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_posts_importance  ON posts(importance DESC);
CREATE INDEX IF NOT EXISTS idx_posts_soul        ON posts(soul_used);

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
    DELETE FROM posts_fts         WHERE rowid = old.rowid;
    DELETE FROM posts_fts_trigram WHERE rowid = old.rowid;
END;

CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    DELETE FROM posts_fts         WHERE rowid = old.rowid;
    DELETE FROM posts_fts_trigram WHERE rowid = old.rowid;
    INSERT INTO posts_fts(rowid, content)         VALUES (new.rowid, new.content);
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
    rel_type    TEXT NOT NULL,               -- friend / classmate / teammate / mentor / family
    strength    REAL DEFAULT 0.5,            -- 0—1，反思器维护
    last_seen   TEXT,
    metadata    TEXT,
    UNIQUE(entity_a, entity_b, rel_type)
);

-- 反思记录
CREATE TABLE IF NOT EXISTS reflections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    type        TEXT NOT NULL,               -- per_post / daily / weekly / monthly / event
    scope_start TEXT,                        -- 反思覆盖的起始时间（per_post 留空）
    scope_end   TEXT,
    content     TEXT NOT NULL,               -- 反思正文（Markdown）
    related_posts TEXT,                      -- JSON: ["20260523-001", ...]
    metadata    TEXT
);

CREATE INDEX IF NOT EXISTS idx_reflections_ts   ON reflections(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_type ON reflections(type);

-- 待办（迁自 todos.json）
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
```

### 3.3 为什么 FTS5 用 `external content` 模式

`content='posts', content_rowid='rowid'` 这两行让 FTS5 不真的存正文副本，只存 token 化索引。优点：

- 磁盘占用减半
- 正文唯一真相在 `posts.content` 字段
- 自动通过 trigger 保持同步

中文 trigram 表也用同样模式，再省一份。

---

## 4. souls/*.md 格式规范

### 4.1 文件命名

- 文件名 = SOUL 名称，支持中文
- 默认 SOUL 必须是 `souls/default.md`
- 用户自定义示例：`souls/毒舌闺蜜.md`、`souls/林黛玉.md`、`souls/十年后的自己.md`
- 当前激活的 SOUL 由 `state.db.meta` 表的 `active_soul` 字段决定，不依赖文件名约定

### 4.2 文件格式

每个 SOUL 文件由 YAML frontmatter + Markdown 正文组成：

```markdown
---
name: 毒舌闺蜜
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

### 4.3 加载机制

启动时：
1. 读 `state.db.meta.active_soul` 得到 SOUL 名（如 `毒舌闺蜜`）
2. 读 `souls/毒舌闺蜜.md`
3. 解析 frontmatter（仅作为元数据，不进 prompt）
4. 把正文整体注入 system prompt 的 persona 段

切换 SOUL：
- 用户在前端选择 → 写入 `state.db.meta.active_soul`
- 下一次会话生效（与 Hermes 一致，保持当前 session prefix cache）
- 或在前端"立即切换" → 主动重建 system prompt（牺牲缓存换体验）

### 4.4 多 SOUL 同时激活（EL 阶段）

EL 交互组的"多 AI 好友评论"功能：
- 一篇 post 可以收到多个 SOUL 的评论
- 每个评论调用一次 LLM，system prompt 用对应 SOUL
- 数据存放：评论作为 reflection 子类型 `comment`，或单独建 `comments` 表

---

## 5. user.md 格式规范

### 5.1 文件结构

`user.md` 合并了原本 Hermes 中 `USER.md` 和 `MEMORY.md` 的角色，但内部用章节区分两类来源：

```markdown
# 用户档案

<!-- 上半部分：硬事实，由用户主动填写或前端表单维护 -->

## 基本信息
- 姓名：xxx
- 学校：南京大学
- 入学：2025-09
- 时区：Asia/Shanghai
- 主要使用时段：21:00—01:00

## 关键身份
- 本科生
- 信息管理学院 2025 级
- 校园 AI 社团成员

<!-- 下半部分：成长画像，由反思器自动维护 -->

## 身份与现状
你正处在大一下学期，刚开始适应密集的课程节奏。除了课内学习，你正在
准备 EL 大赛和江苏 AIGC 大赛，并把 TraceLog 作为主要参赛项目。

## 技能与专长
- Python 后端开发，熟悉 FastAPI 和 SQLite
- 对 LLM 应用工程有持续深入的兴趣
- 文字表达能力强，常用比喻把复杂概念说清楚

## 兴趣与习惯
- 喜欢深夜写代码和构思产品
- 偏好长文记录而非碎片输入
- 周末倾向"深度沉思"而非密集社交

## 关注的核心人际关系
- 队友：本次 EL 大赛同组，分工偏前端
- 导师：暑期可能加入研究小组的目标对象

## 性格与情绪倾向
- 自驱力强但容易陷入完美主义
- 在信息过载时倾向沉默而不是求助
- 接受直接反馈，反感空洞鼓励

## 长期目标与当前痛点
- 长期：以独立开发者身份做出有人用的产品
- 当前：兼顾比赛、课程、项目，时间分配焦虑
```

### 5.2 章节边界规范

为了让反思器知道"哪段它能改，哪段它不能动"，约定两块用 HTML 注释作为分隔符：

```markdown
<!-- USER_FACTS_BEGIN -->
## 基本信息
...
<!-- USER_FACTS_END -->

<!-- AI_PROFILE_BEGIN -->
## 身份与现状
...
<!-- AI_PROFILE_END -->
```

反思器只能修改 `AI_PROFILE_BEGIN`/`END` 之间的内容；用户只能在前端修改 `USER_FACTS` 段（前端读取时也基于这两个 marker）。

### 5.3 增量更新而非全量重写

当前 `router.py.flush_profile` 是"读旧画像 + 近期帖子，让 LLM 重写整篇"。新版本改为：

1. 反思器读最近 N 条 post（默认 7 天） + 读 `AI_PROFILE` 段当前内容
2. LLM 输出**章节级 patch**：`{"身份与现状": "新内容", "性格与情绪倾向": null /* 不改 */, ...}`
3. 主程序按章节合并写回 `AI_PROFILE` 段
4. 不动 `USER_FACTS` 段

好处：
- 永远不会丢历史段（除非 LLM 明确说"删除这段"）
- token 消耗稳定（每次只看变化）
- 用户能看到清晰的 diff

---

## 6. 写入流程

### 6.1 用户发帖的完整链路

```
用户输入 user_input
    │
    ▼
[1] RecordService.save_post(user_input)
    │   - 生成 post_id（YYYYMMDD-NNN）
    │   - 写 state.db.posts 表
    │   - FTS5 双表通过 trigger 自动同步
    │   - ChromaDB.upsert(id=post_id, document=user_input)
    │
    ▼
[2] ContextBuilder.build_context(user_input)
    │   - 读 souls/<active>.md
    │   - 读 user.md
    │   - FTS5 + ChromaDB 双轨检索 top-k 相关历史
    │   - 读最近若干条 post（时间近邻）
    │   - 读活跃待办
    │
    ▼
[3] LLM call（router.call_post_reply）
    │   - 返回 reply + todos_to_upsert + todos_to_delete
    │
    ▼
[4] 主流程更新 state.db.todos 表
    │
    ▼
[5] 前端展示 reply
    │
    ▼
[6] Reflector.spawn_async(post_id)  ← 关键：异步，不阻塞 reply
        │
        ▼ （在后台线程）
        - 读这条 post + 最近 N 条 post
        - 调用便宜模型（gpt-4o-mini）做"轻反思"
        - 输出 JSON: { entities: [...], emotions: [...], events: [...], importance: 0.7 }
        - 写 state.db.entities / post_entities / emotions / events
        - 更新 posts.importance
        - 如果触发周/月反思周期 → 同时跑深反思（见 §7）
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
| ChromaDB upsert | 记录到 `meta.pending_embeddings`，下次启动重试 |
| 主 reply LLM 调用 | 已落盘的 post 不动，提示用户重试 |
| 反思器 | 后台线程吞掉异常，记日志，下次启动可批量补跑 |

---

## 7. 读取流程：双轨检索 + RRF

### 7.1 检索路由

接到查询时，先决定走哪条路或两条都走：

```
用户/Agent 查询 query
        │
        ▼
   ┌────────────────────┐
   │  Query Router      │
   └────────────────────┘
        │
        ├── 含具体名词、日期、人名 → FTS5 优先
        │   触发条件：query 中含 entities 表里的名字 / 含 ISO 日期 / 长度 ≤ 6 字
        │
        ├── 抽象、情绪、状态描述 → ChromaDB 优先
        │   触发条件：包含"感觉""觉得""为什么""最近""那种"等模糊词
        │
        └── 其他/默认 → 双轨 + RRF 融合
```

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
| 默认每次对话 | system prompt 已包含 SOUL + user.md | 0 成本 |
| 当前 post 找相关历史 | ChromaDB top-3（保持现状） | 低成本 |
| 用户追问"我之前是不是说过…" | RRF 双轨混合 | 中成本 |
| Agent 工具调用 `search_memory` | RRF + 三因子重排 | 高成本 |

---

## 8. 反思器（Reflector）设计

### 8.1 双层反思

| 层级 | 触发 | 输入 | 输出 | 频率 |
| --- | --- | --- | --- | --- |
| 轻反思 | 每条 post 写入后 | 当前 post + 最近 3—5 条 | entities/emotions/events/importance | 每帖 |
| 深反思 | 周/月周期 + 用户手动触发 | 周期内所有 post + 当前 user.md | reflection 文档 + user.md AI_PROFILE 段 patch | 每周/月 |

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

### 8.3 深反思触发策略

第一期采用最简单的两种触发：

1. **手动触发**：用户在前端点"生成本周复盘"
2. **启动时检查**：程序启动时检查"上次周反思距今 ≥ 7 天"，触发一次

第二期再加 cron 风格定时任务（参考 Hermes cron 系统）。

### 8.4 深反思 prompt 模板要点

```
你是 TraceLog 的反思引擎。下面是用户最近 7 天的所有记录、
情绪标签、事件抽取，以及当前的成长画像。

## 你的任务
1. 生成一份本周反思（500—800 字），包含：
   - 主线事件回顾
   - 情绪与状态趋势
   - 与重要他人的互动
   - 进展、卡点、转折
   - 一条值得用户注意的洞察

2. 输出 user.md 中 AI_PROFILE 段的章节级 patch：
   - 仅修改有充足新证据支持的章节
   - 不删除已有信息，除非新数据明确反驳
   - 输出格式见下方 schema

## 输出 JSON
{
  "reflection_md": "...",
  "profile_patch": {
    "身份与现状": "新版本内容 或 null（不改）",
    "性格与情绪倾向": "...",
    ...
  }
}
```

主程序拿到结果：
1. 写入 `reflections` 表
2. 把 `reflection_md` 单独导出为 Markdown 文件（可选）
3. 按章节合并 `profile_patch` 到 `user.md` 的 `AI_PROFILE` 段

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
├── todos.json                # 从 SQLite 导出
└── entities.json             # 从 SQLite 导出（可选）
```

实现要点：
- 单文件 SQLite 一行命令导出，零依赖
- 导出脚本 100—200 行 Python
- 在 README 和 PPT 上明确写：用户任何时候都能"还原"成 Markdown 文件结构

第二期可加 `tracelog import`：从导出包回灌进新机器。

---

## 10. 三阶段实施清单

### 10.1 第一期：5/23—5/31（江苏 AIGC 报名前）

目标：**报名材料里能讲清楚新架构 + demo 能跑起来 + 至少一个新能力可演示**

必做：

- [ ] 建立 `state.db` 与所有表（含 FTS5 双表）
- [ ] 实现 `migrations/001_init.sql`
- [ ] 实现 `core/db.py` 封装连接 + WAL + 重试（参考 Hermes `hermes_state.py`）
- [ ] `RecordService.save_post`：双写 SQLite + ChromaDB
- [ ] `ContextBuilder`：读 SOUL、user.md，调用 RRF 双轨检索
- [ ] 引入 `souls/default.md` 和 `souls/毒舌闺蜜.md`，前端切换
- [ ] `meta.active_soul` 字段切换逻辑
- [ ] 把 `todos.json` 一次性迁移到 `todos` 表
- [ ] 轻反思最简版（同步实现也可以，第一期不强求异步）：每帖抽取 entities + emotions
- [ ] 手动周反思命令 `/reflect week`
- [ ] `tracelog export --format=markdown` 命令
- [ ] 准备 demo 数据集（10—20 条 post 覆盖典型场景）
- [ ] 录制基础演示视频
- [ ] PPT 写"分层记忆架构"页（参考本文 §1.1 的图）

可选（视时间）：

- [ ] 异步轻反思（用 threading）
- [ ] 三因子重排
- [ ] user.md 增量 patch（第一期可继续用全量重写，PPT 上讲"未来增量化"）

不做：

- [ ] 多 SOUL 同时激活（EL 阶段）
- [ ] 前端可视化（情绪曲线、关系图）
- [ ] 自动定时反思

### 10.2 第二期：6—7 月（EL 交互组）

- [ ] FastAPI 后端接口暴露
- [ ] Web 前端：记录、时间线、画像、待办、复盘、搜索
- [ ] user.md 真正的章节级增量 patch
- [ ] 多 SOUL 评论功能（可选 1—3 个 SOUL 同时评论一帖）
- [ ] 异步轻反思 + 失败重试队列
- [ ] 三因子重排上线
- [ ] 情绪曲线、实体提及频次、关系图可视化
- [ ] 大学生场景模板：课程、DDL、社团、竞赛
- [ ] 移动端适配
- [ ] `tracelog import` 命令

### 10.3 第三期：7—9 月（EL Agent 组）

把记忆系统作为 Agent tools 暴露给 Coze（火山引擎扣子）：

- [ ] Tool: `search_memory(query, mode="hybrid|fts|vector")`
- [ ] Tool: `query_entity(name, type)`
- [ ] Tool: `get_emotion_trend(days)`
- [ ] Tool: `list_todos(filter)`
- [ ] Tool: `add_todo(...)` / `update_todo(...)` / `complete_todo(id)`
- [ ] Tool: `generate_reflection(scope="week|month|custom")`
- [ ] Tool: `switch_soul(name)`
- [ ] Tool: `add_post(content)` （Agent 也能帮用户记录）
- [ ] Coze 工作流：成长教练、复盘助手、目标拆解
- [ ] 部署后端 API 到云端供 Coze 调用

---

## 11. 与现有代码的迁移

### 11.1 当前文件的归宿

| 当前文件 | 归宿 |
| --- | --- |
| `main.py` | 保留 CLI 入口，业务逻辑下沉到 `core/` |
| `memory.py` | 拆解：帖子部分 → `core/record_service.py`，画像 → `core/profile_service.py`，待办 → `core/todo_service.py` |
| `router.py` | 拆解：reply → `core/reply_service.py`，flush → `core/reflector.py` |
| `vectorstore.py` | 重命名为 `core/vector_index.py`，接口不变 |
| `workspace/posts/*.md` | 一次性脚本导入 SQLite，迁移完成后归档 |
| `workspace/profile.md` | 重命名/合并到 `workspace/user.md`，加章节标记 |
| `workspace/todos.json` | 一次性脚本导入 SQLite |
| `workspace/chroma_db/` | 保留 |

### 11.2 一次性迁移脚本

```python
# scripts/migrate_to_v3.py
def migrate():
    db = init_state_db()  # 应用 migrations/001_init.sql

    # 1. 导入 posts
    for md in sorted(Path("workspace/posts").glob("*.md")):
        post_id, ts, content = parse_post_md(md)
        db.execute(
            "INSERT INTO posts(id, ts, content, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (post_id, ts, content, time.time(), time.time())
        )

    # 2. 导入 todos
    todos = json.load(open("workspace/todos.json"))
    for t in todos:
        db.execute(
            "INSERT INTO todos(id, task, date, start_time, end_time, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t["id"], t["task"], t.get("date"), t.get("start_time"),
             t.get("end_time"), t.get("status", "未完成"),
             time.time(), time.time())
        )

    # 3. 重命名 profile.md -> user.md，加上章节 marker
    profile = Path("workspace/profile.md").read_text(encoding="utf-8")
    user_md = (
        "# 用户档案\n\n"
        "<!-- USER_FACTS_BEGIN -->\n## 基本信息\n（暂无，可手动补充）\n"
        "<!-- USER_FACTS_END -->\n\n"
        "<!-- AI_PROFILE_BEGIN -->\n" + profile + "\n<!-- AI_PROFILE_END -->\n"
    )
    Path("workspace/user.md").write_text(user_md, encoding="utf-8")

    # 4. 创建默认 SOUL
    Path("workspace/souls").mkdir(exist_ok=True)
    Path("workspace/souls/default.md").write_text(DEFAULT_SOUL, encoding="utf-8")

    # 5. 标记完成
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('migrated_v3', '1')")
```

迁移后 `workspace/posts/` 和 `profile.md` `todos.json` 不删除，归档到 `workspace/_archive_pre_v3/`，留作回退保险。

---

## 12. 关键设计决策摘要

| 决策 | 选择 | 主要理由 |
| --- | --- | --- |
| posts 存哪 | SQLite，不再保留 Markdown 文件 | 前端编辑路径成立，文件冗余无价值 |
| SOUL 存哪 | `souls/*.md` 文件库 | 可分享、可模板化、整体进 prompt |
| 用户档案/画像 | 合并为 `user.md`，章节区分来源 | 一个文件心智更清爽 |
| 检索 | FTS5（双 tokenizer）+ ChromaDB + RRF | 中文必须 trigram；语义必须向量 |
| 反思 | 异步 LLM agent，参考 Hermes background_review | 轻反思每帖、深反思每周/月 |
| 数据主权 | "v2"叙事：本地数据库 + 一键导出 | 失去 .md 编辑性，但导出补回来 |
| 第一期 | SQLite + souls + 双轨检索 + 一键导出 | 报名材料能讲清楚 + demo 跑得起来 |
| 第二期 | 增量 patch + 异步反思 + 前端可视化 | EL 交互组重头戏 |
| 第三期 | Tools 暴露给 Coze | EL Agent 组的核心能力 |

---

## 13. 风险与回退

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 5/31 前完成不了 | 江苏 AIGC 报名材料只能讲旧架构 | 报名材料先讲设计 + 部分实现，正式开发期完成 |
| trigram FTS5 性能不达预期 | 中文检索慢 | 限制 query 长度、增加 ChromaDB 比重 |
| 反思器抽取质量差 | 实体/情绪表充满噪声 | 第一期只做 demo 数据，第二期再加人工反馈循环 |
| 异步反思导致并发 bug | post 已落盘但派生数据缺失 | 第一期同步实现，第二期再异步化 |
| ChromaDB 与 SQLite 不一致 | 检索结果偏 | 启动时校验 chroma 计数 vs posts 计数，差异时重建 |
| 用户改 souls/*.md 后切换混乱 | 加载旧人格 | 切换时检查文件存在性，不存在时退回 default |

---

## 14. 后续可选扩展

第一—三期之后，TraceLog 还能基于本架构生长出：

- **多 SOUL 圆桌**：让 2—3 个 SOUL 在同一帖下互相对话
- **角色导入市场**：导入用户分享的 SOUL（结合 EL 流量点）
- **关系图谱可视化**：用 networkx 从 entities + relations 构造交互式图
- **目标管理**：在 SQLite 加 `goals` 表，反思器追踪进展
- **跨设备同步**：SQLite + souls/ + user.md 打包到 iCloud/Dropbox/Git
- **外挂记忆 provider**：抽象 `MemoryProvider` 接口，允许接入 mem0/Honcho（参考 Hermes 插件机制）
- **声音输入**：voice memo 转文字后走标准 post 流程
- **多语言**：trigram FTS5 已经支持中日韩，扩展英中混排只需调整 router
