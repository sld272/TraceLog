# TraceLog 记忆 v2 — MVP 落地设计

> 状态：实现前定稿（feat/memory-v2 分支）。本文是 [memory-v2-design.md](./memory-v2-design.md) 第 6 节"第一步核心赌注"的可落地展开，把愿景里被一句话带过的细节钉成 schema、op 模型、迁移路径、验收门。
>
> 已纳入 [memory-v2-deep-research-report.md](./memory-v2-deep-research-report.md) 的评估结论：采纳"字段去过载、view 作为受管对象（hash 驱动重综合）、对账输入补 tombstones"等修订与一批默认数值。md 渲染按后续讨论定为 **LLM 综合为主、模板仅失败兜底**（orientation 定位，见 §3.4）。**写时机：批量深反思直接从 raw 抽取/对账（B），不做每帖 proposer**；**读路：本期受控改造**——md 全量注入之外加入 unit 检索 + 接缝 raw，带 feature-flag 可回退 md-only（§7）。轻反思禁用（无消费者）。**每帖增量对账（trickle reconcile）写入 [memory-v2-design.md](./memory-v2-design.md) 作为后续第三步**。其余完整 v2 设施（review 队列、隐私全分类、检索多信号打分等）按 §11 记账延后。凡仍有合理岔路的地方以 **分支** 列出，标注代价与推荐。
>
> 相关：[architecture.md](./architecture.md)、[database.md](./database.md)、[auto-reflection-design.md](./auto-reflection-design.md)。

---

## 0. MVP 的边界：换"写的表示"（unit）+ 受控改读路（加 unit 检索），写时机仍批量深反思

### 0.1 三层真相分工（先把名词钉死）

- **raw evidence —— 原始证据层。** post / chat_message / comment 原文，不可变，是一切记忆的最终出处。**它今天就是主记忆**：回复读路真正承重的是对 raw 池的 hybrid_search，不是 md。v2 之后 raw 仍是主记忆，units 只是给它补上抽象、出处与置信。
- **memory unit —— 结构化真相层 + 审计层。** 一等公民：带 id / 置信 / 证据链 / 状态 / 时间。深反思（对账）的落点；工作台增删改查的对象；**本期起回复读路检索的目标**（§7）。
- **user.md / soul_memories —— 由 core memory units 低频**综合（synthesis）**出的核心画像块。** **不是** unit 的机械渲染/投影，而是对 core 子集的一次有损、整合、有界的综合。它**全量注入**回复 LLM，给模型一个稳定的身份地板，也给用户一面直观的镜子。

一句话：**raw 给真相，unit 给精度与出处，md 给一个恒在的身份框。**

### 0.2 MVP 动哪几件事

整份 v2 愿景有三条腿：①记忆单元化（写的表示）②读路 md→unit→raw 变焦 ③衰减/重组/每帖增量对账（写的时机）。**MVP 做①和②，③延后**——写端用批量深反思直接从 raw 抽取（不做每帖 proposer），读端把 unit 检索拉进回复路。补上"光写不读 = 影子库"的浪费，让 units 真正有消费者。

- **写：深反思直接抽取（B）。** 轻反思**禁用**（无功能消费者）。批量深反思读"自 cursor 起的 raw + 现存 active units + tombstones"，产 `add/confirm/revise/retract` 落 `memory_units`（而非重写 md）。抽象发生在唯一有跨证据视野的地方——深反思；不先量产单帖候选再筛（那是 observation 2.0）。
- **读：受控加 unit 检索（本期最大风险项）。** 回复路从"md 全量 + raw hybrid_search"扩成"md 全量(core) + 检索 unit(非 core) + 顺 evidence 摸 raw + 接缝 raw"（§7）。**因为它动了每条回复的热路径，必须带 feature-flag + md-only 回退**，并把"回复不回归"列为硬验收门（§8）。
- **md：core unit 低频综合（orientation）。** `user.md`/`soul_memories` 从"深反思手写"变成"core unit 综合产出"，全量注入不变（§3）。

一句话 MVP 验收：**深反思直接产 unit ops（不重写 md）；`user.md`/`soul_memories` 由 core unit 综合；回复路在 flag 后加入 unit 检索 + 接缝 raw 且不回归；工作台以 unit 为对象。**

明确**不在 MVP**：decay / 重组 / **每帖增量对账（trickle，第三步，写进愿景文档）**；`retrieve_units` 的多信号打分（MVP 用最简语义召回，打分留 v2.1）；图谱化矛盾边失效、challenge 流程、隐私删除 UX（见 §11）。**轻反思禁用**（代码保留、不挂管线，详见 §2.1）。

---

## 1. 数据模型

### 1.1 `memory_units`（结构化真相所在）

对齐现有约定：id 用字符串主键（同 `posts.id`），时间戳用 `REAL`（同全库），可变结构塞 `metadata TEXT`（同 `entities`/`events`）。**本轮按研究报告做了一次"最小但必要"的字段去过载**：把"改动门禁 / 重要度 / 结构身份 / 用户意图 / selector 结果"拆成各自独立的列，不再让一个字段身兼数职。

```sql
CREATE TABLE IF NOT EXISTS memory_units (
    id               TEXT PRIMARY KEY,            -- mu_<ulid>
    scope            TEXT NOT NULL,              -- 'global' | 'soul:<name>'
    type             TEXT NOT NULL,              -- identity/preference/goal/state/relationship/insight/freeform
    content          TEXT NOT NULL,             -- 跨证据抽象陈述，非单条 raw 转写
    confidence       REAL NOT NULL DEFAULT 0.6,  -- 信念强度（随确认/反驳调整）

    source           TEXT NOT NULL DEFAULT 'reflected'
                       CHECK(source IN ('reflected','user_authored','migrated')),
    status           TEXT NOT NULL DEFAULT 'active'
                       CHECK(status IN ('active','pending','dormant',
                                        'retracted_by_model','retracted_by_user',
                                        'superseded','challenged')),
    retraction_reason TEXT
                       CHECK(retraction_reason IS NULL OR
                             retraction_reason IN ('false','outdated')), -- 仅 retracted_by_* 时填

    tier             TEXT NOT NULL DEFAULT 'contextual'
                       CHECK(tier IN ('core','contextual','episodic')),   -- 结构身份：够不够格进画像
    profile_policy   TEXT NOT NULL DEFAULT 'auto'
                       CHECK(profile_policy IN ('auto','force_include','force_exclude')), -- 用户意图
    importance       REAL NOT NULL DEFAULT 0.5,  -- 画像价值（与 confidence 正交）
    sensitivity      TEXT NOT NULL DEFAULT 'normal'
                       CHECK(sensitivity IN ('high','normal','low')),     -- 仅表示 reconcile 改动门禁

    in_md_slice      INTEGER NOT NULL DEFAULT 0, -- selector 计算结果缓存（非意图/非预算/非隐私）
    normalized_claim TEXT,                        -- 预留：tombstone 查重/冲突辅助，MVP 可空
    superseded_by    TEXT REFERENCES memory_units(id) ON DELETE SET NULL,

    first_seen       REAL NOT NULL,
    last_confirmed   REAL NOT NULL,
    retrieval_count  INTEGER NOT NULL DEFAULT 0, -- 预留给第二/三步，MVP 不写
    metadata         TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_scope_status ON memory_units(scope, status);
CREATE INDEX IF NOT EXISTS idx_units_slice ON memory_units(scope, in_md_slice, status);
```

字段取舍说明：

- 去过载的四条对应判断：**`tier`** 决定结构身份（core 画像 / 普通 contextual / 一次性 episodic），比手改 `in_md_slice` 合适；**`profile_policy`** 是用户"强制进/强制出画像"的意图；**`importance`** 是画像价值打分，与 `confidence`（信念强度）正交——低置信也可能高价值，反之亦然；**`sensitivity`** 收窄为**仅**表示 reconcile 改动门禁（沿用 `profile_service` 现有 high/normal/low 语义），不再兼任画像优先级。
- **`in_md_slice` 降级为纯 selector 计算缓存**：它只是 §3.2 谓词算出来的结果物化，不承载用户意图、预算或隐私。
- `status`：`retracted_by_model`（模型判定不再成立）与 `retracted_by_user`（用户撤销）**对称区分**，后者配 `retraction_reason` 区分 `false`/`outdated`（见 §4.1）；`pending`（迁移候选、低置信待定，仅 §5 迁移用）；`superseded`（被取代）；`dormant`（decay 转入，第三步用）；`challenged` 占位给 v2.2 的挑战流程（MVP 不产生，但写进 CHECK 省得日后重建表）。MVP 写端不产 `proposed`——没有每帖 proposer（§2.1）。
- `source` 复用反思器 scope 语义（global ← 公开 post；`soul:<name>` ← 该 SOUL 私聊+评论）。`user_locked` 这层 MVP 由 `source='user_authored'` 推导，暂不单列。
- `normalized_claim` / `superseded_by` / `retrieval_count` 占位，MVP 几乎不写，先建省得二次迁移。

> 隐私维度（`visibility` / `privacy_level`）**不在 MVP** 拆出来——它绑定的是"不再提起 / 彻底删除"那套删除 UX（§11）。MVP 的删除走 `retracted_by_user` + tombstone 即可。

### 1.2 `memory_unit_evidence`（unit ↔ raw 多对多）

仿 `post_entities`。evidence_id 复用反思器现成格式：`post:<id>` / `chat_message:<id>` / `comment_message:<id>`（见 `reflector._soul_thread_evidence_id`）。本轮按报告加了 `relation` 列，区分"支持 / 反驳 / 修订 / 原始出处"，为冲突与 challenge 留口（MVP 绝大多数是 `supports`）。

```sql
CREATE TABLE IF NOT EXISTS memory_unit_evidence (
    unit_id     TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL,
    relation    TEXT NOT NULL DEFAULT 'supports'
                  CHECK(relation IN ('supports','contradicts','revises','source')),
    created_at  REAL NOT NULL,
    PRIMARY KEY (unit_id, evidence_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_unit_evidence_eid ON memory_unit_evidence(evidence_id);
```

`idx_unit_evidence_eid` 是关键：它让"这条 raw 已被哪些 unit 覆盖"成为一次 join——当年 observation 做不到、只能靠猜的去重，本期读路"由 unit 顺 evidence 摸 raw"（§7.1 第 3 步）直接白嫖。

### 1.3 `memory_unit_ops`（审计 + 工作台的"整理记录"骨架）

把"每次整理改了什么"落成显式 op 日志，比从两份 md 快照反推 diff 干净。**人类"扫一眼了解自己"看 md prose（glance）；审计 diff 走这张表（audit）**——两个视图两个用途。

```sql
CREATE TABLE IF NOT EXISTS memory_unit_ops (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id         TEXT NOT NULL,
    related_unit_id TEXT,            -- supersede 时指向新/旧 unit
    op              TEXT NOT NULL,   -- add/confirm/revise/retract/supersede/
                                     -- user_create/user_edit/user_delete/migrate
    actor           TEXT NOT NULL,   -- 'reconciler' | 'user' | 'migration'
    before_json     TEXT,            -- 变更前快照（null 表示新建）
    after_json      TEXT,            -- 变更后快照（null 表示删除/墓碑）
    reflection_id   INTEGER REFERENCES reflections(id) ON DELETE SET NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unit_ops_unit ON memory_unit_ops(unit_id, id);
CREATE INDEX IF NOT EXISTS idx_unit_ops_reflection ON memory_unit_ops(reflection_id);
```

`reflections` 表继续保留，每次深反思仍插一行（记 scope/trigger/related_posts），但 `content` 不再是"被重写的 md 全文"，而是"本轮对账摘要"；逐条改动落 `memory_unit_ops`，外键回指 `reflections.id`。工作台一次整理的 diff = `SELECT * FROM memory_unit_ops WHERE reflection_id = ?`。

### 1.4 `memory_views`（把综合产物当受管对象，hash 驱动重综合）

按报告，`user.md` / `soul_memory` 不是随手文件，而是受管对象。这张表让"是否需要重综合"退化为一次 hash 比较，并支持**异步重综合**（标 `stale`、稍后渲，不卡在对账事务里）。

```sql
CREATE TABLE IF NOT EXISTS memory_views (
    id                   TEXT PRIMARY KEY,       -- mv_<ulid>
    scope                TEXT NOT NULL,          -- 'global' | 'soul:<name>'
    view_type            TEXT NOT NULL,          -- 'user_md' | 'soul_memory'
    content_md           TEXT NOT NULL,
    source_unit_set_hash TEXT NOT NULL,          -- core 集合 + 各成员物料字段的哈希
    renderer_version     TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'fresh'
                          CHECK(status IN ('fresh','stale','failed')),
    generated_at         REAL NOT NULL,
    updated_at           REAL NOT NULL,
    metadata             TEXT,
    UNIQUE(scope, view_type)
);

CREATE TABLE IF NOT EXISTS memory_view_units (
    view_id     TEXT NOT NULL REFERENCES memory_views(id) ON DELETE CASCADE,
    unit_id     TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    PRIMARY KEY (view_id, unit_id)
);
```

`memory_view_units` 记录"哪些 unit 渲进了这份画像"，是终态"回复出处标到具体 unit"的干净来源（比正则解析 md 锚点可靠）。若想再省，可先靠 §3.5 的锚点、把这张表归到 v2.1。

### 1.5 schema 落地方式

新表全部进 `schema.sql`（`init_db` 每次启动 `executescript` 且都是 `IF NOT EXISTS`，老库自动补建）。**CHECK 约束里的预留枚举值（如 `challenged`、`pending`）现在就写全**——SQLite 改 CHECK 需重建表，提前写省去日后痛苦迁移。`memory_units` 不需要 FTS（MVP 读路用向量召回，active unit 进现成 `vector_docs` / `vector_outbox` 出账管线，§7.1）。

---

## 2. 写路：深反思从"重写文本"改为"产出 unit ops"

### 2.1 现状回顾与层次定位

`reflector.trigger_global_deep_reflection` / `trigger_soul_deep_reflections` 现在：取 cursor 后的 raw → 调 `reflection_router.call_*_deep_reflection` 拿 `{reflection_md, patches}` → `_apply_*_patches` 打进 md（靠 `<!-- id: anchor -->` 锚点，evidence gating + sensitivity gate 已具备）→ 插 `reflections` 行。

**关键复用**：现有 patch 已带 `evidence`/`confidence`/`sensitivity`/`ops`，gating（`_patch_evidence_allowed`）现成。unit op 模型几乎是它的同构升级。

**层次定位（务必守住，否则 unit 退化成 observation 2.0）**：**轻反思禁用**——代码保留、从 `public_post_pipeline` 摘除、gate 一个开关可复活，四张表（entities/emotions/events/relations）留 schema 空置，`posts.importance` 冻在默认值（前端仅显示、无功能依赖，故不补）。**不引入每帖 proposer**：单帖一次 LLM 没有跨证据视野、只能转写，by construction 就是 observation 2.0；"先量产候选再 drop"是用 LLM 成本产废、再让深反思趟废堆。因此**抽象只发生在深反思**——它是唯一天然有跨证据视野（一窗 raw + 全部 active unit）的地方。unit **只承载跨证据抽象**；逐帖复述永远留在 raw 层。这与 Generative Agents 的 observation / reflection 分层同构（raw=observation，unit=reflection）。

> 为什么不要每帖 proposer（审阅结论）：proposer 若只看单帖 = observation 2.0；若给它"当前 post + 召回 raw + 相关 active unit"让它真·跨证据增量提案，那就是**每帖一次对账**（trickle reconcile）——把重调用搬上在线热路径、且把 unit 检索提前进写端。它是 v2 第三步的正解，但 MVP 不上，写进 [memory-v2-design.md](./memory-v2-design.md)。MVP 写端就是批量深反思直接抽取。

### 2.2 op 模型：深反思输出什么

把 `reflection_router` 的深反思 schema 从"文本 patch"换成"unit ops"。深反思读"自 cursor 起的 raw + 现存 active units + tombstones"，一次产出一批 op：

| op | 语义 | 落库动作 | 是否可能触发 md 重综合 |
| --- | --- | --- | --- |
| `add` | 跨证据抽出的新信念 | INSERT unit(status=`active`, source=`reflected`)，LLM 同时给 `type`/`tier`/`importance` + 连 evidence | 仅当新 unit 进 core 子集 |
| `confirm` | 旧 unit 再获证据 | `last_confirmed=now`、`confidence↑`、补 evidence 行，**content 不动** | **否**（§3.3 排除纯 confirm） |
| `revise` | 旧 unit 措辞/细节更新 | 见 §2.3 分支 | 仅当该 unit 在 core 子集内 |
| `retract` | 旧 unit 不再成立（模型判定） | status=`retracted_by_model` | 仅当该 unit 原在 core 子集内 |

每条 `add`/`revise`/`retract` 必带 `target_id`（除 add）、`evidence`（受 gating 约束，证据须落在本轮 scope 内）、`confidence`、`type`、`tier`、`importance`。沿用现有 `_patch_evidence_allowed` 门禁。**`add` 的强约束（防 observation 2.0）**：必须是跨证据抽象，不得逐帖转写——prompt 明示"一条 raw 不足以支撑一个 unit，除非它确立了一个跨时段成立的信念"；§8.1 抽样验"跨证据抽象 ≥ 70%、逐帖复述 ≤ 15%"是这条的验收。

### 2.3 `revise` 的两种实现（分支 D）

- **分支 D1（推荐 MVP）：就地 UPDATE content。** 旧 unit 直接改 content + `updated_at`，历史落 `memory_unit_ops.before_json`。简单。代价：丢失"同一信念历代措辞"的行级时间线（op 日志仍可回溯）。
- **分支 D2：每次 revise = 新 unit + 旧 unit superseded。** 真 bi-temporal。代价：行数膨胀，综合/检索处处要 `WHERE status='active'`，MVP 收益低。

> 推荐 D1。把 D2 留给"显式矛盾"：对账判定新旧**冲突**（非措辞更新）时，才 retract 旧 + add 新 + 写 `superseded_by`。即矛盾才进 bi-temporal，普通更新就地改。

### 2.4 对账的输入（分支 A，已含报告的补充）

要产出 confirm/revise/retract，LLM 必须看到相关现存 unit。

- **分支 A1（推荐 MVP）：把该 scope 下全部 active unit 一次性喂进去。** 对账要的是"这批新 raw 与**全部**已有信念逐一比对"，天然要全量、而非按 query 召回——所以即便读路已上 unit 向量检索（§7），对账输入仍走全量 A1，不复用读路的 top-k 召回（那会漏掉本轮 raw 未触及但需 retract 的旧 unit）。MVP 量级（演示库几百条）完全可控；且**现状深反思本就把整篇 `user.md` 喂进去**，喂全部 active unit 不比现在差。
- 分支 A2（entities/FTS/向量预筛）：当 active unit 数破阈值（建议 N≈150–200）后再降级到"全 core + 本轮 raw 相关召回"；MVP 量级不必。

**对账输入集**（不再喂 light signals——轻反思已禁用；global/soul 同构，都是 raw 直入）：自 cursor 起的本轮 raw evidence ＋ 当前 scope 全部 active units ＋ **当前 scope 的 tombstones（每条带 `retraction_reason`，见 §4.1）** ＋（可选）近几轮 `memory_unit_ops` 摘要。

- **global 深反思**：raw = 自 `reflections.scope_end` 起的公开 post（现成 cursor）。
- **soul 深反思**：raw = 自 `soul_thread_deep_cursor` 起的私聊+评论线程消息（现成 cursor）。

两条 scope 唯一区别是 raw 来源与 cursor，op 模型、active/tombstone 注入、apply 步完全同构。输出由 patch 改为 unit ops，cursor 语义不动。

### 2.5 对账粒度：批量 vs 滴流（分支 B）

- **分支 B1（本次选定）：完全保留现有批量深反思节奏与 cursor**，只换 apply 步（patch→unit ops）。触发时机、双车道、在途闸门、在线热路径一律不碰。抽象只在深反思这一处发生。
- 分支 B-split（每帖 proposer 产候选 + 深反思消解）：审阅否决——单帖 proposer = observation 2.0（量产废候选）；要它真·跨证据就等于每帖对账，见下。
- 分支 B2（每帖增量对账 / trickle reconcile）：把"当前 post + 召回 raw + 相关 active unit"喂 LLM 做增量提案，是新鲜度的正解、也是 sleep-time compute 的形态。但它需 unit 检索就位、LLM 频次上在线路、v1 隔离全到位。**留作 v2 第三步，写进 [memory-v2-design.md](./memory-v2-design.md)。**

> 选 B1。新鲜度在 MVP 由**读路的接缝 raw**（§7：自 cursor 起的近期原文逐字注入）兜住，不靠写端高频。"想更新鲜"就缩小深反思批次 / idle 多跑几次（免费旋钮），不复活 proposer。

### 2.6 写不变量（守住）

深反思 op 落库走 `db.immediate_transaction()`（同原 `_apply_light_reflection` 模式）：先 LLM 重算、再短事务批量写。md 综合（§3）在事务**外**做（甚至异步，见 §3.3），产物再用 `os.replace` 原子落盘——延续"重算在前、写锁在后、毫秒级写窗口"。unit 化后写窗口更小（行更新 vs 整段重写）。

---

## 3. user.md：core memory units 低频综合出的核心画像块

### 3.1 定性与角色（为什么 md 不能被"直接注入 core unit"替代）

md 是对 `in_md_slice` 的 core unit 的一次**综合（synthesis）**：有损、整合、有界、prose，**全量注入**回复 LLM。

**职责定位（决定渲染严苛度）**：md 给人格一个「这个用户整体是谁」的底色，用来更好地理解同时在场的其他 unit 与接缝 raw——**它是 orientation，不是事实精度的来源**（精度在 unit 的 confidence/evidence 与 raw）。所以真正承重的是**选对该进画像的 unit**（§3.2 的 selector：「该进 md 的 unit 进重要组」），而非把这段 prose 渲染得多么机械保真；md 略松可容忍，因为读路 precedence（事实信 raw、框架用 unit）会兜住偏差。

有人会问：既然 md 只来自 core unit 子集，回复时直接注入这批 unit 不就行了？——就信息含量而言确实如此，但读路优化的不是信息含量，而是 (成本 × 质量 × 稳定性 × 可读性)。预先综合好的 prose 在这四维上碾压一堆 unit 卡片：整合动作预先做一次而非每条回复重做；人设声音稳定；token 形状更省；且它本就要生成给人看，注入即免费。所以 md 的正当性不是"独占信息"，而是 **"core 身份切片的一个物化、综合过的缓存"**。

还有比成本更硬的理由：**检索照不到的身份地板。** 终态里 unit 按 query 检索，而"这个用户根本上是谁"必须在他发一条与身份毫不相关的帖子时也在场——基于当前帖检索不会召回它。md/core 就是那块 query 无关、保证恒在的地板。这正是 MemGPT/Letta 的 core memory 块、Generative Agents 周期综合的 self-summary 的角色；你日用的 `CLAUDE.md` 同理。

### 3.2 单向综合链、selector 与预算

绑定是一条干净的单向 DAG，**md 在 units 下游、无独立真相**（故无漂移）：

```
raw evidence ──reconcile──▶ units(结构化真相, 可检索, 带证据)
                              │  取 in_md_slice=1 的 core 子集
                              ▼  低频综合(synthesis, 见 §3.4)
                          user.md / soul_memories(prose 身份框, 全量注入 + 给人看)
```

约束：方向单向 unit→md；耦合是**综合 + 集合级 + 松**（不为每个 unit 锁一行，允许融成段落），非逐条 1:1 结构化绑定；源只取 units、不直接取 raw（否则 md 成第三个独立写入者）。

**core 子集准入谓词**（采纳报告默认值；隐私子句因 §11 延后而省略）：

```text
in_md_slice = 1 当且仅当：
  status = 'active'
  AND profile_policy != 'force_exclude'
  AND (
        profile_policy = 'force_include'
        OR ( tier = 'core'
             AND ( source = 'user_authored' OR confidence >= ENTER )
             AND importance >= 0.60 )
      )
```

**滞回**防边界抖动：`ENTER = 0.82`，`EXIT = 0.62`。**core 准入强制 `DWELL = 2`**（连续两次对账稳定才迁入 md；`tier='core'` 单元首次出现不直接进画像，至少经一轮 confirm）——`tier` 由对账 LLM 自评，常驻 md 的身份地板必须有稳定性缓冲，防单轮误判污染。`in_md_slice` 物化结果，避免每次重算全表。

**迁移衔接**：经人工确认的迁移 core 单元由 `profile_policy='force_include'` 进 md（见 §5），不依赖 `confidence>=ENTER`，避免迁移后冷启动空画像。

**type 权重**（用于 importance 估计与排序，建议初版）：identity 1.00、long_term_goal 0.95、stable_preference 0.90、relationship 0.88、enduring_project 0.85、durable_constraint 0.82、stage_state 0.65、insight 0.60、episodic_event 0.30、freeform 视用户选择。

**预算硬上限**（写死，否则 md 重新膨胀）：`user.md` ≤ 1200 中文字符 / ≤ 700 tokens；单个 `soul_memory` ≤ 600 字 / ≤ 350 tokens；一次 prompt 的 always-on baseline 总预算 ≤ 1200 tokens。超预算按序裁剪：`force_include` ＞ `user_authored core` ＞ identity ＞ long_term_goal ＞ relationship ＞ stable_preference ＞ project ＞ constraint。

`in_md_slice` 的**第二用途**（终态）：检索器据它跳过已在 md 的 core unit，避免"md 全量 + 检索 unit"双重注入。

### 3.3 重综合的触发：用 `memory_views` 的 hash 失效驱动

不再维护一张手写的"四条触发"清单，而是：**任何对账/编辑结束后，重算本 scope 的 `source_unit_set_hash`（= core 子集成员 + 各成员的 content/status/tier/sensitivity/profile_policy 物料字段的哈希）；与 `memory_views.source_unit_set_hash` 不一致则置 `status='stale'`。** 一个后台步把 stale 的 view 重综合（可异步，不卡对账事务）。`renderer_version` 变更同样置 stale。

**显式排除（不会改变 hash → 不重综合）**：纯 `confirm`（只动 last_confirmed/confidence）；confidence 小幅波动但未跨 slice 边界；core 子集外 unit 的任何增删改；retrieval_count 等维护字段。绝大多数对账 op 落在此处 → 重综合天然低频 → 接 LLM 的成本钉死在小常数。**触发病没有搬家**——这是"换记忆模型却没把批量重写挪到综合侧"的关键保证。

### 3.4 综合器：LLM 综合为主，模板仅作失败兜底

> **定性决定渲染策略**：md 是给人格的「用户整体底色」（§3.1），职责是 orientation 而非事实精度——精度在 unit 与 raw。因此渲染以 **LLM 直接综合 core units** 为主、prompt 级约束保真即可；**不**搞"模板即内容真值 + 机械校验无新增断言"那套：怎么机判"新增断言"本就模糊难落地，且 md 从不充当事实唯一来源，收益低。模板退回它该在的位置——**LLM 失败/不可用时的兜底地板**。

```
主路径： core units ──LLM 综合(orientation prose + prompt 约束)──▶ 预算检查 ──▶ user.md
兜底：   LLM 失败/超时 ──▶ 模板渲染(按 type 分组, 每 unit 一行) ──▶ user.md
```

- **LLM 综合（主路径，默认开）**：直接吃 core units 产连贯 prose。prompt 严格约束（**软约束，不另设机械 entailment 闸**）：只能用提供的 units、不得新增信息、不得把短期状态夸成长期身份、不稳定内容用"近期/阶段性"表述、压在 `char_budget` 内、风格直接不煽情、证据不足宁可省略。
- **模板兜底（仅失败时）**：LLM 失败/超时才触发，按 type 分组每 unit 一行 bullet，零 LLM、不可幻觉，保证 md 永远有内容。
- **保真靠三道软网而非硬闸**：① 上面的 prompt 约束；② md 不是事实来源——读路 precedence（事实信 raw、框架用 unit）兜住偏差；③ md 低频重综合、工作台 glance 面可人眼复核，发现不对就**改底层 unit**（md 不可直接编辑，§4），形成纠正回路。

`renderer_version` 记进 `memory_views`，prompt/格式变更时全量置 stale 重综合一次。

### 3.5 与回复读路的衔接

综合产物仍写到 `user.md` / `soul_memories/<name>.md` 的**老路径、老格式**（带 `## 章节`，sensitivity 分级保留），**全量注入**不变。于是 `memory_review_service.read_*` 读到的 md "形状"不变——**md 这一段**回复链路零改动。**但本期读路在 md 之外加了 unit 检索 + 接缝 raw（§7）**：md 仍是常驻 core 画像，额外多了"按 query 召回的非 core unit + 顺 evidence 摸到的 raw + 接缝 raw"。区别两点：md 现在是机器综合产物；md 不再是回复唯一的记忆来源。

**生成文件头**（廉价卫生，声明它是产物、为未来冲突检测埋线）：综合时在文件头写

```markdown
<!-- generated_by=tracelog view_type=user_md editable=false
     source_unit_set_hash=sha256:... renderer_version=baseline-v1
     generated_at=... content_hash=sha256:... -->
```

并建议给每段埋 `<!-- tracelog:units=u1,u2 -->` provenance 锚点（region 级，非逐行锁），支撑本期读路"回复用了哪些 unit"的归因（§7.1），逐 region 精确归因留 v2.1，零成本。MVP 不实现"用户改本地文件后回灌"的冲突流程（md 不可编辑，见 §4 与 §11）。

---

## 4. 用户编辑：MVP 不碰 md，编辑面落在 unit 工作台

**锁定原则：`user.md` 永不接受用户直接编辑（纯综合产物）；用户的一切编辑都以 unit 为对象，发生在工作台。** 从根上消掉"用户改 prose → 反解析成 unit ops"的 NL-diff 噩梦。要支持自由写一段话，也走"freeform = 退化 unit"（逐字渲染、user_authored、对账免疫）。

**MVP 范围与可裁剪度**：工作台两面——**glance**：渲染出的 md prose，给用户"系统怎么看我"的镜子；**manage**：units 一条条列出，可下钻 evidence、看 op 日志 diff。编辑（增删改）落在 manage 面；按"用户编辑可以再说"，**编辑深度可裁**：MVP 可先上只读 manage 面（展示 + diff + 下钻），把增/删/改作为紧随其后一步。§4.1/§4.2 两条硬规则在启用增/删后即为必需，设计先就位。

### 4.1 用户删除是多意图动作，按原因分流（启用删除后必需，分支 E）

用户点「删除」至少混了三种意图，且对对账作用**相反**，必须分流，不能一刀切落 `retracted_by_user`：

1. **错的 / 从来不对**（`retraction_reason='false'`）：`status='retracted_by_user'` + 写 tombstone。防诈尸**开**——支撑它的 raw 还在，下次对账会重派生，必须抑制。
2. **曾经对、现在过时**（`retraction_reason='outdated'`）：`status='retracted_by_user'` + tombstone。防诈尸**软**——允许在**有新证据**时重新派生，但 confidence 重算；不是「永不再现」。（decay 驱动的 dormant 变体留第三步。）
3. **属实、但别再提 / 别写进画像**：**这不是删除**。`status` 保持 `active`，置 `profile_policy='force_exclude'`，**不写 tombstone**，对账**应继续 confirm 它**。注意 MVP 读路已含 unit 检索（§7）：`force_exclude` 只挡 md 画像，**不挡检索召回**——一条 active 的 force_exclude unit 仍可能被 query 召回进回复。要"连检索也屏蔽"需 `visibility='no_prompt'`（§11，v2.2）。MVP 若要严格"任何地方都别提"，临时手段是改判为 `retracted_by_user`（reason=`outdated`），代价是丢了"它仍为真"的语义——这正是 `no_prompt` 存在的理由，故 v2.2 必补。

所有删除都保留 evidence 连接与 op 日志（`memory_unit_ops.after_json` 已快照 `retraction_reason`，无需另加列）。

**防诈尸按 `retraction_reason` 分流**（修正分支 E 的一刀切）：

- **分支 E1（推荐）：prompt 级抑制。** 对账输入带本 scope 的 tombstones（每条含 `retraction_reason`，§2.4）：对 `false` 严禁再生成同义条目；对 `outdated` 仅作软提示。代价：软约束。
- **分支 E2：落库级去重守卫。** 深反思 `add` 落库前与 `false` tombstone 做近似查重（`normalized_claim` / 实体重叠 / 第二步 embedding），命中则丢弃该 add。

> 推荐 E1 起步，叠一条**廉价硬规则**（仅对 `false`）：若某 `add` 的 evidence 集合 ⊆ 某 `false`-tombstoned unit 的 evidence 集合，直接抑制。E2 的 embedding 查重等第二步 unit 进向量库后补上。

### 4.2 用户手写/编辑 = ground truth，对账免疫（启用后必需，分支 F）

用户新增的 unit：`source='user_authored'`、`confidence=1.0`、evidence 可空。**编辑一条 `reflected` unit 时，按报告建议新建一个 `user_authored` unit 并把旧 reflected 标 `superseded`**——"系统原先这么认为"与"用户后来明确这么说"并列可追溯，比就地改更利于审计与未来 challenge。

- 进 core 子集：§3.2 谓词里 `source='user_authored'` 直接过 ENTER 门槛，默认进 md。
- 对账免疫：
  - **分支 F1（推荐 MVP）：硬免疫。** 对账的 retract/revise 不得作用于 user_authored unit；模型只能 `confirm`。代价：用户旧断言永不衰减/纠正。
  - 分支 F2：挑战不自动应用（生成 `challenged` 挂工作台待确认）。UX 更好、工作量更大，留 v2.2（§11）。

> 推荐 F1。`source` 字段是这两条规则的统一解：库里从此区分"用户的话"与"模型的推断"，对账区别对待。

### 4.3 工作台后端 API（`memory_review_service` 旁新增 `memory_unit_service`）

现有 `read_*`（服务 glance 面读 md）、`list/get_*_revisions` 保留；`save_*` 在 md 不可编辑后退居"导出/快照"或停用。新增：

- `list_units(scope, *, status=None, tier=None, in_slice=None)` — manage 主列表。
- `get_unit(unit_id)` / `get_unit_evidence(unit_id)` — 详情 + 下钻 raw。
- `list_unit_ops(*, reflection_id=None, unit_id=None)` — "整理记录 / 改了什么"，复用 ReflectionsPage 的 RevisionDetailBlock（改吃 op 日志）。
- *（编辑启用后）* `create_unit`（user_authored，可选 tier/profile_policy）/ `update_unit`（reflected→新建 user_authored + supersede 旧；亦承载 `profile_policy='force_exclude'` 即「属实但别再提」）/ `delete_unit(unit_id, reason)`（`reason∈{'false','outdated'}` → `retracted_by_user` + `retraction_reason` + 墓碑）；改到 core 子集则置 view stale。UX：删除按钮至少二分——「这条不对（删掉别再生成）」 vs 「属实但别再提 / 别写进画像」（后者走 `update_unit` 的 force_exclude，**不**走 delete）。

---

## 5. 迁移：现有 md → units（分支 G，含报告细化）

老库里 `user.md` / `soul_memories/<name>.md` 已有内容。翻转前要变成 units，但**不直接全部写成 active**：

1. 读旧 md，按章节切分（身份/偏好/项目/关系/阶段背景）。
2. LLM 抽候选 units：`source='migrated'`、**`status='pending'`**、**`confidence` 初始 ≤ 0.75**、尽力回填 evidence 或留空。
3. 生成 preview：候选 unit 列表 + 由这些 unit 合成的新 md + 与旧 md 的并排 diff。
4. **人工确认**（accept / reject / merge 去重）。确认后转 `active`；**被确认为四类 core（身份/长期目标/核心偏好/核心项目）的单元置 `profile_policy='force_include'`**（确认即用户意图，经 §3.2 force_include 子句直接进 md，避免冷启动空画像），`source` 保持 `migrated` 存出处；旧 md 留备份。

**迁移验收线**：身份、长期目标、核心偏好、当前核心项目四类不得缺失，且**渲染出的** `user.md` 必须实际包含被确认的这四类 core 单元（不只是库里有 unit，而是 selector 真的放行进了 md）；新 md 不得出现旧 md 没有、units 不支持的断言；合成 `user.md` 不超过旧版长度 +20%；候选 units 里"单条 raw 转写型垃圾"≤ 15%。

> 这里的人工确认用迁移脚本里的一步实现即可，**不必**上 §11 的 `memory_reviews` 队列。分支 G2（不回填、起于空、旧 md 冻结前置）作为回填质量不达标时的降级。

---

## 6. shadow 验证窗口 vs 直接翻转（分支 C）

"md 从深反思手写翻转为 core unit 综合"是 MVP 风险最高的子项。推荐**折中（短 shadow + go/no-go）**：

- 先双写 **2–3 个真实整理周期**：保留旧 patch→md 路径继续产出旧 md，并行产出 `user.synth.md`。
- 每轮并排比较四项：信息是否丢失、是否幻觉、长度是否超预算、回复质量是否退化。
- 全绿过验收门（§8）→ 翻转，旧 md 留副本可回滚。

shadow 期以"天"计而非"周"，迁就答辩档期；比一刀切稳。

---

## 7. 读路改造（本期纳入，带 flag + md-only 回退）

units 只写不读 = 影子库，且 demo 的"懂你 + 点出处"靠读路真的用上 unit。所以 MVP 把愿景 §4 的读路落进来——但因为它**动每条回复的热路径**，是本期最大风险项，全程带 `MEMORY_V2_READ` flag，关掉即退回今天的 md-only + raw hybrid_search。

### 7.1 本期读路（flag 开时）

现状 `build_public_post_reply_context` 直接 `hybrid_search` 打 raw 池取 top-k。改为有序展开：

```
md 全量注入(core 画像, 常驻无 query)                       ← §3 综合产物
  + 检索 unit(非 core, 按 query 召回, 带 confidence/type/自何时)  ← retrieve_units
  + raw(① 命中 unit 顺 evidence_ids 摸到  ② hybrid_search 直达孤儿话题)
  + 接缝 raw(自 cursor 起、尚未对账成 unit 的近期原文, 逐字)
  + 读取规则(precedence: 事实信 raw, 框架用 unit, 冲突以新为准)
```

1. **md 常驻**：core 画像无 query 注入（不变）。
2. **检索 unit**：`retrieve_units(query, scopes, k≈6–8, exclude_in_md=True)` — active unit 进向量库（复用 `vector_docs`/`vector_outbox`），ANN 召回非 core 的相关 unit，带 confidence/type/自何时。**MVP 用最简语义召回**（单一 embedding 相似度 + status=active + 排除 in_md_slice）；多信号打分（semantic+BM25+entity+time+importance）留 v2.1（§11）。
3. **顺 evidence 摸 raw**：命中 unit 已挂 evidence_ids，按需 hydrate 源 raw；命中 unit 已覆盖的 raw 不重复塞，仅当 raw 能补 unit 抽象掉的细节时才带。
4. **接缝 raw**：自 cursor（`soul_thread_deep_cursor` / 全局 `reflections.scope_end`）起的近期原文逐字带上，不论与当前话题是否相关——这是模型对"刚发生"的感知，也是 MVP 不靠写端高频的新鲜度来源（§2.5）。
5. **precedence 写进回复 prompt**：事实信 raw（尤其接缝近期 raw）、框架用 unit（带 confidence 软着说）、近期 raw 与旧 unit 矛盾以 raw 为准。

### 7.2 回退与验证（硬要求）

- **feature-flag**：整条新读路挂 `MEMORY_V2_READ`；关掉 = 回今天的 md-only + raw hybrid_search，零行为变化。出问题一键回滚。
- **回归门**：§8.1"回复不回归"从"兜底回归"升为**硬验收**——flag 开/关同组 post 跑回复，质量不得下降。
- **分阶段**：可先只加"检索 unit"不加接缝 raw、再叠接缝，逐段验证；任一段回归即单独关。

### 7.3 仍然预留、本期不全用的缝

1. **`memory_views` / `memory_view_units` + provenance 锚点**（§1.4 / §3.5）："回复出处标到具体 unit"——MVP 可先标到"用了哪些 unit"，逐 region 精确归因留 v2.1。
2. **`memory_unit_evidence.relation`**（支持/反驳区分）：MVP 召回只用 supports，contradicts/revises 留图谱化。
3. **`retrieve_units` 多信号打分**：MVP 单 embedding，打分公式 v2.1。
4. **`retrieval_count` 回灌**（读时记 unit 被检索/采用 → decay 信号）：第三步，MVP 不写。

冲突一致性由愿景文档 §4.4 precedence 处理（已落进 7.1 第 5 步）；本设计单向 unit→md + raw 始终是 unit 上游证据，与之兼容。

---

## 8. 验收门与演示脚本

### 8.1 验收门（翻转前必须全绿）

- **结构正确性**：schema 初始化成功、老 DB 可补表；CHECK 生效；`memory_views` fresh/stale 切换正确；hash 变化触发重综合。
- **记忆质量**：抽样 50 条 reflected unit，**跨证据抽象 ≥ 70%**，"逐帖复述"≤ 15%；合成 md 在预算内；抽样人眼复核合成 md **大体忠于 core units、无明显臆造**（软标准——md 是 orientation 非事实源，不做机械校验，见 §3.4）。
- **用户控制**（启用编辑后）：user_authored 不被对账自动改写；`force_exclude`（属实但别再提）保持 active、不进 md、不被 tombstone、对账仍可 confirm；`reason='false'` 删除后同 evidence 不诈尸；`reason='outdated'` 删除无新证据不复活、有新证据可重派生且 confidence 重置。
- **重综合低频**：埋点——纯 confirm / core 外 op **零重综合**，重综合次数 ≪ 对账 op 次数。
- **回复不回归（硬门，读路已变）**：`MEMORY_V2_READ` flag 开/关同组 post 跑回复，开 flag 质量**不得低于**关 flag（md-only 基线）；理想是"懂得更多"。这是本期读路改造（§7）的承重验收，非"兜底"。
- **读路正确性**：检索 unit 排除 in_md_slice（不双注入）；顺 evidence 摸到的 raw 不与召回 unit 重复；接缝 raw 确实带上自 cursor 起的近期原文；flag 关时读路与今天逐字一致。

### 8.2 演示脚本（冲刺答辩用，5 幕）

1. **看到画像**：打开工作台 glance 面，展示 `user.md` + manage 面 units 列表。
2. **看到出处**：点开一条 unit，下钻其 raw evidence。
3. **用户纠正系统**：把一条 reflected unit 改成 user_authored，触发新 md 综合，画像随之更新。
4. **删除不诈尸**：删一条记忆 → 跑一次整理 → 证明它不复活。
5. **回复效果（读路高光）**：发一条与 core 画像无关的帖 → 回复里既有 md 给的稳定身份底色、又召回了相关的非 core unit、还带最近动态（接缝 raw）；点开回复能看出"用了哪些 unit / 哪些 raw"。对比 flag 关（md-only）凸显"懂得更多但不乱编"。

---

## 9. 落地顺序（建议）

1. 建表（§1：units / evidence / ops / views / view_units）进 schema.sql + 单测建表/约束。
2. `memory_unit_service`：先 list/get/op 日志（只读 manage 面）跑通展示与 diff；增删改与 §4 墓碑/免疫作为紧随一步。
3. 综合器（§3.4 LLM 综合为主 + 模板失败兜底）+ core 子集 selector/滞回（§3.2，core 强制 DWELL）+ `memory_views` hash 失效（§3.3），先对"手造 units"综合，验产物形状与老 md 一致、预算达标。
4. **禁用轻反思**：`public_post_pipeline` 摘除 `run_light_reflection`（gate 到开关、保留可复活）；更新 `test_public_post_pipeline`。注意保留 `maybe_trigger_global_deep_reflection`。
5. 改 `reflection_router` 深反思 schema：文本 patch → unit ops；`reflector` apply 步（global + soul）改写为 §2 op 落库（add/confirm/revise/retract，保留 gating；对账输入 = raw + active units + tombstones，§2.4）。
6. **读路改造（本期最大风险项，带 flag）**：active unit 进向量库（复用 `vector_docs`/`vector_outbox`）；`retrieve_units` 空壳填上最简语义召回（§7）；`context_builder` 加"md 全量 + 检索 unit + 顺 evidence 摸 raw + 接缝 raw + precedence 规则"，整条挂 `MEMORY_V2_READ` flag，关掉即回 md-only。
7. 预留缝（§7）随手落地：evidence relation、`tier`/`importance`、provenance 锚点与文件头、接缝 cursor 复用。
8. 迁移脚本（§5）+ shadow 跑（§6）+ 验收门（§8，含"回复不回归"硬门）。
9. 翻转：删旧 patch→md 路径，md 改由综合产出；读路 flag 开。工作台 ReflectionsPage 接 op 日志 diff。

decay/重组、**每帖增量对账（trickle）**、unit 检索多信号打分——第三步 / v2.1，不在此清单。

---

## 10. 决策状态与待你拍板的分支

**已定（含本轮吸收报告后的更新）**：

| 议题 | 结论 |
| --- | --- |
| md 定性 | core memory units 低频**综合**出的核心画像块，全量注入；非机械渲染 |
| 三层分工 | raw=主记忆/证据；unit=结构化真相+审计；md=身份框 |
| 绑定 | 单向 unit→md，综合（非投影），集合级松绑 |
| md 编辑 | MVP 不做；编辑面永远在 unit 工作台，md 纯产物 |
| 字段语义 | 去过载：`tier`/`profile_policy`/`importance`/`sensitivity`(仅门禁) 各司其职，`in_md_slice` 降为缓存 |
| 渲染 | **LLM 综合为主（prompt 约束）+ 模板仅失败兜底**；md 是 orientation 非事实源，不设机械校验闸 |
| 重综合触发 | `memory_views.source_unit_set_hash` 失效驱动，可异步 |
| 对账输入 | raw(自 cursor) + 全部 active units(A1) + tombstones(带 reason)；global/soul 同构，仅 raw 源与 cursor 不同 |
| 写时机 | **批量深反思直接抽取（B1）**，不做每帖 proposer（= observation 2.0）；新鲜度靠读路接缝 raw |
| 读路 | **本期改造**：md 全量 + 检索 unit + 顺 evidence 摸 raw + 接缝 raw + precedence；带 flag 可回 md-only |
| 轻反思 | **MVP 禁用**（代码留、不挂管线、四表空置、importance 不补） |
| 每帖增量对账 | trickle reconcile = v2 第三步，写进愿景文档；MVP 不上 |
| 删除语义 | 按 `retraction_reason` 分流：false（防诈尸）/ outdated（可重派生）/「别再提」= force_exclude 非删除 |

**待你拍板**：

| 分支 | 选项 | 我的推荐 |
| --- | --- | --- |
| C 翻转策略 | C1 长 shadow / C2 直接翻转 / 折中 | **折中（短 shadow + go/no-go）** |
| D revise 实现 | D1 就地改 / D2 一律 supersede | **D1（矛盾才 supersede）** |
| E 防诈尸 | E1 prompt 抑制 / E2 落库查重 | **E1 + 证据子集硬规则** |
| F 手写免疫 | F1 硬免疫 / F2 挑战待确认 | **F1**（F2 留 v2.2） |
| G 迁移 | G1 LLM 回填(pending+人工确认) / G2 起于空 | **G1（不达标降 G2）** |
| 编辑是否进 MVP 首版 | 只读 manage / 含增删改 | 你定（影响 §4.3 落地时点） |

默认数值已按报告填入（ENTER 0.82 / EXIT 0.62 / DWELL 2、type 权重表、预算上限、迁移 confidence ≤0.75 与验收线、抽象率 ≥70%）。你可直接用或微调。

---

## 11. 已评估但暂不纳入 MVP（附理由）

记录研究报告里"形状正确、但服务的是 v2.1/v2.2 功能"的设计，**预留而不现在建**，免得日后被当成新发现重捡。

- **`memory_reviews` 队列 + challenge / pending-review / import-diff 流程**：服务 F2 挑战、raw 删除 reconsider、外部 md 导入。MVP 走 F1 硬免疫、无这些入口，表基本空转。预留形状，不建流程。
- **隐私全分类 `visibility`(no_prompt/private/restricted) + `privacy_level`(normal/private/sensitive)**：v2.2。「错的 vs 属实但隐藏」的**删除意图分流**前移进 MVP（`retraction_reason` 列 + `force_exclude` 路由，堵住「把真事实当假抹掉」的 bug，见 §4.1）。**但因 MVP 读路已含 unit 检索，`force_exclude` 只挡 md、挡不住检索召回**——"属实但任何地方都别提"需 `no_prompt`，这条因读路前移而从"可有可无"升级为 **v2.2 必补**（§4.1 意图 3 的缺口）。raw scrub 仍留 v2.2。
- **raw 证据删除流程 + `memory_audit_logs` 审计表 + scrub 为 `[deleted_by_user]`**：高风险、影响审计与 unit 完整性，属隐私治理（v2.2）。
- **`retrieve_units` 多信号打分**（semantic+BM25+entity+time+importance+source_bias）：v2.1。MVP **已上**最简语义召回（单 embedding，§7.1），仅多信号打分公式延后。precedence 规则本身在 MVP 落地（§7.1 第 5 步），只是权重不调优。
- **本地文件编辑的 hash 冲突三向 diff 回灌**：md 在 MVP 不可被用户编辑，仅写生成文件头（§3.5），不做冲突解决流。
- **`user_locked` 独立列**：MVP 由 `source='user_authored'` 推导。
- **`normalized_claim` 的实际填充与 embedding 级查重**：列占位，等 E2 再启用（MVP 读路虽已上向量库，但 add 去重仍用 E1 prompt 抑制 + 证据子集硬规则，不依赖 normalized_claim）。
- **轻反思**：MVP **禁用**（非延后——代码保留、gate 开关、不挂管线，四表空置）。其实体/关系图作为 v2 unit 骨架的数据源，届时需重启轻反思或由每帖增量对账（trickle）补吐。
- **每帖增量对账 / trickle reconcile（分支 B2）**：把"当前 post + 召回 raw + 相关 active unit"喂 LLM 做在线增量提案，是 sleep-time compute 的正解、新鲜度上限最高。需 unit 检索就位 + 在线 LLM 频次控制 + v1 隔离全到位。**v2 第三步，已写进 [memory-v2-design.md](./memory-v2-design.md)。** MVP 用批量深反思 + 读路接缝 raw 替代。

> 取舍原则：MVP 切"写端 unit 化 + 读端最简 unit 检索"，把每帖增量对账（trickle）、decay/重组、隐私治理、多信号打分等留给后续。**预留 schema 形状 ≠ 现在就建流程；上最简读路 ≠ 上完整检索栈。**
