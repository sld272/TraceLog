# TraceLog 记忆 v2 — 当前状态块 · goaltool · 统一建议机制 设计

> 状态：设计定稿（feat/memory-v2 分支），实现前。本文承接 [memory-v2-mvp-design.md](./memory-v2-mvp-design.md)，把三件在 MVP 迭代中浮现、相互咬合的设计钉死：①短期状态的"当前状态与关注"always-on 块；② goaltool（目标管理）；③统一的"建议机制"（系统提议、用户确立）。
>
> 这三者由一次真实 dry-run 暴露的问题驱动：短期状态（"这周很累""期末压力大"）该不该、怎样进上下文；以及 goal 这种"带承诺"的东西不能像被动信念那样被系统自动塞进用户的清单。
>
> 相关：[memory-v2-design.md](./memory-v2-design.md)、[memory-v2-mvp-design.md](./memory-v2-mvp-design.md)、[architecture.md](./architecture.md)。

---

## 0. 一句话主张

把"用户是谁"（稳定身份）和"用户现在怎样、在忙什么"（短暂状态/关注）拆成**两条独立通道**：身份走低频综合的 `user.md`，当前状态走一个读时即时装配、永远注入、会自动过期的小块。同时引入 **goaltool** 承载"目标"这一介于 todo 与被动信念之间的层，并把"目标/待办进入用户清单"统一收敛到一条原则——**系统只提议，用户在对话当下确立**。

---

## 1. 四条线的分工（先把边界钉死）

系统里关于"用户"的结构化信息分四类，**各有各的家、各有各的判定松紧与生命周期**，互不越界：

| 线 | 内容 | 来源/判定 | 归属 | 生命周期 |
| --- | --- | --- | --- | --- |
| 待办 todo | 具体、常带日期的任务（"周三交作业"） | 系统建议 + **对话当下确认** | todotool | 完成/过期 |
| 目标 goal | 短期 + 长期目标（"这学期提 GPA""跨专业考研"） | 系统建议 + **对话当下确认** | goaltool | status 驱动（完成/放弃） |
| 当前状态 state | 短暂状态/情绪/处境（"这周很累"） | reconcile **被动抽取** | 记忆单元 `type=state` | 7 天硬过期 + supersede |
| 身份/偏好/关系/洞察 | 稳定的"用户是谁" | reconcile 被动抽取 | 记忆单元 → `user.md` | 慢衰减（Phase 3） |

判定松紧的核心区别：**被动信念**（state / 身份…）抽松一点无妨——它是"系统的印象"，会衰减、不以"你的清单"示人；**用户清单**（todo / goal）带承诺感，读起来是"我的"，所以**任何东西未经用户确认绝不进 active**。

---

## 2. 当前状态与关注块

### 2.1 定位：独立 always-on 通道，不焊进 user.md

v1 的"当前状态与关注"是一坨手写、整篇重写的 md blob：能 always-on、能快迭代，但没出处、不自动过期、和稳定画像耦合重写。

v2 **不能**把短期状态塞进 `user.md`：`user.md` 是 core unit 的低频综合、靠 `source_unit_set_hash` 失效驱动重综合；短期状态天天变 → hash 天天变 → 每天重调 LLM 综合整篇画像，把"低频"优势打没。

所以：**身份画像（稳定、慢、预综合持久化）与当前状态块（短暂、快、读时即时）是两条通道。** 保留 v1 的体验（always-on + 快迭代），但实现成独立轻量块。

### 2.2 两个来源

| 块内分区 | 数据来源 | 谁管 |
| --- | --- | --- |
| 当前**状态** | `type=state` 记忆单元（被动抽取） | reconcile |
| 当前**关注** | **goaltool 里"用户确认的 active 短期目标"的只读投影** | goaltool |

"当前关注"**不是独立存储**，而是对 goaltool 的筛选视图（status=active、horizon=short、近期在推进）。单一真相源是 goaltool，块只"开窗看一眼"，所以不存在两处存目标对不上的冲突。

### 2.3 预算与装配

- **≤ 5 条**（状态 + 关注合计），按 recency × importance 取 top-K，宁少勿杂。
- **读时即时装配**：读路里一条便宜 SQL（近窗口内 active state 单元 + active 短期目标）+ 模板渲染，**零 LLM**。每条回复重新算，永远最新，没有 stale view 要管。
  - 与 `user.md` 的对比：身份画像预综合持久化（为稳定性/省钱）；当前状态块读时即算（为新鲜，且天然无 stale）。
- **注入位置**：作为独立 prompt section，与读路其它层并列：

```
[基线认知]  ← user.md 稳定画像（慢，低频综合）
[当前状态]  ← 近期 state 单元 + active 短期目标（快，读时即时）← 本块
[相关记忆]  ← 按当前话题检索的 unit
[最近动态]  ← freshness seam：尚未对账的原始新事件（逐字）
```

与 freshness seam 互补不重复：seam 是**未消化的原始事件**（分钟/小时级、逐字），本块是**已抽象的近期状态/关注**（天/周级、概括）。

### 2.4 过期：硬上限是兜底，不是主路径

- **状态**：7 天硬过期；**关注（短期目标）**：30 天没推进则移出块（目标本体留在 goaltool）。
- 关键语义：窗口是 **GC 天花板**。正常情况下一条状态在窗口内早被新证据 supersede/retract（或目标被改 status）淘汰；窗口只保证"万一漏了也绝不赖过 7/30 天"。
- 实现为**惰性 GC**：读路/对账时顺手把超窗口的 state 单元标 `dormant`，不另设定时任务。窗口期按 `type` 从 `last_confirmed` 推算，不新增列。
- 与 Phase 3 全局 decay 的关系：本块的时间窗负责"进不进 always-on"（轻、即时）；`dormant` 的长期清理是 Phase 3 第三档（重、低频）。本块不依赖 decay 先做好。

### 2.5 边界：与读侧 scope policy 配合

本块也走 [`memory_scope_policy`](../core/memory_scope_policy.py)：
- **公开回复**：注入 global 公开的当前状态；该人格自己私聊得知的状态 `soft` 准入（能调出但自判断能否当众说）。
- **私聊**：global 当前状态 + 该人格私聊的当前状态。
- 别的人格的私聊状态：永远 `forbidden`。

---

## 3. goaltool：补上 todo 与记忆之间的层

### 3.1 定位

系统原本缺一层：todotool 管"具体任务"，记忆单元管"被动信念"，中间的"目标"无家可归。goaltool 管**短期 + 长期目标**，主动管理（系统建议 + 用户编辑），形成自然层级：**长期目标 → 拆短期目标 → 落具体 todo**。

### 3.2 目标是"用户承诺"，不是被动信念

- **作为被追踪的目标对象** → 只活在 goaltool，且**只有用户确认的才 active**。
- reconcile **不再产 `type=goal` 单元**（避免"伪装成已确立目标"的被动条目）。
- **愿望/倾向不丢**：用户随口的"想做游戏开发"仍可作为 `preference/insight` 单元被动留在记忆里（低调、会衰减）。它停在"印象"层，不被冒充成"承诺的目标"。
- 两者不冲突，因为是**不同承诺级别**：一个是"系统觉得你似乎想…"，一个是"用户确立的目标"。

### 3.3 短/长期与注入

- **长期目标** always-on 注入（类比 todos 现在单独注入）；`user.md` 不再承担"列目标"职责，专注稳定特质/偏好/关系。
- **短期目标**：active 的进"当前关注"块（§2.2）。
- **迁移**：goaltool 上线时，把现有 `type=goal` 单元（如"跨专业考研"）迁移成 goaltool 长期/短期目标；记忆库只保留"用户正在为某目标努力"这种关系/状态痕迹。

### 3.4 schema 草案（设计，未实现）

```sql
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,            -- g_<ulid>
    title       TEXT NOT NULL,
    detail      TEXT,
    horizon     TEXT NOT NULL CHECK(horizon IN ('short','long')),
    status      TEXT NOT NULL DEFAULT 'active'
                  CHECK(status IN ('active','done','abandoned','paused')),
    source      TEXT NOT NULL DEFAULT 'user'  -- 'user' | 'suggested_accepted'
                  CHECK(source IN ('user','suggested_accepted')),
    focus       INTEGER NOT NULL DEFAULT 0,   -- 是否"当前关注"
    last_progress_at REAL,                     -- 最近推进时间（喂 30 天关注窗口）
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
```

### 3.5 MVP 范围

首版：LLM 建议 + 用户对话当下确认 + 用户编辑 + status + 短/长期分类 + long-term always-on 注入 + 现有 goal 单元迁移。**不做** goal↔todo 关联（留后续）。

---

## 4. 统一建议机制（suggestions）

### 4.1 原则

**凡是会进入"用户自己的清单"的东西，系统只提议，用户来确立。** 适用于 todo、goal，未来可扩展到记忆单元复核。做成**一套共享基建**，不要每个工具各搞一套。

### 4.2 共享存储（schema 草案）

```sql
CREATE TABLE IF NOT EXISTS suggestions (
    id           TEXT PRIMARY KEY,            -- s_<ulid>
    kind         TEXT NOT NULL CHECK(kind IN ('todo','goal')),  -- 预留扩展
    payload_json TEXT NOT NULL,               -- 拟创建对象的内容（含 deadline/horizon 等）
    evidence_ref TEXT,                         -- 来源（post/comment/chat 或 evidence event）
    confidence   REAL NOT NULL DEFAULT 0.6,
    status       TEXT NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','accepted','dismissed')),
    normalized_key TEXT,                       -- 防诈尸去重键（归一化内容+来源）
    created_at   REAL NOT NULL,
    decided_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_suggestions_kind_status ON suggestions(kind, status, id);
```

采纳后由对应工具在自己的 active 存储里建对象（todos / goals），并回填 `status='accepted'`。

### 4.3 确认时机：**todo 与 goal 统一在对话当下确认**（已定）

- 系统在对话中发现候选 todo/goal 时，**当场提示**用户确认（"你提到周三要交作业 / 想长期做游戏开发，要记进待办/目标吗？"），而不是默默丢进托盘等用户事后发现。
  - 这条尤其对带 deadline 的 todo 重要：纯异步托盘有"deadline 已过用户才看到"的风险，对话当下确认规避之。
- **工作台托盘**仍保留，作为**复核/兜底**：展示历史 pending、可补确认、可手动新增；但主确认路径在对话当下。

### 4.4 防诈尸

被 `dismissed` 的建议留**墓碑**（按 `normalized_key`），抑制系统反复拿同一证据再建议同样的东西。复用记忆单元那边 false/outdated tombstone 的同一模式，体验一致。

### 4.5 对 todotool 是行为变更

把建议机制应用到 todotool 是**改一个已在跑的功能**：todo 从"自动出现"变成"建议 → 对话当下确认"。这是有意变更，落地时做成**可切换（开关）**，别默默改掉用户已习惯的行为。goaltool 是新功能，无此包袱。

### 4.6 终局：与 memory_reviews 收敛

记忆单元自身的复核（[memory-v2-mvp-design.md](./memory-v2-mvp-design.md) §11 推迟的 `memory_reviews` 队列——挑战一条信念、确认迁移候选）是**同一个模式**。现在把建议基建做扎实，将来 todo / goal / 记忆复核都收敛到**同一个"系统提议、用户处置"面**，用户只需养成"瞄一眼、采纳或划掉"的单一习惯。

---

## 5. 工作台面板

- **当前状态与关注**：只读小块，展示 ≤5 条（状态 + 关注），点条目下钻到证据 / 对应 goal。
- **goaltool 管理面**：增删改长短期目标、调 status（像 todos 管理面）。
- **建议托盘**：复核 pending 建议（采纳/忽略），兜底对话当下没确认的。

---

## 6. 落地排序（解耦，避免返工）

1. **当前状态块（状态部分）**：并入 Phase 5 读路——"无条件召回近期 state 单元 top-K + 7 天惰性硬过期"的小装配器 + 注入。独立可上，不依赖 goaltool。
2. **统一建议基建**：`suggestions` 表 + service + 对话当下确认流 + 防诈尸墓碑。
3. **goaltool**：schema + service + 建议抽取 prompt（接 §4）+ 工作台 + long-term always-on 注入 + 现有 goal 单元迁移。落地后"当前关注"作为投影插进状态块。
4. **todotool 接入建议机制**：behind flag，把现有"自动写 todo"改为"建议 → 对话当下确认"。

reconcile 去掉 `type=goal` 与 goaltool 上线同批做（避免空窗期目标无人抽）。

---

## 7. 决策状态

**已定**

| 议题 | 结论 |
| --- | --- |
| 短期状态归处 | 独立 always-on 块，不进 user.md；读时即时装配 |
| 状态块预算 | ≤ 5 条，分"状态 + 关注" |
| 过期窗口 | 状态 7 天 / 关注 30 天；为硬上限兜底，主路径是 supersede/status |
| 状态来源 | `type=state` 记忆单元（被动） |
| 关注来源 | goaltool 用户确认的 active 短期目标投影 |
| goaltool | 引入；管短 + 长期目标 |
| 目标 vs 记忆 | 目标=用户承诺，归 goaltool；reconcile 不再产 goal 单元；愿望可留为 preference/insight |
| 建议机制 | 统一共享基建，应用于 todo + goal |
| 确认时机 | **todo 与 goal 统一在对话当下确认**；工作台托盘做复核兜底 |
| 防诈尸 | dismissed 建议留墓碑，复用记忆 tombstone 模式 |
| todotool 接入 | 行为变更，behind flag |

**待定 / 推荐**

| 议题 | 推荐 |
| --- | --- |
| goal↔todo 关联 | MVP 不做 |
| 每工具"信任度"自动采纳档 | 可选，留后续（当前统一为对话当下确认） |

---

## 8. 不在本期

- goal↔todo 父子关联与进度滚动。
- 建议机制的"信任度自动采纳"分档。
- `memory_reviews` 统一复核面（与建议机制收敛是方向，非本期）。
- 当前状态块的多信号打分（MVP 用 recency × importance 简单排序）。
