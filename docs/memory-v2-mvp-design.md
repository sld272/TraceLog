# TraceLog 记忆 v2 — MVP 落地设计

> 状态：实现前定稿（feat/memory-v2 分支）。本文是 [memory-v2-design.md](./memory-v2-design.md) 第 6 节"第一步核心赌注"的可落地展开，把愿景里被一句话带过的细节钉成 schema、op 模型、迁移路径、验收门。
>
> 已纳入 [memory-v2-deep-research-report.md](./memory-v2-deep-research-report.md) 的评估结论：采纳"字段去过载、模板优先渲染、view 作为受管对象（hash 驱动重综合）、对账输入补 signals/tombstones"四处修订与一批默认数值；其余完整 v2 设施（review 队列、隐私全分类、检索打分等）按 §11 记账延后。凡仍有合理岔路的地方以 **分支** 列出，标注代价与推荐。
>
> 相关：[architecture.md](./architecture.md)、[database.md](./database.md)、[auto-reflection-design.md](./auto-reflection-design.md)。

---

## 0. MVP 的边界：只换"写的表示"，不换"写的时机"，更不换"读"

### 0.1 三层真相分工（先把名词钉死）

- **raw evidence —— 原始证据层。** post / chat_message / comment 原文，不可变，是一切记忆的最终出处。**它今天就是主记忆**：回复读路真正承重的是对 raw 池的 hybrid_search，不是 md。v2 之后 raw 仍是主记忆，units 只是给它补上抽象、出处与置信。
- **memory unit —— 结构化真相层 + 审计层。** 一等公民：带 id / 置信 / 证据链 / 状态 / 时间。深反思（对账）的落点；工作台增删改查的对象；终态读路检索的目标。
- **user.md / soul_memories —— 由 core memory units 低频**综合（synthesis）**出的核心画像块。** **不是** unit 的机械渲染/投影，而是对 core 子集的一次有损、整合、有界的综合。它**全量注入**回复 LLM，给模型一个稳定的身份地板，也给用户一面直观的镜子。

一句话：**raw 给真相，unit 给精度与出处，md 给一个恒在的身份框。**

### 0.2 MVP 只动其中两件事

整份 v2 愿景有三条腿：①记忆单元化（写的表示）②读路 md→unit→raw 变焦 ③衰减/重组/滴流对账（写的时机）。**MVP 只做第①条，并且刻意把它做到不惊动读路。** 这是本设计最重要的克制，它一次性拆掉三个风险：

- **读路不动 → 演示稳定性不受影响。** 回复生成今天怎么读 `user.md` / `soul_memories`，MVP 之后还是怎么读——它读的仍是一篇全量注入的 Markdown，只不过这篇 md 从"深反思直接重写"变成"由 core unit 低频综合"。回复链路一行 prompt 都不用改。
- **触发时机不动 → 触发难题继续搁置。** MVP **保留**现有深反思的批量节奏与 cursor，只把它的**输出**从"文本 patch"换成"unit ops"。滴流对账（第 3 档、高频）是 v2 第三步，不进 MVP。
- **范围单点 → 单点赌注名副其实。** 本轮吸收研究报告时，刻意只取"写端单点"那一刀，不被完整 v2 的设施诱导着翻倍工程量（见 §11）。

一句话 MVP 验收：**深反思不再重写 md，而是产出 unit ops 落到 `memory_units` 表；`user.md` / `soul_memories` 改由 core unit 低频综合产出（全量注入不变）；工作台改为以 unit 为对象。回复读路、触发时机原样不动。**

明确**不在 MVP**：unit 进向量库 / 读时检索 unit / 接缝 raw 注入（第二步）；decay / 重组 / 高频滴流对账（第三步）；图谱化矛盾边失效、challenge 流程、隐私删除 UX（见 §11）。但第二步的接口缝必须在 MVP 就预留好（见 §7）。

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
- `status`：`retracted_by_model`（模型判定不再成立）与 `retracted_by_user`（用户撤销）**对称区分**；`pending`（迁移候选、低置信待定）；`superseded`（被取代）；`challenged` 占位给 v2.2 的挑战流程（MVP 不产生，但写进 CHECK 省得日后重建表）。
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

`idx_unit_evidence_eid` 是关键：它让"这条 raw 已被哪些 unit 覆盖"成为一次 join——当年 observation 做不到、只能靠猜的去重，到第二步"由 unit 顺藤摸 raw"直接白嫖。

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

新表全部进 `schema.sql`（`init_db` 每次启动 `executescript` 且都是 `IF NOT EXISTS`，老库自动补建）。**CHECK 约束里的预留枚举值（如 `challenged`、`pending`）现在就写全**——SQLite 改 CHECK 需重建表，提前写省去日后痛苦迁移。`memory_units` 不需要 FTS（MVP 不检索 unit；第二步进向量库走现成 `vector_docs` / `vector_outbox` 出账管线）。

---

## 2. 写路：深反思从"重写文本"改为"产出 unit ops"

### 2.1 现状回顾与层次定位

`reflector.trigger_global_deep_reflection` / `trigger_soul_deep_reflections` 现在：取 cursor 后的 raw → 调 `reflection_router.call_*_deep_reflection` 拿 `{reflection_md, patches}` → `_apply_*_patches` 打进 md（靠 `<!-- id: anchor -->` 锚点，evidence gating + sensitivity gate 已具备）→ 插 `reflections` 行。

**关键复用**：现有 patch 已带 `evidence`/`confidence`/`sensitivity`/`ops`，gating（`_patch_evidence_allowed`）现成。unit op 模型几乎是它的同构升级。

**层次边界（务必守住，否则 unit 退化成 observation 2.0）**：轻反思**不变**，它继续抽 entities/emotions/events/relations——本质是贴近单条 raw 的 **signal 层**（特征，不是真相）。unit **只承载跨证据抽象**；逐帖复述永远留在 signal 层与 raw 层。这与 Generative Agents 的 observation / reflection 分层同构。

### 2.2 op 模型：LLM 输出什么

把 `reflection_router` 的深反思 schema 从"文本 patch"换成"unit ops"。每条 op：

| op | 语义 | 落库动作 | 是否可能触发 md 重综合 |
| --- | --- | --- | --- |
| `add` | 新抽象信念 | INSERT unit(status=active, source=reflected)，LLM 同时给 `type`/`tier`/`importance` + 连 evidence | 仅当新 unit 进 core 子集 |
| `confirm` | 旧 unit 再获证据 | `last_confirmed=now`，`confidence↑`，补 evidence 行，**content 不动** | **否**（§3.3 排除纯 confirm） |
| `revise` | 旧 unit 措辞/细节更新 | 见 §2.3 分支 | 仅当该 unit 在 core 子集内 |
| `retract` | 旧 unit 不再成立（模型判定） | status=`retracted_by_model` | 仅当该 unit 原在 core 子集内 |

每条 `add`/`revise`/`retract` 必带 `target_id`（除 add）、`evidence`（受 gating 约束，证据须落在本轮 scope 内）、`confidence`、`type`、`tier`、`importance`。沿用现有 `_patch_evidence_allowed` 门禁。

### 2.3 `revise` 的两种实现（分支 D）

- **分支 D1（推荐 MVP）：就地 UPDATE content。** 旧 unit 直接改 content + `updated_at`，历史落 `memory_unit_ops.before_json`。简单。代价：丢失"同一信念历代措辞"的行级时间线（op 日志仍可回溯）。
- **分支 D2：每次 revise = 新 unit + 旧 unit superseded。** 真 bi-temporal。代价：行数膨胀，综合/检索处处要 `WHERE status='active'`，MVP 收益低。

> 推荐 D1。把 D2 留给"显式矛盾"：对账判定新旧**冲突**（非措辞更新）时，才 retract 旧 + add 新 + 写 `superseded_by`。即矛盾才进 bi-temporal，普通更新就地改。

### 2.4 对账的输入（分支 A，已含报告的补充）

要产出 confirm/revise/retract，LLM 必须看到相关现存 unit。MVP 没有 unit 向量检索（第二步）。

- **分支 A1（推荐 MVP）：把该 scope 下全部 active unit 一次性喂进去。** 零额外检索设施；MVP 量级（演示库几百条）完全可控；且**现状深反思本就把整篇 `user.md` 喂进去**，喂全部 active unit 不比现在差。直接绕开"对账依赖检索、检索又在第二步"的鸡生蛋。
- 分支 A2（entities/FTS 预筛）/ A3（提前上向量检索）：复杂度提前，MVP 不值。

**完整对账输入集**（按报告补 signals 与 tombstones）：本轮 new raw evidence ＋ 本轮 light signals ＋ 当前 scope 全部 active units ＋ **当前 scope 的 tombstones / `retracted_by_user` claims**（防诈尸，见 §4.1）＋（可选）近几轮 `memory_unit_ops` 摘要。

### 2.5 对账粒度：批量 vs 滴流（分支 B）

- **分支 B1（推荐 MVP）：完全保留现有批量深反思节奏与 cursor**，只换 apply 步。触发时机、双车道、在途闸门一律不碰。
- 分支 B2（立即上高频滴流）：需 v1 隔离方案全部到位，LLM 频次飙升，演示前引入新不稳定源。

> 推荐 B1。"换数据模型"和"换触发节奏"是两件事，MVP 只做前者。

### 2.6 写不变量（守住）

unit op 落库走 `db.immediate_transaction()`（同 `_apply_light_reflection`）：先 LLM 重算、再短事务批量写。md 综合（§3）在事务**外**做（甚至异步，见 §3.3），产物再用 `os.replace` 原子落盘——延续"重算在前、写锁在后、毫秒级写窗口"。unit 化后写窗口更小（行更新 vs 整段重写）。

---

## 3. user.md：core memory units 低频综合出的核心画像块

### 3.1 定性与角色（为什么 md 不能被"直接注入 core unit"替代）

md 是对 `in_md_slice` 的 core unit 的一次**综合（synthesis）**：有损、整合、有界、prose，**全量注入**回复 LLM。

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

**滞回**防边界抖动：`ENTER = 0.82`，`EXIT = 0.62`，可选 `DWELL = 2`（连续两次对账稳定才迁移）。`in_md_slice` 物化结果，避免每次重算全表。

**type 权重**（用于 importance 估计与排序，建议初版）：identity 1.00、long_term_goal 0.95、stable_preference 0.90、relationship 0.88、enduring_project 0.85、durable_constraint 0.82、stage_state 0.65、insight 0.60、episodic_event 0.30、freeform 视用户选择。

**预算硬上限**（写死，否则 md 重新膨胀）：`user.md` ≤ 1200 中文字符 / ≤ 700 tokens；单个 `soul_memory` ≤ 600 字 / ≤ 350 tokens；一次 prompt 的 always-on baseline 总预算 ≤ 1200 tokens。超预算按序裁剪：`force_include` ＞ `user_authored core` ＞ identity ＞ long_term_goal ＞ relationship ＞ stable_preference ＞ project ＞ constraint。

`in_md_slice` 的**第二用途**（终态）：检索器据它跳过已在 md 的 core unit，避免"md 全量 + 检索 unit"双重注入。

### 3.3 重综合的触发：用 `memory_views` 的 hash 失效驱动

不再维护一张手写的"四条触发"清单，而是：**任何对账/编辑结束后，重算本 scope 的 `source_unit_set_hash`（= core 子集成员 + 各成员的 content/status/tier/sensitivity/profile_policy 物料字段的哈希）；与 `memory_views.source_unit_set_hash` 不一致则置 `status='stale'`。** 一个后台步把 stale 的 view 重综合（可异步，不卡对账事务）。`renderer_version` 变更同样置 stale。

**显式排除（不会改变 hash → 不重综合）**：纯 `confirm`（只动 last_confirmed/confidence）；confidence 小幅波动但未跨 slice 边界；core 子集外 unit 的任何增删改；retrieval_count 等维护字段。绝大多数对账 op 落在此处 → 重综合天然低频 → 接 LLM 的成本钉死在小常数。**触发病没有搬家**——这是"换记忆模型却没把批量重写挪到综合侧"的关键保证。

### 3.4 综合器：模板优先 + LLM 润色（带校验闸）

> 本轮按报告**翻转**了渲染策略：从"LLM 优先 + 模板兜底"改为"**模板优先（内容真值 + 校验基准）+ LLM 润色（受闸门约束）**"。理由：模板兜底只在 LLM *失败* 时触发，挡不住 LLM *悄悄幻觉*；模板优先则让模板成为内容真值，LLM 润色后凡出现模板/unit 里没有的断言一律判越界、回退模板——把"综合幻觉"从演示时可能翻车降为结构上不可能。

```
core units → 模板渲染器 → baseline markdown(内容真值)
           → 可选 LLM 润色(prose 化) → 校验"无新增断言" → 通过则用, 否则回退模板 → final user.md
```

- **模板渲染器（地板，永远跑）**：按 type 分组，每条 unit 一行 bullet，结构稳定、零 LLM、可回退、不可幻觉。
- **LLM 润色（默认开，可降级）**：把模板输出润成连贯 prose。综合 prompt 严格约束：只能用提供的 units、不得新增信息、不得把短期状态夸成长期身份、不稳定内容用"近期/阶段性"表述、压在 `char_budget` 内、风格直接不煽情、证据不足宁可省略。

`renderer_version` 记进 `memory_views`，prompt/格式变更时全量置 stale 重综合一次。

### 3.5 与回复读路的衔接

综合产物仍写到 `user.md` / `soul_memories/<name>.md` 的**老路径、老格式**（带 `## 章节`，sensitivity 分级保留），**全量注入**不变。于是 `memory_review_service.read_*` 与回复读路读到的"形状"不变——**回复链路零改动**。区别只在：这篇文件现在是机器综合产物。

**生成文件头**（廉价卫生，声明它是产物、为未来冲突检测埋线）：综合时在文件头写

```markdown
<!-- generated_by=tracelog view_type=user_md editable=false
     source_unit_set_hash=sha256:... renderer_version=baseline-v1
     generated_at=... content_hash=sha256:... -->
```

并建议给每段埋 `<!-- tracelog:units=u1,u2 -->` provenance 锚点（region 级，非逐行锁），为第二步"回复出处标到具体 unit"埋线，零成本。MVP 不实现"用户改本地文件后回灌"的冲突流程（md 不可编辑，见 §4 与 §11）。

---

## 4. 用户编辑：MVP 不碰 md，编辑面落在 unit 工作台

**锁定原则：`user.md` 永不接受用户直接编辑（纯综合产物）；用户的一切编辑都以 unit 为对象，发生在工作台。** 从根上消掉"用户改 prose → 反解析成 unit ops"的 NL-diff 噩梦。要支持自由写一段话，也走"freeform = 退化 unit"（逐字渲染、user_authored、对账免疫）。

**MVP 范围与可裁剪度**：工作台两面——**glance**：渲染出的 md prose，给用户"系统怎么看我"的镜子；**manage**：units 一条条列出，可下钻 evidence、看 op 日志 diff。编辑（增删改）落在 manage 面；按"用户编辑可以再说"，**编辑深度可裁**：MVP 可先上只读 manage 面（展示 + diff + 下钻），把增/删/改作为紧随其后一步。§4.1/§4.2 两条硬规则在启用增/删后即为必需，设计先就位。

### 4.1 用户删除 = 墓碑，对账必须认（启用删除后必需，分支 E）

用户删一条 unit **不是** `DELETE`。落 `status='retracted_by_user'` + 写 tombstone，保留 evidence 连接与 op 日志。

防诈尸——支撑它的 raw 还在库里，下次对账会重新派生：

- **分支 E1（推荐）：prompt 级抑制。** 对账输入里带上本 scope 的 `retracted_by_user` claims（§2.4），明示"用户已否决，禁止再生成同义条目"。代价：软约束。
- **分支 E2：落库级去重守卫。** add op 落库前与 tombstone 做近似查重（`normalized_claim` / 实体重叠 / 第二步 embedding），命中则丢弃或转 `pending`。

> 推荐 E1 起步，叠一条**廉价硬规则**：若某 `add` 的 evidence 集合 ⊆ 某 tombstoned unit 的 evidence 集合，直接抑制。E2 的 embedding 查重等第二步 unit 进向量库后补上。

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
- *（编辑启用后）* `create_unit`（user_authored，可选 tier/profile_policy）/ `update_unit`（reflected→新建 user_authored + supersede 旧）/ `delete_unit`（retracted_by_user 墓碑）；改到 core 子集则置 view stale。

---

## 5. 迁移：现有 md → units（分支 G，含报告细化）

老库里 `user.md` / `soul_memories/<name>.md` 已有内容。翻转前要变成 units，但**不直接全部写成 active**：

1. 读旧 md，按章节切分（身份/偏好/项目/关系/阶段背景）。
2. LLM 抽候选 units：`source='migrated'`、**`status='pending'`**、**`confidence` 初始 ≤ 0.75**、尽力回填 evidence 或留空。
3. 生成 preview：候选 unit 列表 + 由这些 unit 合成的新 md + 与旧 md 的并排 diff。
4. **人工确认**（accept / reject / merge 去重），确认后转 active；旧 md 留备份。

**迁移验收线**：身份、长期目标、核心偏好、当前核心项目四类不得缺失；新 md 不得出现旧 md 没有、units 不支持的断言；合成 `user.md` 不超过旧版长度 +20%；候选 units 里"单条 raw 转写型垃圾"≤ 15%。

> 这里的人工确认用迁移脚本里的一步实现即可，**不必**上 §11 的 `memory_reviews` 队列。分支 G2（不回填、起于空、旧 md 冻结前置）作为回填质量不达标时的降级。

---

## 6. shadow 验证窗口 vs 直接翻转（分支 C）

"md 从深反思手写翻转为 core unit 综合"是 MVP 风险最高的子项。推荐**折中（短 shadow + go/no-go）**：

- 先双写 **2–3 个真实整理周期**：保留旧 patch→md 路径继续产出旧 md，并行产出 `user.synth.md`。
- 每轮并排比较四项：信息是否丢失、是否幻觉、长度是否超预算、回复质量是否退化。
- 全绿过验收门（§8）→ 翻转，旧 md 留副本可回滚。

shadow 期以"天"计而非"周"，迁就答辩档期；比一刀切稳。

---

## 7. 终态读路与本期必须预留的接口缝

MVP 读路不动，但为第二步"md 全量 + 检索 unit + 顺藤 raw"留好缝，否则二次返工要动 schema 与读路。终态读路图：

```
md 全量注入(core 画像)
  + 检索 unit(非 core, 按 query 召回, 带 confidence/type/自何时)
  + raw(① unit.evidence_ids 顺藤摸到  ② hybrid_search 直达孤儿话题)
  + 接缝 raw(自 cursor 起、尚未对账成 unit 的近期原文)
```

下列缝**在 MVP 就落地**，终态才用起来：

1. **`memory_unit_evidence` + `idx_unit_evidence_eid` + `relation` 列**（§1.2）：unit→raw 的 join、去重、支持/反驳区分。
2. **`in_md_slice` / `tier`**（§1.1 / §3.2）：core（进 md）与非 core（靠检索）的分界；终态据它对检索结果去重。
3. **`memory_views` / `memory_view_units` + provenance 锚点**（§1.4 / §3.5）：终态"回复出处标到具体 unit"。
4. **`context_builder` 里放 `retrieve_units(query, scopes, k=8, exclude_in_md=True, hydrate_raw=False)` 空壳**（MVP 返回 `[]`）：第二步检索 unit 的唯一插入点。打分公式（多信号）见 §11，留作 v2.1。
5. **unit 的 `confidence`/`type`/`status`/`scope`/`importance` 齐备**（§1.1）：终态排序、过滤、排除墓碑。
6. **接缝 cursor 复用现成的**（`soul_thread_deep_cursor`、全局 `reflections.scope_end`）：MVP 不用，别动其语义。

冲突一致性（终态）由愿景文档第 4.4 节 precedence（事实信 raw、框架用 unit、冲突以新为准）处理；本设计单向 unit→md + raw 始终是 unit 上游证据，与之兼容。

---

## 8. 验收门与演示脚本

### 8.1 验收门（翻转前必须全绿）

- **结构正确性**：schema 初始化成功、老 DB 可补表；CHECK 生效；`memory_views` fresh/stale 切换正确；hash 变化触发重综合。
- **记忆质量**：抽样 50 条 reflected unit，**跨证据抽象 ≥ 70%**，"逐帖复述"≤ 15%；合成 md 在预算内；合成内容无 unit 不支持的新增断言。
- **用户控制**（启用编辑后）：user_authored 不被对账自动改写；删除后同 evidence 不诈尸；置 `force_exclude` 后不进画像。
- **重综合低频**：埋点——纯 confirm / core 外 op **零重综合**，重综合次数 ≪ 对账 op 次数。
- **回复不回归**：翻转前后同组 post 跑回复，质量无明显下降（读路未变，兜底回归）。

### 8.2 演示脚本（冲刺答辩用，5 幕）

1. **看到画像**：打开工作台 glance 面，展示 `user.md` + manage 面 units 列表。
2. **看到出处**：点开一条 unit，下钻其 raw evidence。
3. **用户纠正系统**：把一条 reflected unit 改成 user_authored，触发新 md 综合，画像随之更新。
4. **删除不诈尸**：删一条记忆 → 跑一次整理 → 证明它不复活。
5. **回复效果**：用更新后的画像生成回复，体现"懂你"但不乱编。

---

## 9. 落地顺序（建议）

1. 建表（§1：units / evidence / ops / views / view_units）进 schema.sql + 单测建表/约束。
2. `memory_unit_service`：先 list/get/op 日志（只读 manage 面）跑通展示与 diff；增删改与 §4 墓碑/免疫作为紧随一步。
3. 综合器（§3.4 模板优先 + 可选润色）+ core 子集 selector/滞回（§3.2）+ `memory_views` hash 失效（§3.3），先对"手造 units"综合，验产物形状与老 md 一致、预算达标。
4. 改 `reflection_router` 深反思 schema：文本 patch → unit ops；`reflector` apply 步改写为 §2 op 落库（保留 gating；对账输入用 A1 + signals + tombstones）。
5. 预留缝（§7）随手落地：evidence relation、`tier`/`importance`、provenance 锚点与文件头、`retrieve_units` 空壳、cursor 不动。
6. 迁移脚本（§5）+ shadow 跑（§6）+ 验收门（§8）。
7. 翻转：删旧 patch→md 路径，md 改由综合产出。工作台 ReflectionsPage 接 op 日志 diff。

读路改造、decay/重组、unit 进向量库——第二/三步，不在此清单。

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
| 渲染 | **模板优先（真值+校验）+ LLM 润色（不得新增断言闸门）** |
| 重综合触发 | `memory_views.source_unit_set_hash` 失效驱动，可异步 |
| 对账输入 | raw + light signals + 全部 active units(A1) + tombstones |

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
- **隐私全分类 `visibility`(no_prompt/private/restricted) + `privacy_level`(normal/private/sensitive)**：绑定"不再提起 / 彻底删除"删除 UX（v2.2）。MVP 删除用 `retracted_by_user` + tombstone 即可，不拆这两列。
- **raw 证据删除流程 + `memory_audit_logs` 审计表 + scrub 为 `[deleted_by_user]`**：高风险、影响审计与 unit 完整性，属隐私治理（v2.2）。
- **`retrieve_units` 多信号打分**（semantic+BM25+entity+time+importance+source_bias）**与读时 precedence 具体权重**：v2.1 读路。MVP 只留空壳与方向。
- **本地文件编辑的 hash 冲突三向 diff 回灌**：md 在 MVP 不可被用户编辑，仅写生成文件头（§3.5），不做冲突解决流。
- **`user_locked` 独立列**：MVP 由 `source='user_authored'` 推导。
- **`normalized_claim` 的实际填充与 embedding 级查重**：列占位，等 E2 / 第二步向量库再启用。

> 取舍原则：报告把"完整 v2"画全了，MVP 只切"写端单点赌注"那一刀。**预留 schema 形状 ≠ 现在就建流程。**
