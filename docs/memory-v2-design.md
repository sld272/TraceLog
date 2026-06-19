# TraceLog 记忆架构 v2 设计构想

> 状态：北极星构想（feat/memory-v2 分支）。本文记录对记忆系统的一次"格局打开"式重构思路，作为 v2 的路线底稿。
>
> **实现状态**：核心三层（evidence ledger → unit → view）、按 bucket 的 reconcile 写路、边界、unit 向量检索、freshness seam、edit/delete challenge 重判**均已实现**——已实现部分的权威说明见 [memory-v2-architecture.md](./memory-v2-architecture.md)，与本文冲突时以该文件为准。本文中**尚未实现**的部分：每帖增量对账（trickle，§3.1）、第 3 档重组/decay（§3）、实体图升为 unit 骨架（§2.2）、读写闭环 retrieval_count 浮沉（§5）、图谱化矛盾边失效（§6 可选增强）。
>
> 相关：[memory-v2-architecture.md](./memory-v2-architecture.md)（已实现状态，权威）、[architecture.md](./architecture.md)（legacy 架构）、[database.md](./database.md)、[overview.md](./overview.md)。

---

## 0. 一句话主张

把记忆从"一篇被深反思反复重写的 Markdown 文档"，改造为"一组一等公民的 **memory unit**；Markdown 降级为 core unit 低频综合出的核心画像视图；可编辑的业务原文由 append-only evidence event 固化成可审计证据"。读写两条路都随之改变：写路从批量重写变成 evidence event 驱动的 unit 对账，读路从两极（整篇注入 md + 检索 raw）变成 **md → unit → evidence 的连续变焦**。

**核心洞察**：我们之前几轮纠结的"什么时候触发整理"是症状，不是病根。批量、攒阈值、挑空档跑——这些都是"文档重写模型"的伴生病。换掉记忆单元这个底层，触发问题不治而愈。

---

## 1. 为什么要推翻"文档重写"模型

当前链路：`raw evidence → 深反思 reconcile → 重写 user.md / soul_memories 的 Markdown 章节`。Markdown 可读、可注入，现有工作台还提供用户控制与 revision，这些优点要保留；但 v2 会把"用户控制"迁到 unit，而不是继续让用户直接改生成视图。当前模型仍强加了三个结构性代价：

1. **记忆没有稳定身份。** 一条"关于用户的信念"没有 id、confidence、显式证据链、时间有效期。现靠 `<!-- id: ... -->` 注释锚点做 patch，是 workaround 而非数据模型。无法查询"我们关于 X 相信什么、凭什么、何时起"。
2. **reconcile 是整段 LLM 重写。** 把"信念是否改变"与"措辞如何"耦合在一起，既贵又非确定，且每次需把整段喂入——这正是它必须批量、必须攒阈值、必须挑无人回复时跑的根因。
3. **只增不忘。** "当前状态与关注"本意快进快删，但无衰减机制，过时内容赖到下次深反思碰巧覆盖。

还有一道迟早撞上的墙——**scaling cliff**：`user.md` 是整篇注入而非检索的，它持续长大，终将超出可注入预算，且没有"对记忆本身做检索"的计划。

---

## 2. memory unit：被推翻后的新真相

### 2.1 主客易位：证据真相、信念真相与视图分层

> **证据真相** = append-only、带 revision/content hash 的 evidence event；**信念真相** = 一组一等公民的 memory unit；`user.md` / `soul_memories/<name>.md` 则只是这些 unit 低频综合出来的核心画像视图（synthesis，非机械渲染）。

这里必须纠正一句容易误导实现的话：`posts` / `comments` / `chat_messages` 这些业务行并非不可变——聊天可编辑，回复可 rerun。不可变的不是"当前行"，而是**每次 create/edit/rerun/delete 产生的证据版本事件**。unit 的证据链指向事件版本，而不是只指向会变内容的裸 `source_id`。

unit 的字段草案：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定身份 |
| `owner_scope` | 记忆归谁管理：`global` 或 `soul:<name>` |
| `visibility_scope` | 允许在哪个边界使用：`public` / `thread:<post_id>` / `private:soul:<name>` |
| `source_channel` | 首次/主要来源：post / comment / chat / user；完整来源仍以 evidence event 为准 |
| `prompt_policy` | `allow` / `no_prompt`；与"是否进入画像"正交 |
| `type` | fact / preference / goal / state / relationship / insight 等 |
| `content` | 跨证据的抽象陈述（**不是**某条 raw 的转写） |
| `confidence` | 置信度，随确认/反驳调整 |
| `sensitivity` | high / normal / low（沿用现 user.md 章节分级语义） |
| `evidence_event_ids` | 支撑它的不可变 evidence event 版本列表 |
| `status` | active / superseded / retracted / dormant |
| `superseded_by` | 被哪个 unit 取代（矛盾/时效） |
| `first_seen` / `last_confirmed` | 时间戳，用于衰减与有效期 |
| `retrieval_count` | 被读取/采用次数，用于浮沉 |

**边界不变量（MVP 即执行，不延后）**：

1. `owner_scope` 回答"记忆归谁"，`visibility_scope` 回答"能在哪出现"，两者不能再压进一个 `scope`。
2. reconcile / confirm / revise / retract / merge / supersede 只能发生在同一 `(owner_scope, visibility_scope)` bucket 内。
3. 公开 post 默认落 `global + public`；评论默认落当前 `thread:<post_id>`；私聊默认落 `soul:<name> + private:soul:<name>`。
4. thread unit 若要成为 public unit，必须经显式 promote op 新建 public unit并保留 provenance；private unit 永不自动晋升为 thread/public。
5. `profile_policy=force_exclude` 只表示"不进画像"；`prompt_policy=no_prompt` 才表示"任何回复 prompt 都不得使用"。

### 2.2 不会重蹈 observation 覆辙的那道坎

砍掉 observation 的原因是它**逐帖复述单 post**，与 raw 检索重叠——它是复制，不是抽象。架构文档里那句"需要重新设计为真正**可更新、可删除、可去重、可审计**的 memory unit"正是验收标准。

unit 必须过的坎：**是跨证据的抽象，不是单条证据的转写。**

- "用户在准备考研、焦虑但坚持" → 抽象，应成 unit。
- "6/3 那条帖说他在背单词" → 复述，永远留在 raw 层。

**复用已有投资**：轻反思已抽出 entities / emotions / events / relations 四张表，现"解耦、留作可视化基建"基本是死重。但 `entities.mention_count`、`relations.strength` 本就是跨帖累积的抽象，不是复述。把这套结构从死表提升为 **unit 的骨架**（实体图 + 情节事件做支架，语义信念 unit 挂其上），既清死重，又天然满足"genuine abstraction"。

> **已实现现状（见 [memory-v2-architecture.md](./memory-v2-architecture.md)）**：轻反思**禁用**；写端用批量 reconcile 直接从 raw 抽 unit（不做每帖 proposer——单帖无跨证据视野 = observation 2.0，见 §3.1）。本节「实体图升为 unit 骨架」尚未实现，是 v2 增强，届时需重启轻反思或由每帖增量对账（§3.1）补吐实体/关系。

### 2.3 unit 模型解锁了什么

- **触发问题消失。** reconcile 从"批量重写"变成"事件对账"：每批新 evidence event → 几个候选 op（confirm / revise / retract / add）。event id 是单调 cursor，create/edit/rerun/delete 都能进入同一消费链，不再靠业务时间戳猜"上次处理到哪"。它就是后台车道里持续滴流的小操作——社媒大量 idle 时间正好喂这种轻整理（业界称 *sleep-time compute*：用空闲算力预先整理记忆，让在线回复更便宜）。v1 的"双车道 + 在途闸门"隔离设计，正是它的天然底座。
- **遗忘 = 一个字段 + 一个后台扫描。** `decay = f(last_confirmed, importance, retrieval_count)`，低于阈值转 dormant，综合出的画像里自然消失，但可审计可恢复。
- **矛盾与时间有效期成一等公民。** 新证据与旧 unit 冲突时，不是覆盖文本，而是把旧 unit 标 `superseded` 并记 `superseded_by`——即知识图谱里的 bi-temporal / 边失效路线（Zep / Graphiti）。"当时真、现在假"终于可表达。
- **工作台几乎免费。** "查看整理记录 + 每次改了什么" = `SELECT ... ORDER BY changed_at`，diff = unit 状态机转移，不必从两份 Markdown 快照反推。
- **检索覆盖记忆本身。** unit 进向量库，回复时检索相关 unit 而非整篇注入，scaling cliff 解除。

---

## 3. 写路：把"整理"拆成三个时间尺度

当前是 fast（轻反思）+ slow（深反思）两档。v2 补上第三档"重组/压缩/遗忘"（睡眠隐喻里干的事）：

1. **捕获（在线，毫秒）**：业务行 create/edit/rerun/delete 与 `memory_ingest_events` 在同一事务内写入；事件固化 `source_revision + content_snapshot + content_hash + visibility_scope`。（轻反思在 MVP 已禁用——其结构化信号无消费者；v2 若复活，应作为 unit 骨架的实体/关系数据源而非独立 signal 层。）
2. **对账（后台滴流）**：按 `(owner_scope, visibility_scope)` bucket 消费新 evidence event → unit ops。替代现批量深反思。高频。**这一档的落地形态见 §3.1。**
3. **重组（低频，深夜 / 长 idle）**：细碎 unit 升一层抽象（层次化摘要，RAPTOR 式树）、合并重复、执行遗忘、重建检索索引、把常用 unit 浮上 md 核心画像。这是真正该周期跑的"大整理"，与"高频做第 2 档"不冲突。

这也对齐 Generative Agents（Stanford）的做法：memory stream + 周期 reflection 生成更高层 insight 节点并链回证据，本质就是第 3 档。

写路不变量（v1 已满足，需守住）：WAL、"重算在前、写锁在后、毫秒级写窗口"。业务内容变更与 evidence event 必须同事务提交；LLM 重算在事务外，unit ops + bucket cursor 在同一个短事务内提交；Markdown 物化视图仍用 `os.replace` 原子落盘。

### 3.1 第二档的落地形态：每帖增量对账（trickle reconcile）

> 这是当前实现**刻意延后**的设计（**尚未实现**），在此存档为 v2 第三步的目标形态。当前写端按 visibility bucket 批量消费 evidence events（每条事件不立即产 unit），新鲜度由读路的有预算 event seam 兜住。

**为什么不能"每帖单独抽 unit"**（核心陷阱，必须钉死）：per-post 一次 LLM 若只看这一条 post，**by construction 没有跨证据视野，只能转写**——这就是被砍掉的 observation 2.0。给它套一层"候选/可丢弃（proposed）+ 深反思再 promote/drop"也救不了：壳只降低了垃圾**进 active 的风险**，没降低**产垃圾的成本**；drop 率高，LLM 花在产废、深反思还要趟废堆。

**正解是把"每帖"做成"每帖一次小对账"，而非"每帖一次抽取"**——调用时带一个检索窗口：

```
触发：新 evidence event 落库（在线，后台车道）
输入：当前 event + 同 visibility bucket 的最近相关 evidence(向量 top-k)
      + 同 bucket 的相关 active unit(向量 top-k)
任务：针对已有记忆做增量提案——
      · confirm 旧 unit（本帖再次印证）
      · add 新 unit（仅当跨这条 + 召回证据构成真抽象）
      · flag 矛盾（本帖与某 active unit 冲突 → 标记待 supersede）
落库：直接产 active unit ops（带 evidence_event_ids），无 proposed 中间态
```

关键差别：它一上来就有跨证据视野（召回的 evidence + active unit），且召回先经过 visibility hard filter，产出的是**真·跨证据信号**而非单帖转写——drop 率与"先量产后筛"的浪费都没了。这正是业界 *sleep-time compute*（空闲算力预先整理记忆）的形态，也是 v1"双车道 + 在途闸门"隔离设计的天然用武之地。

**它依赖什么（也是 MVP 不上的原因）**：
- **unit 向量检索必须就位**——"召回相关 active unit"是它的前提。MVP 虽已为**读路**上了 unit 检索，但把它接进**写路的每帖触发**会显著抬高在线 LLM 频次。
- **在线 LLM 频次控制**：min_interval / 单飞 / 背景串行 / 开关（沿用 v1 隔离方案，§7）。
- **冷启动**：库空时召回为空，退化成"看单帖凭空抽"——需用阈值（active unit 数 < N 时不触发每帖对账，仍靠批量深反思）兜过早期。

**与批量深反思的关系**：两者不互斥。每帖对账做高频增量；批量深反思（或第 3 档重组）做周期性的全局一致化（跨更大窗口的合并、矛盾消解、遗忘）。MVP 只有后者；v2 第三步加前者。

---

## 4. 读路：md → unit → evidence 的连续变焦

破直觉：**md / unit / evidence 不是三个并列仓库，而是同一件东西的三个分辨率。** evidence event 是带版本的原始像素，unit 是识别出的物体，md 是图说。读取不是"选哪个"，而是从粗到细逐级展开，外加一道时间接缝。

### 4.1 三层各自的读时角色

- **md 核心画像 —— 常驻基线，无需 query，全量注入。** core memory unit 低频**综合（synthesis，非机械渲染）**出的**有界身份画像块**；它整篇注入 prompt——有界性由 core 子集准入控制，故全量可负担；非 core 单元不进 md，留在 unit 层靠下文检索。回答"我在跟谁说话 / 这个 SOUL 与 TA 的关系"——与当前话题无关也该在场的自我。prose 形态，因为身份要靠连贯叙述被吸收。对应 MemGPT/Letta 的 core memory 块、Generative Agents 周期综合的 self-summary，是检索照不到的身份地板；用户编辑落在 unit 层，不直接改这篇产物。
- **unit —— 话题召回，按 query 检索。** 当前 post 讲 X，就检索与 X 相关的 unit（哪怕重要度不够、进不了 md 基线）。精确召回此刻相关的具体信念/事实/目标，每个带 confidence、"自何时"、evidence_event_ids。结构化卡片形态，要的是精度与出处。这正是当年 observation 想当没当成的"可检索中间层"。
- **evidence event —— 落地验证，按需下钻。** 三个角色缺一不可：(a) unit 太粗时下钻取当时版本的"用户原话/细节"；(b) 引用接地，回复有据可查不编（现 evidence 面板即此）；(c) **最新、尚未对账成 unit 的事件**。逐字快照形态，可引用；如果源内容后来被编辑，也不会悄悄改写旧 unit 的历史依据。

一句话：**md 给框架，unit 给精度，evidence event 给可回放的证据。**

### 4.2 读取流水线：先做边界路由，再检索 unit → 顺藤摸 evidence → 补接缝

当前 `build_public_post_reply_context` 直接 `hybrid_search` 打 raw 池取 top-k。v2 改为有序展开：

1. **边界路由（硬门）**：先根据当前 channel 计算允许的 visibility buckets，并过滤 `prompt_policy='no_prompt'`；这一步在向量召回和 LLM 之前完成，不能靠 prompt 提醒模型自律。
2. **常驻**：注入当前边界允许的综合 md 核心画像。
3. **检索 unit**：用改写后 query 在独立的 unit collection（如 `memory_units_v1`）召回 top 相关 unit；raw collection 与 unit collection 分开检索、分别健康检查，不做同池排序。
4. **顺藤摸瓜**：命中 unit 已挂 evidence event ids，按需 hydrate 当时版本的内容快照。unit↔evidence 去重从此是一次 join，不再靠猜。
5. **并行打 raw/evidence 池兜底**：对尚无 unit 覆盖的话题，仍走现有 raw hybrid search。
6. **预算内组装**：unit 优先承担抽象，evidence 只补细节/出处；两路结果在 context assembler 中合并，不要求把异质分数强行归一。

读取上下文不能继续只有一份 shared context。目标拓扑：

- **GlobalContext**：公开、所有 SOUL 都可见，只构建一次。
- **SoulContext**：按 SOUL 单独构建，承载 `owner_scope=soul:<name>` 且当前 visibility 允许的 unit。
- **ThreadContext**：当前 `thread:<post_id>` 内公开共享，所有参与该评论线程的 SOUL 可见。
- **PrivateContext**：仅当前 `private:soul:<name>`，绝不进入公共 post 或评论上下文。

公共 fanout 使用 `GlobalContext + 每个 SOUL 自己的 SoulContext`；评论使用 `GlobalContext + ThreadContext + 当前 SoulContext`；私聊使用 `GlobalContext + 当前 SoulContext + PrivateContext`。现有 `soul_memories/<name>.md` 在 v2 中按**私聊画像**处理，不再被公共评论直接注入。

### 4.3 时间接缝（freshness seam）—— 最易被忽略的一环

对账异步，哪怕持续滴流，**永远存在一段"已发生但未消化成 unit"的 evidence events**。若读路只读 unit + 相关历史内容，模型会对"刚刚发生"瞎。

因此读路必须显式带上当前 visibility bucket 中、`memory_reconcile_cursors.last_event_id` 之后的近期 evidence events。旧的 `soul_thread_deep_cursor` / `reflections.scope_end` 不再承担 v2 cursor：它们无法覆盖 edit/rerun/delete，也可能在时间戳并列与 LIMIT 截断时漏数据。

接缝不是无上限全塞。每个 prompt 使用三重硬预算：`max_events`、`max_age`、`max_tokens`；优先级为当前线程事件 ＞ 最新用户事件 ＞ 与 active unit 潜在冲突的事件 ＞ 其他新事件。超预算时置 `freshness_truncated=true`、记录 backlog 数量并排队 catch-up reconcile。**cursor 已消费**只表示对账看过，不等于某条 evidence 已被 unit 覆盖。

完整图景：**md（陈年沉淀，已成身份）→ unit（中期信念，已抽象）→ 接缝 evidence（新鲜未消化）**，是一条从老到新、从抽象到具体的连续光谱。

### 4.4 in-prompt 冲突的 precedence 规则

md / unit 都派生自 evidence events，故：

- **讲事实、给细节、要引用 → 信最新 evidence event**，unit 不作事实出处。
- **讲框架、讲倾向 → 用 unit / md**，unit 带 confidence，低置信软着说。
- **近期 event 与旧 unit 矛盾 → 以近期 event 为准**，并视为"该 unit 即将被对账更新"的信号，回复时弱化旧 unit。

即把写路 reconcile 的精神在读路也贯彻一次：event 是证据版本，unit 是信念，最新证据压过陈旧信念。此规则须写进回复 prompt 组装。

### 4.5 模型最终看到的组装

```
[SOUL 人格]                      ← 不变
[基线认知]  ← md 核心画像(综合), prose ← 常驻全量注入，无 query
[相关记忆]  ← 检索到的 unit 卡片  ← 带 confidence / 自何时 / evidence 链
   · 每张可选附 1 条 evidence event snapshot 作支撑
[最近动态]  ← 接缝 evidence，逐字 ← 当前 visibility bucket 中未消费的近期事件
[本轮输入]  ← 当前 post / 消息
[读取规则]  ← 事实信最新 event，框架用 unit，冲突以新为准
```

读完一条回复，天然知道它用了哪些 unit、哪些 evidence event——**读时出处成一等公民**，evidence 面板从"只能标业务行"升级为"能标到具体 unit 与证据版本"，回复审计与工作台打通。

---

## 5. 读写闭环

读时记录"哪些 unit 被检索 / 被采用"，回灌为 unit 的 `retrieval_count` 与 decay 信号：常被用到的 unit 浮上 md 核心画像，无人理的沉为 dormant。第 3 档重组据此重排 md 核心画像。于是记忆不是静态文档，而是随使用自我浮沉的活体。

---

## 6. 迁移路径（不 big-bang）

现有 reconcile 能跑，且有比赛 deadline，故不推倒重来。下**一个最高杠杆的赌注**，其余按需叠加：

1. **第一步（核心赌注，已实现）**：先落 evidence event ledger、`owner_scope + visibility_scope + prompt_policy` 硬边界与 per-bucket cursor；再引入 memory unit/ops/view；深反思改为按 bucket 输出 unit ops；`user.md` / 私聊 `soul_memories` 改由允许进入画像的 core unit 低频综合；最后把 unit 向量检索与有预算的 freshness seam 受控接入读路。已实现状态详见 [memory-v2-architecture.md](./memory-v2-architecture.md)。
2. **第二步**：unit 检索多信号打分、precedence 权重调优、provenance 精确归因，以及 thread → public 的人工 promote UX。
3. **第三步**：decay + 第 3 档重组 + **每帖增量对账（§3.1）**。
4. **可选增强**：图谱化（实体支架、矛盾边失效），不阻塞主线。

**兼容性**：Markdown 视图与 revision 保留；用户控制从"全文覆盖 md"细化为"在工作台直接编辑 unit"，md 退为只读产物。CLI 退出整理保留（天然停顿点）。

---

## 7. 已知张力与待定决策

- **md 核心画像的选取准则**：重要度 × 置信 × 衰减 × 检索频次的具体打分；画像预算多大。
- **unit 与 raw/evidence 的组装策略**：独立召回后，各自配额与最终 token 预算如何分配。
- **对账粒度**：每条 evidence 即对账，还是小批 micro-batch（成本/质量权衡）。
- **unit 拆分/合并**：何时把一个 unit 裂成两个、何时合并重复，交给第 3 档还是对账即时做。
- **冲突仲裁**：最新 evidence event 与 unit 矛盾时，对账如何决定 supersede vs 降 confidence。
- **成本**：滴流对账提高 LLM 调用频次，需 min_interval / 单飞 / 背景串行 / 开关控制（沿用 v1 隔离方案）。
- **进程内假设**：v1 的"回复在途计数"依赖 worker 与路由同进程；若 worker 拆独立进程需改 DB/文件锁。

---

## 8. 一句话收束

不要再纠结"什么时候触发批量整理"。把整理拆成 evidence event 驱动、按 visibility bucket 隔离的 unit 对账，把读取拆成 md→unit→evidence 的连续变焦，用有预算 event seam 兜新鲜度、用 precedence 规则兜一致性。scaling cliff、observation 的去重老问题、新鲜度盲区与跨场景串记忆，在 v2 的读写两路上一并解决。代价是把"换触发"升级为"换记忆模型"，工作量与风险高一档，故采用边界先行 + 增量迁移。
