# TraceLog 项目概述

本文档介绍 TraceLog 的项目背景、产品定位、当前实现状态、架构总览、关键设计决策与风险。具体技术架构设计见 [architecture.md](./architecture.md)，数据库 Schema 见 [database.md](./database.md)。

## 1. 项目背景与定位

TraceLog 是一款面向学生与青年用户的陪伴型 AI 产品，由两条核心支柱构成，并以递进的方式形成完整产品逻辑：

- **入口层：向内的 AI 社交媒体** — 用最熟悉的“发帖”交互承接用户日常表达，让记录像发朋友圈一样轻。AI 是用户唯一的读者，解决“愿不愿意写”的问题。
- **价值层：AI 成长记忆引擎** — 把每一次轻量输入沉淀为长期可用的画像、记忆、待办与复盘，解决“写了有没有用”的问题。

### 与专业型 Agent 的赛道区分

TraceLog 与 Claude Code、Codex、OpenClaw 等专业型 Agent 不在同一赛道：

| 维度 | 专业型 Agent | TraceLog |
| --- | --- | --- |
| 用户定位 | 开发者、专业工作者 | 学生与青年用户 |
| 核心价值 | 提升专业生产力 | 陪伴个人生活与成长 |
| 交互形态 | 任务指令型 | 表达式、社交化、轻量化 |
| 数据沉淀 | 项目代码与工作产出 | 长期个人记忆与成长轨迹 |

## 2. 当前实现状态

当前 Web + CLI 双入口原型的核心闭环已基本跑通。默认入口是 Web，CLI 保留完整配置、管理与调试能力。核心 service 落位、SQLite + FTS5 + ChromaDB 三轨数据底座、SOUL 与私聊/评论双通道、轻反思与全局/SOUL 深反思、TodoTool 开关全部可用。

**已完成（按模块）**

- 数据底座：`schema.sql` 唯一 SQLite 初始化脚本，WAL、外键、FTS5/trigram 校验；项目未发布，不保留旧 schema 兼容。
- workspace 编排：初始化 workspace、`user.md`、默认 SOUL 与对应 `soul_memories/` 文件。
- 顶层入口：`main.py` 默认启动 Web，`cli` 子命令进入原 CLI；`core/cli/` 与 `core/web/` 分别承载 CLI 和 Web 启动细节。
- Web 应用：FastAPI API + Vite/React 前端覆盖首页 timeline、发帖、SOUL 首评、基于 `(post_id, soul_name)` 的内联评论对话、待办、反思预览/触发、SOUL 私聊和右侧摘要栏。
- 记录与检索：双写 SQLite + ChromaDB，支持 `pending_embedding` 重索引；FTS5 双 tokenizer + ChromaDB + 动态权重 hybrid scoring 融合。
- LLM 路由：按能力拆分为 reply / todo / reflection router，全部依赖 `LLMClient` Protocol。
- SOUL 体系：同步、列表、新建/编辑、启用/禁用、排序、缺失文件自动禁用；默认初始化“默认”与“毒舌好友”两个 SOUL。
- 评论与私聊：启用 SOUL 并发首评；单 SOUL 多轮评论对话与私聊。两者都不写 posts、不进 FTS5、不触发轻反思；评论和私聊消息会进入 ChromaDB 统一检索池。
- 回复上下文：公开 post 只注入 `user.md`、hybrid search 命中的 raw `# 相关帖子` 和活跃 todos；私聊/评论对话使用当前消息序列 + unified retrieval 命中的 `# 相关记忆`。
- 反思器：每帖轻反思只维护实体、情绪、事件、关系和 post importance；全局深反思直接读取 raw posts + 当前 `user.md` + todos，对画像做 confirm / revise / retract / add；SOUL 深反思直接读取该 SOUL 的 raw chat/comment messages，更新对应 `soul_memories/<name>.md`。
- 待办：TodoTool 严格只从公开 post 抽取，支持开关控制。
- 日志：本地 JSONL 日志默认记录完整 LLM prompt/response/parsed 结果，并为 query rewrite、FTS 构造、hybrid retrieval 和 context assembly 写结构化 debug trace；API key/token 仍会自动脱敏。
- 画像与长期记忆审计：sensitivity 阈值分级落盘，写 `user_md_revisions` / `soul_memory_revisions` 留痕；`memory_review_service` 已提供未来前端可复用的 `user.md` / `soul_memories` 用户覆盖写入与 revision 只读审计底座。
- 测试：覆盖 chat / comment / profile / reflector / todo / soul / cli / vectorstore / workspace。

**尚未完成 / 公开前后优先级**

- Web 设置界面：已支持 provider、模型、embedding、日志、SOUL 新建/启用/排序和 workspace 状态查看；首次无 `config.json` 时仍需 CLI 向导或手动配置让 API 完成启动，长期记忆正文编辑与 revision 浏览仍需补前端。
- 记忆前端：长期记忆编辑与 revision 审计已有 service/API 底座，但 Web UI 尚未完成。
- 新中间层记忆：旧版中间层已整体移除；后续需要重新设计更好的可检索、可修正、可审计中间层。
- 数据导出：`tracelog export` 一键打包尚未实现。
- demo 资产：预置演示数据脚本。
- 异步反思体验：API 后台任务已有基础，但深反思的前端状态、取消、重试和调度策略仍需打磨。
- 三因子重排、可视化等增强项。

## 3. 当前数据流

`post`（公开记录）链路：

```
Web Composer / CLI input
  → record_service.save_post                    # 写 posts，触发 FTS5 trigger
  → vectorstore.index_post                      # upsert ChromaDB；失败进入 pending_vector_doc
  → todo_service.run_for_post_safely            # TodoTool 开关时才跑，写 todos.source_post
  → reply_context.rewrite + retrieval.hybrid_search
  → context_builder.build_context               # user.md + raw 相关帖 + 活跃 todos
  → reply_service.fanout                        # 启用 SOUL 并发评论，写 comments
  → reflector.run_light_reflection_safely       # 轻反思 → entities/emotions/events/relations/importance
  → 手动触发或 CLI 退出
      → reflector.trigger_global_deep_reflection
        → raw posts + user.md + todos 对账 → reflections + profile_service.apply_patch
      → reflector.trigger_soul_deep_reflections
        → 每个 SOUL 读取自己的 raw chat/comment messages → soul_memories/<name>.md + revision
```

SOUL 私聊链路：按 thread 加载历史，检索统一 `# 相关记忆` 作为背景，调用单 SOUL，写 `chat_messages` 并索引到 ChromaDB。私聊不写 posts、不进 FTS5、不触发轻反思；其长期影响只可能在 SOUL 深反思中进入该 SOUL 自己的相处记忆。

评论对话链路：与私聊相似，但会话键就是 `(post_id, soul_name)`；`comments.seq=0` 是首评，后续追问/回复以内联形式长在 SOUL 首评下方，并由 SOUL 深反思统一吸收。

用户可控面当前收敛在最终长期记忆层：用户编辑 `user.md` 或 `soul_memories/<name>.md` 时会以 `source='user'` 写入 revision。系统不再暴露或维护旧版中间层、证据展开或整理状态。

## 4. 架构总览

### 4.1 三层记忆模型

TraceLog 的当前记忆系统分为三层，每一层的职责、存储介质、加载时机都独立：

**L1：人格与 SOUL 记忆层** — 存储 AI 好友的人格设定与相处记忆（`souls/*.md` + `soul_memories/*.md`）。每个 SOUL 有独立记忆，只在该 SOUL 被调用时注入，默认启用多 SOUL 并发评论。

**L2：身份与画像层** — 存储用户档案与成长画像（`user.md`），由 AI 反思器主动维护全文，用户在前端也可编辑。章节按 sensitivity（high / normal / low）分级：高敏章节需更高置信度才允许 AI 自动写入，`low` 用于「当前状态与关注」的快进快删。

**L3：结构化证据层** — 存储所有历史数据（`state.db` SQLite + `chroma_db/` 向量索引）。posts、comments、chat messages、entities、emotions、events、todos、reflections 等全部落 SQLite；语义向量索引 posts、comments 和 chat messages；关键词查询只走 posts 的 FTS5 双表。

**反思器** — 后台 LLM agent。轻反思每条 post 后抽取结构化信号；深反思按可配置触发策略直接读取 raw evidence，对 `user.md` / `soul_memories` 做条目级修正，而不是只追加摘要。

### 4.2 设计原则

1. **会进 system prompt 的 → Markdown 文件**（`souls/*.md`、`soul_memories/*.md`、`user.md`）：使用 prefix cache 友好，用户能直接看到自己的 AI 记忆。
2. **会被查询/聚合/统计的 → SQLite**（posts、entities、emotions、todos…）：跨条聚合、关联查询、时间趋势必须靠数据库，不进 system prompt。
3. **向量检索独立成层**：ChromaDB 与 SQLite 两者协同。
4. **反思器是 LLM agent 而非定时脚本**：参考 Hermes Agent，轻反思抽结构化信号，深反思做画像/相处记忆对账。
5. **数据主权**：所有数据本地，可一键导出、一键删除。

## 5. 关键设计决策

| 决策 | 选择 | 主要理由 |
| --- | --- | --- |
| posts 存哪 | SQLite，不再保留 Markdown 文件 | 前端编辑路径成立，文件冗余无价值 |
| SOUL 存哪 | `souls/*.md` 文件库 + `souls` 表管理启用/排序 | 文件可分享、可模板化；DB 表负责状态切换不刷文件系统 |
| SOUL 记忆存哪 | `soul_memories/*.md` + `soul_memory_revisions` | 借鉴 peer-specific memory 思路，每个 SOUL 根据自己的风格形成不同理解 |
| SOUL 调用模型 | 默认启用多 SOUL 并发评论 | 入口层是社交媒体形态，多 AI 好友并列才有“群聊”质感 |
| 私聊与主记忆 | 私聊独立存储，不写 posts，不进 FTS5；进入 ChromaDB 统一检索池但只允许当前 SOUL 检索；不直接进全局 `user.md` | 私聊不污染公开关键词检索和全局画像，但能在 SOUL 边界内塑造关系记忆 |
| 用户档案/画像 | 合并为 `user.md`，AI 反思器主动维护全文，用户可编辑，章节带 sensitivity | 一个文件心智更清爽；稳定画像保守写入，当前状态用 low 门槛积极增删 |
| 检索 | FTS5（双 tokenizer）+ ChromaDB + 动态权重 hybrid scoring | 中文必须 trigram；语义必须向量；融合分数保留单路强命中并奖励双路一致 |
| 回复上下文 | raw related posts，而不是中间层记忆摘要 | 先保持证据链干净，避免半成品中间层污染回复；后续重新设计更好的中间层 |
| 反思 | 直接从 raw evidence 对长期记忆做 reconcile | “反思”应修正已有画像/相处记忆，不只是抽取和堆叠 |
| 第三方 memory provider | 不接入 | TraceLog 的记忆是产品核心，不能把事实源和画像权交给第三方 |
| 数据主权 | 本地数据库 + 一键导出 | 可在前端编辑，或直接导出 |
| 用户记忆控制面 | 只编辑 `user.md` / `soul_memories` 最终长期记忆 | 用户能修正 AI 最终记住的内容，不需要理解内部派生表 |

## 6. 后续方向

TraceLog 还能基于本架构生长出：

- **新中间层记忆**：重新设计更干净的 memory unit，要求具备去重、更新、删除、证据边界和老化机制。
- **Entity Resolution**：实体重复、实体冲突和关系消歧仍是独立后续阶段。
- **多 SOUL 圆桌**：在已并发评论基础上加二轮，让 2-3 个 SOUL 互相回应彼此评论，形成对话。
- **角色导入市场**：导入用户分享的 SOUL。
- **关系图谱可视化**：从 entities + relations 构造交互式图。
- **目标管理**：在 SQLite 加 `goals` 表，反思器追踪进展。
- **跨设备同步**：SQLite + souls/ + soul_memories/ + user.md 打包到 iCloud/Dropbox/Git 等。
