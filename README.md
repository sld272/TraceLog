# TraceLog ✦ 拾迹

> **向内运行的 AI 社交媒体，也是一台陪你长出自己的记忆引擎。**
>
> 在这里，你写给自己的每一句碎片都不会被算法裹挟向外推送，而是被一群"AI 好友"读到、回应、记住，并在你看不见的地方慢慢织成一份属于你的成长档案。

---

## 🌱 核心理念

TraceLog 同时承担两个角色：

1. **向内的 AI 社交媒体**——把社交媒体的"发帖—评论—互动"那套熟悉手感，反向用于自我表达。你发出的内容只给自己看，评论来自一群可被你定义的 AI 人格（SOUL）。
2. **AI 成长记忆引擎**——所有发出的内容、衍生出的情绪/事件/人物/关系，都会沉淀进一套分层记忆系统，让 AI 真正"认识你"，并随你一起变化。

底层信念：

- **数据主权 v2**：所有数据本地存储，可一键导出为 Markdown，可一键删除，可在 iCloud / Dropbox / Git 之间自由同步。
- **可读优先**：会进 system prompt 的（人格、用户档案）一律是 Markdown 文件，你随时能打开看；只用于查询/聚合的（帖子、实体、情绪、待办）才落进 SQLite。
- **AI 与用户对等编辑**：用户档案 `user.md` 不再分"AI 段 / 用户段"，整篇是一份共享文档，AI 反思器和你都能编辑任何条目，硬事实类章节通过敏感度分级再加一道审核栏。
- **反思而非缓存**：AI 不是在被动堆积聊天记录，而是在每条新内容后异步抽取信号，并在每周/每月主动复盘、更新它对你的理解。

---

## 🏗️ 主体结构：四层 + 一个反思器

```
┌─────────────────────────────────────────────────────────────┐
│  L1 人格层  souls/*.md                                      │
│    多个"AI 好友"，每个都是一份 Markdown                       │
│    社交媒体形态：所有启用 SOUL 对每条 post 各发一条评论       │
│    Agent 形态：可在启用集合内指定一个主 SOUL                  │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  L2 身份与画像层  user.md                                    │
│    基本信息 + 成长画像合并到一份文档                          │
│    章节带 sensitivity：normal 直接落盘，high 经审核才合并     │
│    所有写入有 revision 历史，可时间线回滚                     │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  L3 结构化记忆层  state.db (SQLite) + chroma_db/             │
│    posts / comments / chat_messages                          │
│    entities / emotions / events / relations                  │
│    todos / reflections / souls / meta                        │
│    FTS5 双 tokenizer（unicode61 + trigram）+ 向量索引         │
│    关键词 / 语义 / 混合（RRF）三种检索模式                    │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  反思器 Reflector — 后台异步                                 │
│    轻反思：每条 post 抽实体/情绪/事件/重要性/关系增量         │
│    深反思：每周/每月聚合 + 私聊摘要 + 当前画像               │
│            → 输出反思文档 + user.md 条目级 patch              │
└─────────────────────────────────────────────────────────────┘
```

### 三种交互通道

- **Posts（默认主线）**：你写一条内容，所有启用的 SOUL 各自给一条评论。
- **私聊（Per-SOUL）**：单独和某个 SOUL 进多线程对话。私聊与主记忆**物理隔离**，不进检索池，避免寒暄污染语义搜索；深反思只读私聊的 LLM 摘要，不读原文。
- **待办**：从 post 与私聊里同步抽取，统一汇入 todos 表，活跃待办自动注入 system prompt。

### 反思器的两层

| 层级 | 触发 | 做什么 |
| --- | --- | --- |
| 轻反思 | 每条 post 写入后 | 抽 entities / emotions / events / importance / relations 增量；填 L3 派生表 |
| 深反思 | 每周/每月 + 用户手动触发 | 聚合周期内的轻反思 + 私聊摘要 + 当前 user.md → 生成 500—800 字反思文档；对 user.md 输出条目级 patch（按 sensitivity 决定直落或入审核） |

每次画像更新都附带 evidence（post_id 列表）+ confidence；不达阈值的 patch 直接丢弃并记日志。

---

## 🧭 三阶段路线图

### 第一期 · 江苏 AIGC 报名前
- 把 `state.db` 与所有派生表落地，Markdown/JSON 旧数据迁移到 SQLite + ChromaDB
- 多 SOUL 并发评论：每条 post 让所有启用 SOUL 各发一条
- 私聊基础能力：CLI 与单个 SOUL 多线程私聊
- 轻反思最简版（同步实现亦可），手动触发的周反思
- `tracelog export` 一键导出整套 Markdown 备份

### 第二期 · EL 交互组
- FastAPI 后端 + Web 前端：时间线、画像、待办、复盘、搜索、私聊
- 画像页：所有条目就地编辑、新增、删除、拖拽排序；AI 待审核区 + 历史版本时间线
- 多 SOUL 评论流式渲染；SOUL 管理页（启用 / 排序 / 新建 / 编辑）
- 异步轻反思 + 失败重试队列；深反思读私聊 LLM 摘要
- 三因子（recency / importance / emotion）重排上线
- 情绪曲线、实体提及频次、关系图可视化

### 第三期 · EL Agent 组
把记忆系统作为 Agent tools 暴露给 Coze（火山引擎扣子）：

- `search_memory(query, mode="hybrid|fts|vector")`
- `query_entity(name, type)` / `get_emotion_trend(days)`
- `list_todos / add_todo / update_todo / complete_todo`
- `generate_reflection(scope="week|month|custom")`
- `list_souls / enable_soul / disable_soul / set_main_soul`
- `list_chat_threads / get_chat_history / send_chat_message`
- `read_user_md / propose_user_md_patch`

让"成长教练 / 复盘助手 / 目标拆解"这类工作流可以直接调用你的私人记忆。

---

## 🔮 后期愿景

记忆地基铺好之后，TraceLog 的天花板远不止"日记 + 画像"：

- **多 SOUL 圆桌**：在并发评论之上加一轮，让 2—3 个 SOUL 互相回应彼此的评论，形成自发对话。
- **角色市场**：导入并分享社区贡献的 SOUL，把"AI 好友"做成可流通的内容资产。
- **关系图谱**：用 networkx 把 entities + relations 渲成交互式人物关系图。
- **目标管理**：goals 表 + 反思器追踪进展，从"画像"演化为"目标推进系统"。
- **跨设备同步**：SQLite + souls/ + user.md 打包到 iCloud / Dropbox / Git，无需中心服务。
- **外挂记忆 provider**：抽象 `MemoryProvider` 接口，允许接入 mem0 / Honcho 等第三方记忆服务。
- **声音输入**：voice memo 转文字后走标准 post 流程，让记录不再被键盘绑死。
- **多语言**：trigram FTS5 天然支持中日韩，扩展中英混排只需调整 router。

最终愿景：TraceLog 不只是你的记忆容器，而是**一面随时间长出回声的镜子**——你写出的每一段散碎心情，都会在未来某个时刻以更完整的样貌走回到你面前。

---

## 📁 仓库结构

- `docs/memory_architecture.md` — 工程级技术设计文档（v3）
- `docs/competition_roadmap.md` — 三阶段参赛路线
- `main.py` / `router.py` / `memory.py` / `vectorstore.py` — 当前 CLI 原型，向新架构演进中

完整设计请阅 [`docs/memory_architecture.md`](docs/memory_architecture.md)。

---

**TraceLog 拾迹** — *致敬每一份不被遗忘的成长。*
