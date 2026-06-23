# 人格记忆 / 相处记忆：公开互动 + 私聊 设计定稿

> 状态：**定稿（决策于 2026-06-23 闭环）** · 关联：`docs/memory-v2-architecture.md`
> 本文经多轮 grill 收敛；最终方案 = **路线 A**（不是早期草案推荐的路线 B，原因见 §4）。

---

## 1. 产品模型：真实社交圈的折中

目标是模仿真实人类社交圈。完全仿真 = 每个人格一套完全独立的记忆，但那样重复且不合理，所以折中成两类记忆：

- **主记忆（global/public）**：公开对话（帖子 + 公开评论）里抽取的、关于**用户本人**的事实。即"社交圈里大家都知道的你"。任何人格都能用。= 用户画像。
- **人格记忆（owner=soul:X）**：一切由"用户 ↔ X 互动"产生的记忆，**横跨两个可见层**：
  - 公开层 `visibility=public`：用户在 X 评论区与 X 的互动。
  - 私密层 `visibility=private:soul:X`：用户与 X 的私聊。
  - 桶里的 unit 是**公开基线之上的增量**：私聊里额外聊到的、与 X 的专属动态/称呼/约定，或 X 对用户的风格化解读。

**核心映射**：「一个人格一个记忆桶」= **owner 层 = 人格**；public/private 只是桶内子层，**只管"这条能多自由地说"（discretion + 铁律），不拆散"这是 X 的记忆"这个整体**。

---

## 2. 现状问题：公开互动的关系信号被丢弃

一次「用户在 X 评论区与 X 互动」混着两种**不同主语**的信号：

| 信号 | 主语 | 应去的家 | 现状 |
|---|---|---|---|
| 用户事实（"我在备考考研"） | 用户本人 | `global/public` → 主记忆，跨人格共享 | ✅ 正确 |
| 相处纹理（称呼、节奏、默契、边界、X 对用户的理解） | 用户 ↔ X | X 的人格记忆 | ❌ **被丢弃** |

根因：分桶重构（`record_comment_mutation`，`core/memory_events_service.py:199`）把公开评论**整体**路由进 `global/public`，且其 reconcile 只抽"关于用户本人"的信念。理由"评论里是用户事实不是关系"对了一半，把关系那半也扔了。

**残留证据（说明这是被砍断的设计，不是新需求）**
- `validate_boundary`（`core/memory_unit_service.py:51-52`）**已显式放行 `(soul:X, public)` 桶**，注释："public may be owned by global (user) or a soul (its public beliefs)"。
- `memory_view_producer._format_units`（`:24`）已在给相处记忆 unit 标 `场景=公开评论/私聊`。
- `soul_relationship_memory.souls_needing_view`（`:192`）至今在查 `visibility LIKE 'thread:%'` 的 relationship unit——它还在等公开互动产生的关系单元。

---

## 3. 脊柱（贯穿所有决策的不变量）

> **owner 管「归谁用」；visibility 管「能多自由地说」；敏感度只能单向流动。**

- **铁律（不可协商）**：**更敏感的证据永远不能挂到更不敏感的单元上**（private 证据 ❌ 进 public 单元；public 证据 ✅ 可进 private 单元）。防止"私聊内容借一条公开单元泄露"。
- **discretion 复用现成**：`memory_scope_policy.classify`（`core/memory_scope_policy.py:58`）：public → HARD（自由引用），自己的 private → 公开场景 SOFT（提及前自判）。

---

## 4. 实现路线：A（定稿）

**评论落库时扇出两条事件**，各进自己的真桶：

```
评论 create/edit/delete/rerun（在 record_comment_mutation 单一 chokepoint 扇出）
  ├─ comment_message      → (global, public)        用户事实镜头（现状不变）
  └─ comment_relationship → (soul:X, public)        关系镜头（新增）
```

- `(soul:X, public)` 是一个**普通真桶**：自带事件、游标、units，`buckets_with_pending_events` / `reconcile_bucket` / `challenge_units_for_source` 全部**原样工作**。
- **铁律保持绝对**：关系单元的证据就在自己桶里，`_assert_events_in_boundary`（`core/memory_unit_service.py:111`）**一行不用动**。
- **贴合现有 grain**：单元 `owner=soul:X`，相处叙事本就按 owner 查；challenge 跨"两个 source_type"对称传播（编辑评论 → 两条 edit 事件 → 分别 challenge 主记忆单元和关系单元，各自 in-bucket）。

**为什么否决路线 B（单事件多归属、放宽同桶不变量）**
B 的唯一战略理由是"它是跨 bucket 合并的钥匙"。但经 grill 厘清：**用户要的 P2 是"桶内深反思（去重/矛盾处理）"，发生在单桶内、不需要跨桶搬证据**；而隐私决策又让朴素的跨 bucket 单元合并基本非法。**跨 bucket 合并需求不存在 → B 放宽核心安全不变量的代价成了纯亏。** 故采用 A。

**路线 A 的代价（均不碰核心不变量）**
- 评论事件量 ~2×（append-only，便宜）。
- 每次评论 mutation 必须在 `record_comment_mutation` **结构性地同时扇出两条事件**（用一个函数发两条，杜绝漏发导致关系桶 desync）。
- 每个 soul 的 public 桶多一次 reconcile LLM pass。

**需要的 enum/schema 改动**
- `memory_ingest_events.source_type` 的 CHECK（`schema.sql:368`）与 `SOURCE_TYPES` 常量新增 `'comment_relationship'`（`source_channel` 仍是 `'comment'`）。独立 source_type 让其 `source_revision` 序列与 `comment_message` 互不干扰（`_next_revision` 按 `(source_type, source_id)` 取序，避免 `UNIQUE(source_type,source_id,source_revision)` 冲突）。

---

## 5. 写路径

### 两个镜头 + 反事实测试 + 增量闸

两个镜头读各自 source_type 的证据，用同一句判据划界，杜绝双抽：

> **反事实测试**：「把对象换成另一个 AI 好友，这条还成立、还有用吗？成立 → 关于用户本人 → 主记忆 global/public；只在和 X 这段关系里才有意义 → 人格记忆 soul:X。」

| 信息 | 反事实 | 归属 |
|---|---|---|
| "用户喜欢科幻" / "在备考考研" | 换谁都成立 | 主记忆 |
| **沟通风格（"话少点/别用 emoji"）** | 换谁基本都成立（用户性格） | **主记忆（默认）** |
| "管 X 叫'小迹'" / "和 X 习惯互怼" / "和 X 约好每晚互道晚安" | 只对 X 成立 | 人格记忆 soul:X |

- **增量闸（决策 2 定稿）**：人格 unit 允许和主记忆 unit **重叠**，但**仅当它在公开基线之上添加了 X 专属增量**（私聊细节 / 与 X 的专属动态 / X 的解读）才允许存在；纯复述公开事实、零增量 → 不建（那是主记忆的活）。
- 两个镜头 prompt 加**对称**的"别越界"指令：用户事实镜头明确"soul 专属称呼/约定不是用户事实，跳过"；关系镜头明确"用户客观事实交给主记忆，只抽 X 专属增量"。
- 关系镜头同样记两种 role、**只挖 user 发言为证据、X 回复作上下文**（与现有 reconcile 一致）。

### 风格化思考（X 对用户的解读）

整个 memory-v2 立在不变量上：**unit 是有证据链的信念，view 是 evidence→unit→view 单向 DAG、无独立真值、不漂移。** 解读没有事实证据链，三条出路，**定稿先走 (I)，(II) 留作下一步**：

- **(I) 读时风格化（P1 采用）**：不存解读。人格桶只存**有证据**的关系事实；"风格化"发生在 **X 说话时**——人设(system prompt)读取"主画像 + 关系记忆"时自然染色。不持久化，但人设稳定 → 每次染色一致。**架构纯净。**
- **(II) 沉淀"X 亲口说过的解读"（后续）**：把 X 在对话里**真说出口**的对用户的看法存成 unit，**证据 = X 那次发言**（仍 evidence-grounded、可被 X 日后自我矛盾 challenge）。代价：要为人格桶**放宽"assistant 消息不能成为证据"**（仅限"X 对用户的看法"，不含 X 自我设定）。
- **(III) 系统替 X 脑补从没说过的解读**：最拟人但脱离证据、challenge/relink 失效。**不做。**

---

## 6. 读 / 合成路径

- `relationship_units_for_soul`（`core/soul_relationship_memory.py:79`）：`visibility = private:soul:X` 放宽为 `visibility IN ('public', 'private:soul:X')`（owner 仍限 `soul:X`、type 仍 `relationship`）。
- `souls_needing_view`（`:192`）：死掉的 `thread:%` 查询替换为 `visibility='public'`。
- **相处叙事 = 合成 主画像 + X 公开关系 unit + X 私密关系 unit 成一段**——用户在 P1 阶段**就能看到"结合后的相处记忆"**。`_format_units` 的"场景=公开评论/私聊"标注无需改。
- 注入：`relationship_memory_for`（`core/memory_read.py:51`）+ `PUBLIC_USE_RULE` 已处理"公开提及私聊内容前自判"，零改动。

### 关系单元归谁用（决策 3 定稿）——必须主动堵的漏洞
当前通用检索 `_allowed_visibility_sql`（`core/memory_read.py:117`）**只按 visibility 过滤、不看 owner**，且只排除 `state`（`_RETRIEVE_EXCLUDED_TYPES`，`:69`）。**A 一旦产出 `(soul:X, public, relationship)` 单元，soul Y 的通用检索会把"用户和 X 的相处默契"也捞走。**

- **改法**：把 `relationship` 加进 `_RETRIEVE_EXCLUDED_TYPES`。关系类单元**只经其 owner 的相处叙事注入，不走跨人格通用检索**；owner 由此成为关系信念的访问边界（即便 visibility=public）。
- **产品语义**：公开互动**发生过**是公开可见的事实；但由此提炼的**相处默契只属于 X**，别的人格不据此行动。

---

## 7. P1 / P2 分工（关键简化）

**「结合 / 合并」放在哪一层做**，决定了 P1 改动大小。定稿：**不让快速 reconcile 变成 owner 级。**

- **P1 — 快速 reconcile 保持 per-(owner,visibility) 不变**：`(soul:X,public)` 与 `(soul:X,private)` 各自 reconcile、各自产出分层关系 unit。每个 pass 只看一个可见层 → **私聊证据物理上不可能喂到公开 unit，铁律零成本守住**。P1 对现有循环几乎零改，只多"`comment_relationship` 事件 + 关系镜头 prompt"。"结合"在**合成层**呈现（§6）。
- **P2 — owner 级深反思（桶内巩固升级版）**：一次看 X 的 public+private 全部 unit，做去重、矛盾处理、晋升/衰减、**以及跨层合并成私密 unit（决策 1A）**。合并方向永远"朝更私密"（私密 unit 可持公开证据，铁律允许）。这是"合并"这种写操作该待的地方——需要全局视角 + 小心处理铁律——而不是塞进逐事件的快速 reconcile。

> 注：合并成私密 unit 后，那条的公开部分也走 discretion，**但不构成损失**——主画像独立持有那条公开用户事实（HARD），X 仍能公开自由引用；人格记忆里的是**增强版**。

---

## 8. 已定决策清单（2026-06-23 闭环）

1. **路线 A**（双事件扇出），否决路线 B；`_assert_events_in_boundary` 铁律保持绝对、零放宽。
2. **人格 = owner=soul:X**，横跨 public/private；reconcile/约束按"owner 聚合、visibility 约束"。
3. **铁律**：更敏感证据不得挂到更不敏感单元。
4. **决策 2 → 增量闸**：人格 unit 可与主记忆重叠，但须有 X 专属增量；沟通风格默认归主记忆。
5. **决策 1A → 允许人格内跨层合并成私密 unit**，且为 **P2** 深反思的操作（P1 不碰）。
6. **决策 3 → 关系类单元排除出跨人格通用检索**，owner 成访问边界。
7. **风格化 → P1 走 (I) 读时染色**；(II) 沉淀"X 说过的解读"留作后续。
8. **P1/P2 分工**：P1 产出分层 unit + 合成层结合；P2 owner 级深反思做合并/去重/矛盾/晋升衰减。

---

## 9. 兼容与清理

- **死路径清理**：`thread:<post_id>` 桶与 `describe_scene` 的 thread 分支已死（评论早改走 global/public）。本设计**不**复活 thread 桶（公开关系来源用 `(soul:X, public)` 单桶，避免按帖子碎片化），并清理 `souls_needing_view` 的 `thread:%` 查询。
- **双事件 sync**：扇出必须在 `record_comment_mutation` 单 chokepoint 结构性完成（一个函数发两条），create/edit/delete/rerun 全覆盖，否则关系桶 desync。
- **存量数据**：历史公开评论只在 global/public 留痕；是否回填 `comment_relationship` 事件 = 一次性 backfill（可选，非阻塞）。

---

## 10. 风险 / falsifier

**风险**
- 双事件 sync 漏发 → 关系桶 desync。靠单 chokepoint + 结构性同发 + 测试钉死。
- 两个镜头职责划不清 → 双重抽取。靠反事实测试 + 增量闸 + 对称"别越界"指令防住。

**falsifier（何时该收缩范围）**
- 若真实使用里公开评论极少、关系信号几乎全来自私聊 → 路线 A 的公开层收益有限，优先只做 §11 的 P0 选择器解耦即可。

---

## 11. 分阶段落地

- **P0（独立快赢，先发）**：相处记忆选择器与画像三阈值 `passes_core_predicate`（`core/memory_view_service.py:59`，`tier=core ∧ conf≥0.82 ∧ imp≥0.70`）**解耦**——给相处记忆一套自己的入选规则（纳入 `type=relationship` 的 contextual，按 importance×recency 排序、条数/字数预算封顶）。不依赖任何写路径改动，**立刻让人格视图有内容**。（依据：真实库 11 条 unit 仅 2 条 core，一条 `imp=0.70/conf=0.95` 的 preference 仅因 `tier≠core` 出局。）
- **P1（本设计主体）**：路线 A 双事件 + `comment_relationship` source_type + 关系镜头 prompt（反事实测试 + 增量闸）+ §6 读侧聚合与 relationship 检索排除 + §9 清理。风格化走 (I)。
- **P2（深反思）**：owner 级桶内深反思——去重、矛盾处理、跨层合并成私密 unit（1A）、contextual→core 晋升与衰减；同时解决"只增不减"。
- **后续**：风格化 (II)（沉淀 X 说过的解读，放宽人格桶的 assistant-as-evidence）；记忆图谱（结构化分析，已预留）。

---

## 12. 实现状态（2026-06-23 已落地）

| 阶段 | 状态 | 关键文件 |
|---|---|---|
| **P0** 选择器解耦 | ✅ 已实现 + 测试 | `core/soul_relationship_memory.py`（`_passes_relationship_predicate` / `REL_*`） |
| **P1** 路线 A 双来源 | ✅ 已实现 + 测试 | `memory_events_service`（双发 `comment_relationship`）、`memory_unit_service`（challenge 分组）、`memory_reconcile_producer`（关系镜头）、`soul_relationship_memory`（读侧聚合）、`memory_read`（检索排除 + freshness 跳过） |
| **P2a** 确定性深反思 | ✅ 已实现 + 测试 | `core/memory_reflection.py`（`decay_dormant` / `promote_core` / `reflect_persona`）、`memory_unit_service`（`decay_unit` / `promote_unit_tier` / `count_confirm_ops`） |
| **P2b** LLM 巩固 seam | ✅ 已实现 + 测试 | `memory_reflection.consolidate_persona`、`memory_unit_service`（`supersede_unit` 放宽 + `visibility_rank`）、`memory_router`（巩固 prompt）、`memory_reconcile_producer`（producer 桥接） |

全套测试通过（521）。两条 source_type 的双发、challenge 跨镜头传播、铁律（更敏感证据/信念不入更不私密 unit）均有用例覆盖。

**唯一尚未接线**：深反思（`reflect_persona` / `consolidate_persona`）目前是**按需可调用**的引擎，**还没接入周期/后台触发**——何时跑（cron / 空闲触发 / 写计数阈值）是运维节奏决策，留待确定。接线点：仿照 `memory_reconcile_runner` 在 job 流程里加一个低频 reflection job。
