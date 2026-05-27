# TraceLog 项目概述

本文档介绍 TraceLog 的项目背景、产品定位、当前实现状态、架构总览、关键设计决策与风险。具体技术架构设计见 [architecture.md](./architecture.md)，数据库 Schema 见 [database.md](./database.md)。

## 1. 项目背景与定位

TraceLog 是一款面向学生与青年用户的陪伴型 AI 产品，由两条核心支柱构成，并以递进的方式形成完整产品逻辑：

- **入口层：向内的 AI 社交媒体** — 用最熟悉的"发帖"交互承接用户日常表达，让记录像发朋友圈一样轻。AI 是用户唯一的读者，解决"愿不愿意写"的问题。
- **价值层：AI 成长记忆引擎** — 把每一次轻量输入沉淀为长期可用的画像、记忆、待办与复盘，解决"写了有没有用"的问题。

### 与专业型 Agent 的赛道区分

TraceLog 与 Claude Code, Codex, OpenClaw 等专业型 Agent 不在同一赛道：

| 维度 | 专业型 Agent | TraceLog |
| --- | --- | --- |
| 用户定位 | 开发者、专业工作者 | 学生与青年用户 |
| 核心价值 | 提升专业生产力 | 陪伴个人生活与成长 |
| 交互形态 | 任务指令型 | 表达式、社交化、轻量化 |
| 数据沉淀 | 项目代码与工作产出 | 长期个人记忆与成长轨迹 |

## 2. 当前实现状态

代码侧 v3 第一阶段的 CLI 闭环已基本跑通。核心 service 落位、SQLite + FTS5 + ChromaDB 三轨数据底座、SOUL 与私聊/评论双通道、轻反思与全局/SOUL 深反思、TodoTool 开关全部可用。

**已完成（按模块）**

- 数据底座：`schema.sql` 唯一 SQLite 初始化脚本，WAL、外键、FTS5/trigram 校验。
- workspace 编排：初始化 workspace、`user.md`、默认 SOUL 与对应 `soul_memories/` 文件。
- CLI 入口：`main.py` 极薄入口，`core/cli/` 拆为 app、config、commands、sessions。
- 记录与检索：双写 SQLite + ChromaDB，支持 `pending_embedding` 重索引；FTS5 双 tokenizer + ChromaDB + RRF 融合。
- LLM 路由：按能力拆分为 reply / todo / reflection router，全部依赖 `LLMClient` Protocol。
- SOUL 体系：同步、列表、新建/编辑、启用/禁用、排序、缺失文件自动禁用；默认初始化"默认"与"毒舌好友"两个 SOUL。
- 评论与私聊：启用 SOUL 并发评论；单 SOUL 多轮评论线程与私聊。两者都不写 posts、不进 FTS5/ChromaDB、不触发轻反思。
- Observation 数据底座、提取器、Memory Retrieval v1 与 Progressive Disclosure v1：已创建 `observations` / `observation_sources` / `observations_fts` / `observation_cursors`，公开 post 轻反思会写入 `global` observation，评论线程与私聊会通过 cursor 增量提取 `post_visible` / `soul_scoped` observation；回复上下文已接入 L1 narrative 召回，并按权限展开少量 L2 evidence excerpt。
- 待办：TodoTool 严格只从公开 post 抽取，支持开关控制。
- 反思器：每帖轻反思（含 pending 重试）、CLI 退出时全局深反思、按 SOUL 增量游标的 SOUL 深反思。
- 画像：sensitivity 阈值分级落盘，写 `user_md_revisions` 留痕。
- 测试：覆盖 chat / comment / profile / reflector / todo / soul / cli / vectorstore / workspace。

**尚未完成**

- API 与前端：FastAPI 后端、Web 前端最小闭环。
- Observation 上层流程：尚未实现 Consolidation。
- 数据导出：`tracelog export` 一键打包。
- demo 资产：预置演示数据脚本。
- 异步反思队列：当前同步跑在主线程。
- 三因子重排、可视化等增强项。

## 3. 当前数据流

`post`（公开记录）链路：

```
read_cli_input
  → retrieval.hybrid_search(top_k=3)            # FTS5 双表 + ChromaDB → 动态权重 hybrid scoring
  → context_builder.build_context               # user.md + 启用 SOUL + 相关 post + 活跃 todos
  → record_service.save_post                    # 写 posts，触发 FTS5 trigger，upsert ChromaDB
  → todo_service.run_for_post_safely            # TodoTool 开关时才跑，写 todos.source_post
  → reply_service.fanout                        # 启用 SOUL 并发评论，写 comments
  → reflector.run_light_reflection_safely       # 轻反思 → entities/emotions/events/observations，失败入 pending
  → CLI 退出
      → reflector.trigger_global_deep_reflection
        → 写 reflections + profile_service.apply_patch（normal / high 双阈值）
      → reflector.trigger_pending_soul_deep_reflections
        → 每个 SOUL 读原始私聊+评论消息 → soul_memories/<name>.md + revision
```

`/chat <soul>` 私聊链路：仅按 thread 加载历史 + 检索 posts/相关评论，调用单 SOUL，写 `chat_messages`，不触发任何反思与待办。

`/comment <post> <soul>` 评论线程链路：与私聊对称，但 thread 与 root comment 绑定到 `comment_threads`，由 SOUL 深反思统一吸收。

公开 post 已会在轻反思阶段生成 `global` observation；评论线程和私聊会在 CLI 启动/退出时通过 cursor 增量生成 `post_visible` / `soul_scoped` observation。Memory Retrieval v1 会把允许边界内的 observation narrative 注入 `# 相关记忆`，Progressive Disclosure v1 只在权限允许时展开少量 `observation_sources.excerpt`，不读取完整原文。

## 4. 架构总览

### 4.1 四层记忆模型

TraceLog 的记忆系统分为四层，每一层的职责、存储介质、加载时机都独立：

**L1：人格与 SOUL 记忆层** — 存储 AI 好友的人格设定与相处记忆（`souls/*.md` + `soul_memories/*.md`）。每个 SOUL 有独立记忆，只在该 SOUL 被调用时注入，默认启用多 SOUL 并发评论。

**L2：身份与画像层** — 存储用户档案与成长画像（`user.md`），由 AI 反思器主动维护全文，用户在前端也可编辑。章节按敏感度（high / normal）分级：高敏章节需更高置信度才允许 AI 自动写入。每会话整体注入 system prompt。

**L3：结构化记忆层** — 存储所有历史数据（`state.db` SQLite + `chroma_db/` 向量索引）。posts、comments、entities、emotions、events、todos、reflections 等全部落 SQLite；语义向量落 ChromaDB；关键词查询走 FTS5 双表，混合查询走 RRF 融合。

**反思器** — 后台异步 LLM agent。轻反思每条 post 后抽取实体/情绪/事件和公开 post observation；深反思按可配置触发策略聚合，生成 reflection 并更新 `user.md`。

### 4.2 设计原则

1. **会进 system prompt 的 → Markdown 文件**（`souls/*.md`、`soul_memories/*.md`、`user.md`）— 使用 prefix cache 友好，用户能直接看到自己的 AI 记忆，符合数据主权叙事。
2. **会被查询/聚合/统计的 → SQLite**（posts、entities、emotions、todos…）— 跨条聚合、关联查询、时间趋势必须靠数据库，不进 system prompt。
3. **向量检索独立成层**：ChromaDB 与 SQLite 两者协同。
4. **反思器是 LLM agent 而非定时脚本**：参考 Hermes Agent，每条 post 后异步抽取结构化信号，周期性深反思再更新画像。
5. **数据主权 v2**：所有数据本地，可一键导出、一键删除。

## 5. 关键设计决策

| 决策 | 选择 | 主要理由 |
| --- | --- | --- |
| posts 存哪 | SQLite，不再保留 Markdown 文件 | 前端编辑路径成立，文件冗余无价值 |
| SOUL 存哪 | `souls/*.md` 文件库 + `souls` 表管理启用/排序 | 文件可分享、可模板化；DB 表负责状态切换不刷文件系统 |
| SOUL 记忆存哪 | `soul_memories/*.md` + `soul_memory_revisions` | 借鉴 peer-specific memory 思路，每个 SOUL 根据自己的风格形成不同理解 |
| SOUL 调用模型 | 默认启用多 SOUL 并发评论 | 入口层是社交媒体形态，多 AI 好友并列才有"群聊"质感 |
| 私聊与主记忆 | 私聊独立存储，不写 posts，不进 ChromaDB / FTS5；不直接进全局 `user.md` | 私聊噪声大，进检索池或全局画像会污染主流程；但它能塑造 SOUL 与用户的关系记忆 |
| Observation 升级边界 | 公开叙事单元是 post，私密叙事单元是 chat_thread；私聊 observation 绝对不跨 SOUL | TraceLog 是社交媒体心智，不采用 chatbot 式 session；边界必须先于智能 |
| 用户档案/画像 | 合并为 `user.md`，AI 反思器主动维护全文，用户可编辑，章节带 sensitivity | 一个文件心智更清爽；AI 主动维护，高敏章节以更高置信度阈值自动写入 |
| 检索 | FTS5（双 tokenizer）+ ChromaDB + 动态权重 hybrid scoring | 中文必须 trigram；语义必须向量；融合分数保留单路强命中并奖励双路一致 |
| 反思 | 异步 LLM agent，参考 Hermes Agent | 轻反思每帖、深反思按可配置条件触发 |
| 第三方 memory provider | 不接入 | TraceLog 的记忆是产品核心，不能把事实源和画像权交给第三方 |
| 数据主权 | 本地数据库 + 一键导出 | 可在前端编辑，或直接导出 |

## 6. 后续方向

TraceLog 还能基于本架构生长出：

- **Observation 记忆系统**：公开 post、评论线程与私聊 observation 已开始沉淀并进入 Memory Retrieval v1 + Progressive Disclosure v1；后续可补 Consolidation，不在第一版向量化所有 observation
- **Observation Consolidation**：深反思之后按 visibility boundary 分桶执行 merge / supersede / promote；私聊 `soul_scoped` 永不进入全局 `user.md`
- **多 SOUL 圆桌**：在已并发评论基础上加二轮——让 2—3 个 SOUL 互相回应彼此评论，形成对话
- **角色导入市场**：导入用户分享的 SOUL
- **关系图谱可视化**：从 entities + relations 构造交互式图
- **目标管理**：在 SQLite 加 `goals` 表，反思器追踪进展
- **跨设备同步**：SQLite + souls/ + soul_memories/ + user.md 打包到 iCloud/Dropbox/Git 等
