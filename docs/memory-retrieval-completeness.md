# 三层记忆架构：检索完备性分析与待议清单

> 缘起：2026-06-24「回复检索统一到 memory-v2」之后（见 `docs/memory-v2-architecture.md`），
> 复盘当前三层记忆的检索功能在理论上是否「完备」。本文先给出判断骨架，再把发现的
> 五个问题登记为可深入讨论的条目（R1–R5），供后续逐个展开。结论先行：作为**信念
> 检索系统**接近完备，唯一算「按自身标准也不完备」的是 R1（state 老化死区），其余
> 要么是刻意边界，要么是 precision 而非 completeness 的取舍。

## 0. 分析骨架

把「完备」拆成三个可证伪的子问题，分别评估：

1. **召回完备性**：该被召回的东西是否都召得回（≈ recall）。→ 有真缺陷，见 R1–R5。
2. **划分完备性**：层与层之间是否不重不漏。→ **干净，接近完备**（见 §1）。
3. **soundness 完备性**：用到的东西，可见性对不对、来源标得对不对。→ 分两维：可见性 airtight，
   来源（provenance）曾有缺陷已修（见 §3）。

检索装配入口：`core/memory_read.py::build_memory_section`（line 603）。各层对应：

| 信息生命周期 | 常驻 always-on | 查询相关 query-driven |
|---|---|---|
| portrait 顶层信念 | `[基线认知]` ✓ | 随常驻带出 |
| state 单元 | `[当前状态]` ✓（≤7天, top5） | **被显式排除** |
| 其他信念单元(identity/preference/insight/goal/freeform) | — | `[相关记忆]` ✓（关键词+语义） |
| relationship 单元 | per-soul 叙事 ✓（整段注入） | 被排除（owner 限定） |
| 原始证据·近期(≤3天, 未 reconcile) | — | `[尚未稳定…]` freshness（仅关键词） |
| 原始证据·陈旧(>3天, 从未成 unit) | ✗ | ✗ → **R4** |

## 1. 划分完备性（结论：干净，可作为讨论的稳定前提）

- `in_portrait=0` 把画像成员排除出 `retrieve_units`；state/relationship 各有专属层并被
  `_RETRIEVE_EXCLUDED_TYPES`（`memory_read.py:105`）排除。→ **每个单元最多出现在一层**。
- 两条原始注入路径——`[相关对话原文]`（`_recall_conversations`, line 356）与 freshness
  （`freshness_seam`, line 456）——**按 reconcile 状态互斥**：成了 unit 的证据已过 cursor，
  不会再进 freshness；没成 unit 的进不了 recall。→ 无重复注入。

这一层不需要进一步讨论，作为后续讨论的稳定地基。

---

## 2. 待议清单（R1–R5）

### R1. state 老化死区 ✅ 已解决（2026-06-25，commit `5fe780c`）

- **机制**：`recent_state_block`（line 165）准入靠 `last_confirmed >= now-7天`；而
  `retrieve_units`（line 201）又把 `type='state'` 整个排除。state 是 ephemeral 的「当前
  状态」，7 天窗口本身没问题——问题只在于"7 天后怎么办"。
- **调查结论（钉死"真洞 vs 已兜住"）**：
  - 上游**有** `decay_dormant`（30天→`dormant`）+ `promote_core`，但 `memory_reflection`
    这套深反思**从未接线**（app/api/cli 无触发）。所以 7–30 天是设计内死区，>30 天本应退役
    却因没接线而**无限堆积**成 active-but-invisible。**是真洞。**
  - 但 **7–30 天的"重确认"链路是通的**：reconcile 候选集 `list_reconcile_units_in_bucket`
    取整桶 `active/challenged`、**无时间过滤**，旧 state 一定被丢给 producer，confirm/revise
    刷新 `last_confirmed` 续命（`confirm_unit`, `memory_unit_service.py:274`）。被复述的 state
    永远活着——死洞只困住"仍为真但不再被提"的 state。
- **决策与实现（用户拍板 b + 搭车）**：
  - `decay_dormant` 收窄到 `AND type='state'`：**只有 state 因年龄退役**；持久信念
    （preference/identity/insight/…）永不因年龄消失，只按相关性召回，或被 promote / 显式 retract。
  - `run_pending_reconcile` 搭车触发 `reflect_persona`（每个本轮 reconcile 的 owner 各一次，
    `not dry_run` 守卫、best-effort 不拖垮 job）。**连带**把 promote（contextual→core 沉淀）
    也一起打开了——这是 (b) 里"持久信念只会沉淀、不会遗忘"的正向机制。
- **残留**：dormant 不进 reconcile 候选集，故 state 退役后再被提到会**新建一条**而非复活旧的；
  对 state 无害（本就是"现在又成立了"）。
- **未采纳**：曾考虑的 (a)「让老化 state 进 query 召回」未做——既然 state 7 天窗口合理、且
  decay 已补上退役，旧 state 不需要再进查询路径污染"当前"语义。

### R2. 单轮 / thin / 指代型 query 召回弱 ✅ 已解决（2026-06-25，commit `cfe52a4`+`126f9a6`）

- **机制（原）**：`query` 就是用户最新一句话，关键词侧还是 bigram 子串匹配；指代/省略型提问
  （"那件事后来怎样了"）overlap≈0、语义查空泛代词也弱，`[相关记忆]` 哑火。
- **做了什么（用户拍板：LLM rewrite + 每条都做 + 给 units 上 FTS5）**：
  - **Phase 1（`cfe52a4`）**：给 units 建 FTS5（`memory_units_fts` + trigram，镜像 posts），
    关键词侧从 bigram 换成真 FTS；trigram 不可 token 的 2 字中文走 LIKE 兜底
    （`_fts_unit_ranks` / `_units_matching_like`）。bigram 退出 unit 门（`_keyword_overlap`
    只留给 evidence 选句）。存量库一次性回填 `scripts/backfill_memory_units_fts.sql`。
  - **Phase 2（`126f9a6`）**：复活 `query_rewriter`，产出 `semantic_query`（喂向量）+
    `keywords`（喂 FTS5），并带最近 N 轮对话**消解指代**。四个回复点全接（comment/chat/
    首条 fanout 一次性/root rerun），每条都 rewrite，fail-open 回 raw query。
- **未重蹈 v1**：rewrite 只产**检索 query**，不参与注入；记忆仍是信念层，没退化成全文搜索。

### R3. 语义门无相似度下限（precision）✅ 已解决（2026-06-25，commit `9cface3`）

- **机制（原）**：语义准入是「进了 ANN top-k」即命中，无 distance 阈值 → 冷门 query 把弱相关
  unit 当命中注入（多召噪声）。
- **做了什么**：`_semantic_unit_sims` 读 Chroma 已返回的 **cosine distance**，只保留
  `相似度(1-distance) ≥ SEMANTIC_SIM_FLOOR(0.30)` 的命中，并改用真实相似度评分（近的信念
  在 FTS+语义混合里压过擦边的）；distance 缺失（罕见 Chroma 降级）fail-open 保留。
  **排在 rewrite 之后**，门面对的是已锚定的 query。
- **顺带**：CJK bigram 假命中问题随 Phase 1 把 unit 门换成 FTS（trigram 真分词）一并消除。
- **留待调参**：`SEMANTIC_SIM_FLOOR` 是模型相关常量，起步保守 0.30，需对真实距离分布再调。
- **2026-07 更新**：固定 0.30 地板已升级为按查询自适应门（`adaptive_sim_cutoff`：在相似度
  序列最大落差处截断，0.20 硬地板兜底，无明显落差回退 0.30；FTS 佐证走宽门计分）。换
  embedding 模型的调参改为跑 `scripts/calibrate_embedding_gate.py` 探针集。

### R4. D1：陈旧且从未成 unit 的原始内容不可达（有意边界）

- **机制**：units 走语义召回、freshness 只覆盖 ≤3 天且未 reconcile 的原始证据；二者之间，
  「几天前提过一次、没沉淀成 unit、又不近期」的原始内容召回不到。
- **定性**：**刻意为之**——体现「记忆=信念，不是全文搜索」的设定，上一轮已确认接受。
- **待议**：
  - [ ] 重新审视这个边界在「成长记忆」叙事下是否过严：用户偶尔会期待 AI 记得「我提过一嘴」的小事。
  - [ ] 若要补，正确底料是给 `memory_ingest_events` 建语义索引（带 scope+supersede），**而非**
        复用并行的 comment/chat `vector_docs`（键不对、且等于复活 v1 文档检索）。
  - [ ] 替代杠杆：调 reconcile 节奏，让有意义的原始内容更快沉淀成 unit（unit 本就同时具备
        语义召回 + scope + 去重）。

### R5. 各层预算硬上限

- **机制**：state top5（`STATE_BLOCK_LIMIT`）/ 相关 top8（`RETRIEVE_DEFAULT_K`）/ freshness
  6 条·400 字（`FRESHNESS_MAX_EVENTS` / `FRESHNESS_CHAR_BUDGET`）。
- **定性**：都是**按相关度排序后截断**，丢的是最不相关的——工程上可接受，不算完备性缺陷。
- **待议**：
  - [ ] 是否需要按 channel / 重要度动态调配预算（而非全局常量）。
  - [ ] relationship 叙事是**整段注入**（非检索），随关系增长可能膨胀——属 scalability 而非
        completeness，但与预算话题相邻，一并记下。

---

## 3. soundness 完备性——分两维，别当成铁板一块

soundness（「会不会用到不该用的东西」）有两个**正交**的维度，早期版本只数了第一个、把
整条说成「最稳」，是不准确的：

### 3a. 可见性访问控制——airtight，作为参照系

- scope policy 在 SQL 层、进 prompt 之前就过滤；ANN 排名故意**不带 scope**、之后再与
  scope 过滤后的候选求交集，所以语义检索**永远不可能放宽可见性**。
- 跨 soul 不泄漏、自己的私密在公开场合打 discretion 标。
- 结论：完备且严谨；R1–R5 的任何改动都不应破坏这条不变量。

### 3b. 来源正确性（provenance）——这一维**曾有缺陷，已修**

可见性对了，不等于模型**知道这话是在哪、对谁说的**。bucketing 重构把评论用户事实拍平进
`(…, public)` 后，attribution 仍按 `visibility_scope` 判，于是评论衍生内容被误标「（公开
帖子）」、丢了 soul 区域信息。**最严重处在 freshness**：soul A 评论区的原话合法地进了
soul B 的 freshness（可见性没错，bucket 是 global/public），却被标成公开帖子——B 可能把
一句对 A 说的话当成用户的公开广播，或当成对自己说的。这不是泄漏，是**误判来源**。

- 修复：attribution 改为按 `source_type` 判（评论再经 comments 表还原 soul），与 recall 修复
  同一原则；`FreshnessItem` 带上 `source_type`/`source_id`。2026-06-24，commit `02ae55f`。
- 归类：这是 soundness 的 provenance 维度，**不影响 R1–R5**（recall 候选集/排序/预算/scope
  全未变，attribution 在检索下游只改标签）。但它证明「soundness 最稳」需要拆维度看：可见性
  那半成立，来源那半此前有洞。

## 4. 总评

- 作为**信念检索系统**（portrait/state/units + 一段近期原始证据桥接）：**接近完备**——
  划分干净（§1）、可见性访问控制严谨（§3a）、常驻层给 thin query 兜底。
- 原本唯一「按自身标准也不完备」的 **R1（state 老化死区）已解决**（state-only decay + 搭车
  触发，commit `5fe780c`）；剩下的 R2 是已知弱项有地板兜底，R3 是 precision 调参，R4 是刻意
  边界，R5 是工程截断——均非"按自身标准不完备"的硬缺陷。
- 一句话：**对「记什么」完备，对「搜什么」有意不完备**——这是设计立场，不是疏漏。

> 进度：R1（state 老化）、R2（query rewrite + units FTS5）、R3（语义距离门）均已闭环。
> 剩 R4（D1 边界，刻意保留）与 R5（预算/relationship 注入，scalability）按需推进。
> 近期可做的实证项：观察 rewrite 对召回的真实影响；语义门已改自适应（见 R3 更新），换模型跑探针脚本即可。
