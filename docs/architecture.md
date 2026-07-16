# TraceLog 架构

这份文档描述 TraceLog 的整体结构和记忆系统（memory-v2）的完整规格。读代码前先读这里；改记忆系统时以本文档为准。

## 总览

```text
Web 前端 / CLI
  -> API routes / CLI commands
  -> core/ 服务层
  -> SQLite（本地业务与记忆真相源、日程只读缓存）+ 向量 outbox
  -> Microsoft Graph（日程真相源）
  -> 后台 job worker
       -> embedding 索引
       -> SOUL 回复生成
       -> memory reconcile（记忆整理流水线）
  -> 15 分钟日程同步任务
```

除日程外，持久化以 SQLite 为准，ChromaDB 向量索引可随时由 SQLite 重建。日程以用户的 Exchange / Outlook 日历为准，SQLite 只保留 Graph 事件的读取缓存。后台工作走一条 SQLite job 队列，由 API 进程内的 worker 消费；日程轮询是独立的 API 进程内周期任务，不进入 job 队列。

**调度铁律：后台维护不挡用户。** 单 worker 下，认领 job 时交互类（回复、embedding）永远优先于 memory reconcile；reconcile 自己跑到一半发现有交互 job 在等，也会在桶间让路、提前收工，由续跑 job 无损接续。

---

# 日程与 Microsoft Graph

TraceLog 是 **Microsoft Graph 的客户端，不是日历同步引擎**。Exchange / Outlook 是日程的唯一真相源：读取侧通过 calendarView delta query 维护本地只读缓存，写入侧直接调用 Graph，成功后再写穿本地缓存。系统不做双向冲突合并；下一次 delta 同步总以远端结果覆盖本地状态。

## Graph 边界

- `core/graph/auth.py`：基于 MSAL public client 与设备码流完成 delegated authentication。Application client ID 存在 SQLite `meta` 的 `graph.client_id`；token / refresh token 只进入权限为 `0600` 的 `workspace/graph_token_cache.json`，不写日志。
- `core/graph/client.py`：薄 Graph REST 封装，负责 calendarView delta、事件增删改和账户信息。请求统一使用 `Asia/Shanghai` 时区偏好、15 秒超时；429 与 5xx 尊重 `Retry-After` 并重试一次。
- 这一层不承载本地领域状态或冲突策略；Graph 返回的事件由 `core/schedule_service.py` 规范化后进入缓存。

## 缓存、delta 与写穿

`core/schedule_service.py` 管理今天前 60 天到后 365 天的窗口。`schedule_events` 同时保存 UTC epoch 与上海本地时间，列表读取只查缓存；deltaLink、同步时间和窗口边界保存在 `meta`。窗口变化、强制同步或 Graph 返回 410 时执行全量重拉，远端删除 / 取消事件时同步清理目标链接。

创建、更新、删除日程都先写 Graph，成功后立即 upsert / 删除缓存，不等待下一轮轮询。未配置 client ID 或未登录时，状态和读取接口以 `configured / connected` 明确降级为空结果；需要写 Graph 的操作返回“尚未连接”。退出登录会清 token、delta 状态和事件缓存。

## 目标联动与回复上下文

`core/goal_schedule_service.py` 在本地维护目标↔日程链接和每周期望。周进度按 `Asia/Shanghai`、周一为周首统计本周已链接且未取消的事件，生成 `current / target`；期望存于 `goals.schedule_expectation`，链接本身不回写 Exchange。

`core/context_builder.py` 在公开帖回复的共享上下文中加入“近期日程”块：读取今天到未来 7 天，最多 10 条；已绑定目标的事件附目标标题、期望和本周进度。未连接或没有事件时不注入该块。

## 同步调度

API runtime 初始化时启动独立 asyncio 任务：启动后先尝试同步一次，随后每 15 分钟运行 `ScheduleService.sync()`；未登录时安全跳过，失败只记警告，不影响 API。`POST /schedule/sync` 提供手动同步，设备码登录成功后也会立即同步。FastAPI lifespan 退出时由 `shutdown_runtime()` 取消周期任务和仍在等待的设备码登录。

---

# 记忆系统 memory-v2

## 核心概念

- **evidence（证据）**：用户的每一次输入（发帖/评论/私聊/图片摘要）都以不可变事件的形式记账。编辑、删除也是新事件，旧版本永远保留。
- **unit（记忆单元）**：从证据中沉淀出的一条结构化信念，比如「用户在准备考研」。粒度标准：**一条可独立回问、可单独验证的事实**。
- **bucket（桶）**：记忆的隐私边界，由 `owner_scope`（global 或 soul:<名>）×`visibility_scope`（public / private:soul:<名>）决定。公开帖的事实进全局公开桶；私聊内容只属于那个 SOUL 的私密桶。
- **view（画像视图）**：从稳定、重要的 units 综合出的叙事缓存（全局用户画像、每个 SOUL 的关系记忆）。

## 写链：从一句话到一条记忆

业务写入和证据记账在**同一个事务**里完成，然后入队一个去重的 reconcile job。后台流程：

1. runner 找出 cursor 之后仍有新证据（或存在被 challenge 的 unit）的桶。
2. LLM 只看到当前用户的证据、当前 units 和墓碑，输出 add / retain / confirm / revise / retract 操作。
3. 系统校验每个操作引用的证据属于本批次、不跨桶，然后把 unit 变更、操作日志、cursor、run 记录**原子写入**。LLM 调用永远发生在写事务之外；LLM 失败则不推进 cursor，证据留到下次重试。

**粒度规则**：宏观综合是画像层的职责，不在 unit 层做。可被回问的具体事实（分数、日期、数字、决定、人名）即使一次性也要落成 unit（insight/freeform + episodic 层，importance 0.5）；已有宽泛主题 unit 时，新的具体事实 add 新 unit 而不是只 confirm 到主题上——confirm 堆积会让事实沉在证据里检索不到。

**用户编辑的特殊性**：用户编辑 unit 后，旧证据进入 `review_pending`，由独立的 relink judge 重判哪些证据还支撑新内容。用户亲手创建的 unit 以用户断言本身成立，不因原始内容被删而消失。

## 打分与画像准入

打分不是自由浮点：reconcile prompt 给五个带例子的锚定档位（0.3 / 0.5 / 0.7 / 0.85 / 0.95），代码侧再吸附一次——离散档位的跨模型一致性远好于小数。

进入画像（`tier=core`）是多路的：

- 用户亲述（user_authored / force_include）直通；
- 推断类信念靠 confidence 过 ENTER/EXIT 滞回；
- 或者靠**行为信号**（confirm 次数、独立证据数、存活时长——纯客观计数，换模型不漂）攒够分后走更低的 CONFIRMED_ENTER 水位。

行为信号只加分、永不做硬门槛（教训：曾有个 op-count 门永久挡住了一次性明确自述的身份，已移除）。importance 保持 0.70 硬地板——画像始终是"重要事实"的底座。`portrait_policy` 可强制纳入/排除；`prompt_policy=no_prompt` 把 unit 从所有回复 prompt 中禁用。

检索的语义门按查询自适应：在相似度序列的最大落差处截断（头部簇整体偏低也能进），无明显落差回退保守地板，0.20 兜底；有 FTS 关键词佐证的命中走宽门计分。换 embedding 模型时跑一次 `scripts/calibrate_embedding_gate.py` 重新校准阈值。

## 读链：回复时注入什么

- **基线画像**：global/public 的 `user_portrait`
- **当前状态**：时效窗口内的 state units
- **相关记忆**：scope 过滤后的三路召回——unit 内容 FTS、unit 语义 ANN、**原始证据语义 ANN 反推归属 unit**（unit 内容是有损摘要，"期末考了 92 分"这种具体事实常只活在证据文本里）。有归属的证据只作检索键，注入仍走 unit 层，scope / 折叠 / 墓碑 / 撤回全部照常生效；证据分与 unit 语义分取 max，不重复计分。
- **未沉淀原文**：从未沉淀成任何 unit 的孤儿证据命中后，原文直接注入 `[未整理成记忆的相关原文]`（相似度降序，条数 + 字符预算双限，同源与最新动态去重）。边界：被撤回 unit 的证据仍有关联行、relink 重判中的证据沉淀过一次，都**不算孤儿**——撤回语义不被绕过。
- **对话回想**：命中 unit 的原始证据周边原文；证据通道命中的 unit 用真正匹配查询的那条证据做锚点。
- **最新动态**：cursor 之后尚未整理的近期证据（当前消息本身除外）。
- **SOUL 关系**：当前 SOUL 的 relationship units 聚合。

**时间感**：注入的每条记忆都带代码计算的相对时间标（"3 天前"），模型只解读标签、不做日期运算。写侧对应铁律：unit 内容禁止"今晚 / 最近"等相对时间词，必须落成绝对表述，防止多天后读到时失真。

**隐私**：公开内容跨 SOUL 可用；私聊只有当前 SOUL 可读，在公开场景使用自己的私密信息时附带谨慎披露规则。

## 维护流水线与调度

所有后台整理走同一个 `run_memory_reconcile` job（写入方只入队去重票），链内顺序：

1. **reconcile 对账**——证据 → unit 增删改
2. **relink 证据重挂**——用户编辑 unit 后重判证据支撑（evidence↔unit）
3. **reflect 确定性反思**——无 LLM：state 衰减 + contextual→core 晋升。本轮动过的 owner 即时跑，另有每日一次全量扫兜底（meta 门控）——不活跃 owner 的过期 state 也要能淡出
4. **墓碑断言回填**——撤回内容规范化成 normalized_claim
5. **consolidate 巩固**——LLM 对单个 owner 全量去重去矛盾
6. **crosslink 跨桶链接**——unit↔unit 关系判定
7. job 尾部刷新画像视图、同步向量索引

命名约定：reflect 与 consolidate 是两个独立 pass（旧文档的"深反思 P2a/P2b"合称废止）；consolidate 的操作 actor 记 `consolidation`；**crosslink 管 unit↔unit 跨桶关系，relink 管 evidence↔unit 重挂**，两者无关。

**不挡交互的三道保护**：job 认领交互优先；桶间让路（`should_yield` 每桶之间轮询，有交互 job 等待就提前收工、跳过尾部维护，续跑 job 在队列排空后接续——桶游标保证不丢证据）；consolidate 开跑前单独再让路一次。

**consolidate 三重闸**（它是最贵的 pass：owner 全量 active units 进一个 prompt，判错用户可见）：active units ≥ 12、距该 owner 上次巩固 ≥ 3 天、自上次巩固后有 unit 变更；每次 job 至多巩固 1 个最欠账 owner（从未巩固者优先）。冷却戳只在成功后写入，失败的 owner 留着重试。

`reconcile_bucket` 拆分维持冻结。回复延迟先测后调：chat 链路逐段计时，以 `reply_latency` 事件落日志。

## 跨桶链接 crosslink（链接，不合并）

桶是隐私底座，consolidate/supersede 拒绝跨桶——但同一事实或矛盾常落在两个桶里（公开帖说"在考研"，私聊也说了）。低频 crosslink pass 搭 reconcile 尾部的车：最近变动的 units → 跨桶候选（同文精确匹配 + 向量近邻粗筛）→ 一次批量 LLM 判定 → `memory_unit_links`。喂给 LLM 的桶标签只有"公开/私聊"，人格名不出现。只记录关系，永不搬内容；一对只保留一个关系，后判替换先判。

- `same_fact`：读时折叠——两端同时命中时只注入更私密/更新的一条。
- `contradicts`：更公开的一侧打 `contested_at` 标。标是**无因**的：该 unit 退出画像、检索降权 50%、注入行加「不太确定」并配规则 "不要解释它为什么不确定"——跨桶原因只允许出现在私聊回访里。同桶新证据（confirm/revise）自动清标。
- `context_variant`：双面共存，不折叠不打标——人在公开和私下本来就可以说不一样的话。

## 回访（矛盾的唯一出口）

contested 标只能靠用户亲口给的新证据化解。私聊组装回复时，若本轮检索已自然触及矛盾话题（链接任一端在本轮 used units 里），且持有私密端的正是这个 SOUL，允许搭载一条 [回访] 指令，走阶梯：

- 第一级：只自然问近况，明确禁止提及任何记忆差异——新鲜回答经 reconcile 正常回流，矛盾多半不点破就消解。
- 第二级（上次问过仍不明朗才用）：温和核对现状，给四个轻回答方向，铁律禁止"你之前说过…但…"式对质。

克制约束：仅私聊、仅话题相关、每条回复至多一条、同一条记忆 3 天内至多问一次、`POST /memory/revisit-policy` 一键关闭。

闭环由 crosslink 的链接维护完成：任一端 last_confirmed 越过链接时间（回访回答被吸收）或一端死亡时重判该链接——矛盾消解则降级关系并清标；仍矛盾的会诚实地重新打标。

## 忘记、找回与墓碑

源内容编辑/删除会 challenge 所有引用该版本的 units，下一轮 reconcile 必须为每个 challenged unit 给出唯一决定。

用户侧的撤回叫「忘记」，两种原因语义不同：

- `outdated`（默认）：曾经成立、现在过时，允许新证据重建同一信念。
- `false`：从来不成立，墓碑永久阻止再生。

UI 默认 outdated，因为误标代价不对称——误标过时可逆，误标 false 是静默且永久的封杀。「忘记 → 找回」是原子操作：`restore_unit` 只对 retracted_by_user 开放，状态翻回 active 的同一事务里压制解除、画像重算。模型自己的撤回不可找回——证据仍支持时 reconciler 会自己重建。

**墓碑压制是双保险**：撤回后由轻量 LLM 把撤回内容规范化成 `normalized_claim`（主语统一、去修辞、绝对时间、保留否定词），喂 prompt 时以断言替代原文（换措辞也压得住）；false 墓碑的 claim 同时进向量索引，add 落库前做相似度兜底拦截（≥0.86 阻断，向量不可用时 fail-open）。outdated 墓碑不建向量，且在同桶出现同断言的 active unit 后自动退场；false 墓碑不过期。

**压制范围**：用户撤回升为全局压制——用户的意思是"别再记这件事"，不是"这个桶别记"；跨桶只喂 normalized_claim，私密原文永不进其他桶的 prompt。模型撤回保持桶内。

## 用户可见状态

对外状态收敛为 5 个：已记住 / 整理中（附原因"你最近改了相关内容，正在重新核对"）/ 收起的（不要提起）/ 已忘记·可找回 / 淡出的。superseded 和模型撤回是内部机制态，永不出卡片；动态流同样过滤它们，同一次整理的多条变更聚合成一条摘要，文案不出现内部术语。源内容编辑/ 删除前，前端用 `GET /memory/source-impact` 预告"这条内容还支撑着 N 条记忆"。

## 模块地图

- `core/memory_events_service.py`：证据账本与 cursor
- `core/memory_reconciler.py`：单桶原子对账
- `core/memory_reconcile_runner.py`：流水线调度（对账/relink/反思/ 墓碑回填/巩固/crosslink、让路逻辑）
- `core/memory_reconcile_producer.py`：LLM seam（prompt 组装与解析）
- `core/memory_unit_service.py`：unit、证据链接、操作日志
- `core/memory_reflection.py`：reflect（确定性）与 consolidate（LLM）及其调度闸门
- `core/memory_crosslink.py`：跨桶链接判定与维护
- `core/memory_revisit.py`：回访指令
- `core/memory_view_service.py` / `memory_view_producer.py`：用户画像
- `core/soul_relationship_memory.py`：SOUL 关系画像
- `core/memory_read.py`：scope 过滤的 prompt 读模型
- `core/llm/memory_router.py`：记忆相关的全部提示词路由
- `api/routes/memory.py`：记忆工作台 API
