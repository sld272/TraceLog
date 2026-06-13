# TraceLog 项目概述

本文档介绍 TraceLog 的项目背景、产品定位、当前实现状态、架构总览、关键设计决策与风险。具体技术架构设计见 [architecture.md](./architecture.md)，数据库 Schema 见 [database.md](./database.md)，API 说明见 [api.md](./api.md)。

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

当前 Web + CLI 双入口原型的核心闭环已基本跑通。默认入口是 Web；图片附件、图片识别、网页搜索、后台任务事件流、设置页和记忆工作台主要在 Web/API 路径中使用，CLI 保留文本公开记录、SOUL 管理、私聊、评论线程、工具开关与退出反思能力。核心 service 落位、SQLite + FTS5 + ChromaDB 三轨数据底座、SOUL 与私聊/评论双通道、轻反思与全局/SOUL 深反思、TodoTool 开关全部可用。

**已完成（按模块）**

- 数据底座：`schema.sql` 唯一 SQLite 初始化脚本，WAL、外键、FTS5/trigram 校验；`core.db.init_db()` 保留轻量向前迁移与回填（如 `post_soul_orders`），破坏性开发期变更仍可要求重建 workspace。
- workspace 编排：初始化 workspace、`user.md`、`拾迹者`、`温柔树洞`、`毒舌好友` 三个默认 SOUL 与对应 `soul_memories/` 文件。
- 顶层入口：`main.py` 默认启动 Web，`cli` 子命令进入原 CLI；`core/cli/` 与 `core/web/` 分别承载 CLI 和 Web 启动细节。
- Web 应用：FastAPI API + Vite/React 前端覆盖首页 timeline、发帖、SOUL 首评、基于 `(post_id, soul_name)` 的内联评论对话、待办、反思预览/触发、反思 job 跟踪/取消/重试、SOUL 私聊、右侧摘要栏、设置页、向量索引重试/对账和长期记忆工作台。
- 记录与检索：SQLite 是事实源，ChromaDB 是可重建向量索引；SQLite 维护 `vector_docs`、`vector_outbox`、collection 同步状态和内容 hash，Chroma collection 按 embedding 模型与 base URL 指纹隔离；FTS5 双 tokenizer + ChromaDB + 动态权重 hybrid scoring 融合。
- LLM 路由：按能力拆分为 reply / todo / reflection router，全部依赖 `LLMClient` Protocol。
- SOUL 体系：同步、列表、新建/编辑、启用/禁用、排序、缺失文件自动禁用；默认初始化“拾迹者”“温柔树洞”“毒舌好友”三个 SOUL。每个公开 post 会保存发帖当时的 SOUL 排序快照，旧 post 的首评展示不随之后的设置排序变化。
- 评论与私聊：启用 SOUL 并发首评；单 SOUL 多轮评论对话与私聊。首评生成完成顺序不是展示排序来源，同一 post 下的 SOUL 首评与评论会话按该 post 的 `post_soul_orders` 快照排列。两者都不写 posts、不进 FTS5、不触发轻反思；评论和私聊消息会进入 ChromaDB 统一检索池。
- 图片识别：JPEG/PNG 附件可生成客观视觉摘要，写入 `vision_cache`；公开 post 图片摘要会作为 `post_vision` 文档进入 ChromaDB，并进入回复与反思上下文。识图不可用时只保留“有图但不能看”的边界提示。
- 网页搜索：Web 设置页可开启 DuckDuckGo 或 Tavily；回复前由 LLM gate 判断是否搜索，搜索结果作为外部资料注入公开 post、私聊和评论回复上下文，不进入长期记忆。
- 回复上下文：公开 post 注入 `user.md`、hybrid search 命中的 raw `# 当前用户的历史相关帖子`、活跃 todos 和可选网页搜索结果；私聊/评论对话使用当前消息序列 + unified retrieval 命中的 `# 相关记忆`，评论对话还会注入同一 post 下其他 SOUL 已发生追问的公开评论片段。
- 反思器：每帖轻反思只维护实体、情绪、事件、关系和 post importance；全局深反思直接读取 raw posts + 当前 `user.md` + todos，对画像做 confirm / revise / retract / add；SOUL 深反思直接读取该 SOUL 的 raw chat/comment messages，更新对应 `soul_memories/<name>.md`。
- 待办：TodoTool 严格只从公开 post 抽取，支持开关控制。
- 日志：本地 JSONL 日志默认记录完整 LLM prompt/response/parsed 结果，并为 query rewrite、FTS 构造、hybrid retrieval 和 context assembly 写结构化 debug trace；API key/token 仍会自动脱敏。
- 画像与长期记忆审计：sensitivity 阈值分级落盘，写 `user_md_revisions` / `soul_memory_revisions` 留痕；Web 记忆工作台已支持结构化或 Markdown 全文编辑 `user.md` / `soul_memories`，反思页已支持最近整理记录、变更摘要和完整 revision 快照查看。
- API 后台任务：Web/API 公开 post pipeline 使用 SQLite `jobs` 表和 worker 队列领取任务；多 SOUL 首评仍在 `reply_service.fanout()` 内部并发。手动全局/SOUL 深反思也复用 `jobs`，并可通过 jobs API / Web 反思页跟踪、取消 pending job、重试 failed job。
- 测试：当前测试套件可用 `conda run -n tracelog python -m unittest discover tests` 运行，覆盖 API 管理、chat / comment / profile / reflector / todo / soul / cli / vectorstore / workspace / vision / web search。

**尚未完成 / 公开前后优先级**

- 记忆审计前端：长期记忆编辑、revision 浏览和完整快照查看已有 Web 工作台；revision 回滚 UI 尚未完成，diff 体验仍可继续打磨。
- 新中间层记忆：旧版中间层已整体移除；后续需要重新设计更好的可检索、可修正、可审计中间层。
- 数据导出：`tracelog export` 一键打包尚未实现。
- demo 资产：预置演示数据脚本。
- 异步反思体验：API 后台任务、前端状态、取消和重试已有基础；深反思调度策略、批量整理节奏和失败恢复文案仍需打磨。
- 缩略图生成、附件删除 UI、图片摘要人工校正 UI、三因子重排、可视化等增强项。

## 3. 当前数据流

Web/API `post`（公开记录）链路：

```
Web Composer / API request
  → record_service.save_post                    # 写 posts + post_soul_orders，触发 FTS5 trigger
  → job_service.enqueue                         # 排 embedding/reply/todo/light/deep jobs
  → JobWorker 串行领取 jobs
      → vector_docs / vector_outbox             # 正文进入 SQLite 向量账本，再同步到 ChromaDB
      → vision_service.describe_attachments     # 可选：图片摘要 → vision_cache + post_vision 向量文档
      → reply_context.rewrite + retrieval.hybrid_search
      → context_builder.build_context           # user.md + raw 历史相关帖 + 活跃 todos + 可选网页搜索
      → reply_service.fanout                    # 启用 SOUL 并发评论，写 comments，展示按 post 快照排序
      → todo_service.run_for_post_safely        # TodoTool 开关时才跑，写 todos.source_post
      → reflector.run_light_reflection_safely   # 轻反思 → entities/emotions/events/relations/importance
      → reflector.trigger_global_deep_reflection# 达到阈值或手动 job 时触发
```

CLI 的公开文本发帖路径不排 `jobs`，而是在交互循环中同步执行保存、检索、回复、TodoTool 和轻反思；退出 CLI 时再执行全局与 SOUL 深反思。

深反思触发入口包括 Web 手动触发、API 阈值触发和 CLI 退出：

```
reflector.trigger_global_deep_reflection
  → raw posts + user.md + todos 对账 → reflections + profile_service.apply_patch
reflector.trigger_soul_deep_reflections
  → 每个 SOUL 读取自己的 raw chat/comment messages → soul_memories/<name>.md + revision
```

SOUL 私聊链路：按 thread 加载历史，检索统一 `# 相关记忆` 作为背景，调用单 SOUL，写 `chat_messages` 并索引到 ChromaDB。私聊不写 posts、不进 FTS5、不触发轻反思；其长期影响只可能在 SOUL 深反思中进入该 SOUL 自己的相处记忆。

评论对话链路：与私聊相似，但会话键就是 `(post_id, soul_name)`；`comments.seq=0` 是首评，后续追问/回复以内联形式长在 SOUL 首评下方，并由 SOUL 深反思统一吸收。回复上下文会排除当前 post 自身的检索自引用，但会显式加入同一 post 下其他 SOUL 已发生追问的公开评论片段作为背景。同一 post 下不同 SOUL 的首评和会话入口按 `post_soul_orders` 的发帖时快照排列，单个 SOUL 会话内部仍按 `seq` 排。

用户可控面当前收敛在最终长期记忆层：用户编辑 `user.md` 或 `soul_memories/<name>.md` 时会以 `source='user'` 写入 revision。系统不再暴露或维护旧版中间层；前端引用记忆面板只展示回复时的 evidence metadata 与反馈入口，不是可编辑的中间层记忆。

## 4. 架构总览

### 4.1 三层记忆模型

TraceLog 的当前记忆系统分为三层，每一层的职责、存储介质、加载时机都独立：

**L1：人格与 SOUL 记忆层** — 存储 AI 好友的人格设定与相处记忆（`souls/*.md` + `soul_memories/*.md`）。每个 SOUL 有独立记忆，只在该 SOUL 被调用时注入，默认启用多 SOUL 并发评论。

**L2：身份与画像层** — 存储用户档案与成长画像（`user.md`），由 AI 反思器主动维护全文，用户在前端也可编辑。章节按 sensitivity（high / normal / low）分级：高敏章节需更高置信度才允许 AI 自动写入，`low` 用于「当前状态与关注」的快进快删。

**L3：结构化证据层** — 存储所有历史数据（`state.db` SQLite + `chroma_db/` 向量索引）。posts、post_soul_orders、comments、chat messages、attachments、vision_cache、entities、emotions、events、todos、reflections、vector docs/outbox、evidence feedback 等全部落 SQLite；SQLite 同时保存向量文档账本与 collection 同步状态，ChromaDB 只保存可重建的派生向量；语义向量索引 posts、公开 post 图片摘要、comments 和 chat messages；关键词查询只走 posts 的 FTS5 双表。

**反思器** — 后台 LLM agent。轻反思每条 post 后抽取结构化信号；深反思按可配置触发策略直接读取 raw evidence，对 `user.md` / `soul_memories` 做条目级修正，而不是只追加摘要。

### 4.2 设计原则

1. **会进 system prompt 的 → Markdown 文件**（`souls/*.md`、`soul_memories/*.md`、`user.md`）：使用 prefix cache 友好，用户能直接看到自己的 AI 记忆。
2. **会被查询/聚合/统计的 → SQLite**（posts、entities、emotions、todos…）：跨条聚合、关联查询、时间趋势必须靠数据库，不进 system prompt。
3. **向量检索独立成层**：SQLite 负责事实、版本与同步状态，ChromaDB 负责向量查询；若 collection 滞后或 outbox 失败，向量结果不会参与检索。
4. **反思器是 LLM agent 而非定时脚本**：参考 Hermes Agent，轻反思抽结构化信号，深反思做画像/相处记忆对账。
5. **数据主权**：所有核心数据默认本地；当前可直接复制 `workspace/` 备份，后续补一键导出。

## 5. 关键设计决策

| 决策 | 选择 | 主要理由 |
| --- | --- | --- |
| posts 存哪 | SQLite，不再保留 Markdown 文件 | 前端编辑路径成立，文件冗余无价值 |
| SOUL 存哪 | `souls/*.md` 文件库 + `souls` 表管理启用/排序 | 文件可分享、可模板化；DB 表负责状态切换不刷文件系统 |
| SOUL 记忆存哪 | `soul_memories/*.md` + `soul_memory_revisions` | 借鉴 peer-specific memory 思路，每个 SOUL 根据自己的风格形成不同理解 |
| SOUL 调用模型 | 默认启用多 SOUL 并发评论 | 入口层是社交媒体形态，多 AI 好友并列才有“群聊”质感 |
| SOUL 首评顺序 | 每个 post 固化 `post_soul_orders` | 并发回复完成顺序不影响展示；后续设置页排序变化不改旧 post 的阅读历史 |
| 私聊与主记忆 | 私聊独立存储，不写 posts，不进 FTS5；进入 ChromaDB 统一检索池但只允许当前 SOUL 检索；不直接进全局 `user.md` | 私聊不污染公开关键词检索和全局画像，但能在 SOUL 边界内塑造关系记忆 |
| 用户档案/画像 | 合并为 `user.md`，AI 反思器主动维护全文，用户可编辑，章节带 sensitivity | 一个文件心智更清爽；稳定画像保守写入，当前状态用 low 门槛积极增删 |
| 检索 | FTS5（双 tokenizer）+ ChromaDB + 动态权重 hybrid scoring | 中文必须 trigram；语义必须向量；融合分数保留单路强命中并奖励双路一致 |
| 回复上下文 | raw related posts，而不是中间层记忆摘要 | 先保持证据链干净，避免半成品中间层污染回复；后续重新设计更好的中间层 |
| 反思 | 直接从 raw evidence 对长期记忆做 reconcile | “反思”应修正已有画像/相处记忆，不只是抽取和堆叠 |
| 第三方 memory provider | 不接入 | TraceLog 的记忆是产品核心，不能把事实源和画像权交给第三方 |
| 数据主权 | 本地数据库 + 可复制 workspace，后续补一键导出/删除 | 当前数据默认本地，先保证可见可备份，再补产品化数据管理 |
| 用户记忆控制面 | Web 记忆工作台编辑 `user.md` / `soul_memories` 最终长期记忆 | 用户能修正 AI 最终记住的内容，不需要理解内部派生表 |

## 6. 后续方向

TraceLog 还能基于本架构生长出：

- **新中间层记忆**：重新设计更干净的 memory unit，要求具备去重、更新、删除、证据边界和老化机制。
- **Entity Resolution**：实体重复、实体冲突和关系消歧仍是独立后续阶段。
- **多 SOUL 圆桌**：在已并发评论基础上加二轮，让 2-3 个 SOUL 互相回应彼此评论，形成对话。
- **角色导入市场**：导入用户分享的 SOUL。
- **关系图谱可视化**：从 entities + relations 构造交互式图。
- **目标管理**：在 SQLite 加 `goals` 表，反思器追踪进展。
- **跨设备同步**：SQLite + souls/ + soul_memories/ + user.md 打包到 iCloud/Dropbox/Git 等。
