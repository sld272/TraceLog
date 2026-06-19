# memory-v2 答辩上线实施清单

> 目标：7 月答辩必须真正启用 memory-v2，完成"三层记忆 + todo + goal 的稳定自动整理沉淀"、
> "读取时全量读 md 画像 + 索引 unit 并链接 raw evidence + 读取尚未进 unit 的 raw evidence"、
> "人格不串戏、各 SOUL 用各自记忆正常对话"、"前端记忆工作台"。
>
> 编排原则：按依赖顺序分模块，模块内按先后排。当前分支的根本问题是"读路已进热路径、写路与生命周期未闭环"，
> 因此优先闭合主干，再补边界与亮点。

## 实施进度（feat/memory-v2，未推送）

已完成并提交：

- 主干闭环：reconcile 失败语义三态(不丢证据)、reconcile 接后台 job(写模式 flag)、
  view stale→重综合生命周期、v2 读模式注入画像+兜底、reply 三入口替换 legacy soul_memory+归属。
- 读路：freshness seam(units_and_freshness 真正实现)、chat/comment 真实 query + 最低命中门。
- 健壮性：并发 reconcile 的 cursor CAS、人格不串戏边界回归测试。
- 可解释：unit→raw evidence hydration(unit_detail)。
- goal：list_goals 目标线呈现(生命周期走 unit active/retracted)。
- 检索：unit 向量 ANN 与关键词混合(语义召回 + 关键词精确，scope 仍由 SQL 候选保证，索引不可用时退化关键词)。
- 迁移：评论归属迁移清理 (global, thread:*) 孤立 units/cursors。
- 文档：权威 v2 架构文档 `docs/memory-v2-architecture.md` + README v2 章节。
- 两个开关均默认 legacy(零影响)：`MEMORY_V2_WRITE_MODE=reconcile`、`MEMORY_V2_READ_MODE=units|units_and_freshness`。

待做：前端记忆工作台 UI(18-20，后端 hydration/编辑基础已就绪)、答辩 demo 数据与脚本(25)、
goal 显式「达成 vs 放弃」区分。

## 记忆边界语义基线（产品定义，已与代码核对一致）

事件按 (owner_scope, visibility_scope) 分桶（`memory_events_service.py`）：

- 原贴：`owner=GLOBAL` / `visibility=public`（行 160）——用户的公开记忆。
- 评论：`owner=soul:<该 thread 的 soul>` / `visibility=thread:<post_id>`（行 191-195）——公开人格记忆。
- 私聊：`owner=soul:<name>` / `visibility=private:soul:<name>`（行 222-223）——私密记忆。

读取边界（产品意图，已是 `scope_policy` + `_allowed_visibility_sql` 的现有行为）：

- soul 在帖子/评论回复时，**可检索所有公开记忆，含别的人格的公开评论（跨帖、跨人格）**——这是 feature，不是越界。
- soul 可检索**自己**的私密记忆，但通过提示词自主判断公开场景下是否说出（`SOFT`/discretion，记忆照常进 prompt，不过滤、不硬墙）。
- 别的 soul 的私密记忆永不可读（SQL 只 admit `private_self`）。
- `owner_scope` 的正确用途是**归属标签**（"这是用户在帖 X 对 soul_B 说的"），**不是读取过滤条件**（私密隔离除外）。

> 勘误：审阅报告 P0-3"thread 折叠成全局公开"是基于旧设计文档（thread 仅当前帖、私密硬墙）评判的。
> 本产品已主动推翻那两条硬约束，故 P0-3 在本定义下**基本不适用**；不要据此去"收紧" thread / owner_scope。
> P0-4（legacy 整块 soul_memory 注入）仍需修，但定性为"绕过 discretion + 未用 unit/view 分层"，非跨人格串戏。

## 现状速记（基于代码核对）

- `core/memory_scope_policy.py`：边界模型（classify / admissible_visibility_filters）**已接入 read 层**
  （注释里 "nothing touches the hot path yet" 已过时）；边界基本正确，缺的是归属 hydration 与 legacy 替换。
- `core/memory_read.py`：retrieve 是 SQL 关键词原型；`units_and_freshness` mode 已暴露但行为等同 `units`（空壳）。
- `core/llm/reply_router.py`：post/comment/chat 三个回复入口仍无条件注入 legacy 整块 `soul.soul_memory`，
  绕过了 unit/view 分层与 discretion 自主判断。
- 写路未接：`public_post_pipeline.py` 仍 enqueue `RUN_LIGHT_REFLECTION` / `MAYBE_TRIGGER_GLOBAL_DEEP_REFLECTION`，
  reconcile runner 注明无人自动调用。
- view 生命周期未接：`mark_stale_if_changed` 生产路径零调用；读侧不看 view status。
- goal 基本空白：无独立 goal service，仅 unit type 残留。todo 有完整三件套。
- 工作台 `memory_review_service` 目前读写 legacy md，非 unit。

---

## 模块 0：前置（决定后面所有事的两个开关）

1. 定一个总 feature flag，把"写路用 reconcile / 读路用 v2 / 工作台编辑 unit"绑在同一个开关下，
   杜绝现在"读路 v2、写路 legacy"的半接通状态。
2. **产品决策已定（影响模块 C 实现）**："人格不串戏" = 公开记忆（原贴 + 所有评论）跨帖跨人格共享、
   只有私聊按 soul 隔离；自己的私密在公开场景靠提示词自主判断是否说出。与现有 scope_policy 一致，
   无需改边界，重点转为"归属信息"与"legacy 替换"（见模块 C）。

## 模块 A：自动沉淀（写路 + 三层 + todo + goal）

3. 把 reconcile 接进后台 job：新增 reconcile job 类型，帖子/评论/私聊写入后入队，
   替换 `RUN_LIGHT_REFLECTION` / `MAYBE_TRIGGER_GLOBAL_DEEP_REFLECTION`（public_post_pipeline.py:50-51），
   chat/comment 写路同样接上。
4. 修 LLM 失败语义：producer 返回 success/empty/failed 三态，**只有成功解析才推进 cursor**
   （producer.py:95 + reconciler.py:348），失败 evidence 留待重试；同步修正 `test_..._producer.py:111`
   那条把失败当空结果的断言。
5. 修删除/漏写，保证"每次 mutation 都有事件"：删除事件进对账（按剩余证据 retract/降置信）、
   post 级联删评论补 delete event、空文本附件评论/私聊也写事件。
6. 接通 view 生命周期：reconcile 提交后算受影响 view 并 `mark_stale_if_changed`，
   后台 job 自动重综合 stale/missing view —— "md 画像自动沉淀"的闭环。
7. goal 从零接通：补 goal 抽取（reconcile prompt/schema 已留 goal type）+ 存储/状态机
   （进行中/完成/放弃）+ 自动整理路径。**工作量最大、风险最高**，优先级仅次于读写主干。
8. todo 自动沉淀对齐 v2：确认 todo 走 evidence→整理路径，避免 todo 与 unit 两套事实。
9. 并发安全：reconcile 提交加 cursor 的 CAS/重检，防止两个 job 重复消费产生重复 unit。

## 模块 B：读路（全量 md + unit 索引 + evidence 链 + freshness seam）

10. 全量读画像 md：v2 模式下用 view 综合画像填 `[基线认知]` 通道，补缺失/stale 兜底
    （模板即时兜底或临时回退 legacy），避免"抑制了 user.md 但 view 未生成 → 身份基线整段消失"
    （context_builder.py:33）。
11. unit 索引做实：落实独立 unit 向量索引（现为 SQL 关键词原型 memory_read.py:152），
    加最低命中门（过滤 overlap==0），画像成员从硬排除（in_md_slice=0）改为降权。
12. unit → raw evidence 链接 + **归属信息 hydration**：检索出的 unit 能 hydrate 回依据的 evidence
    （unit 已存 event_ids）；并带上 `owner_scope` 归属与来源（哪个帖、哪个 thread、谁发的），
    让 soul 读到别的人格的公开评论时清楚"这是用户在帖 X 对 soul_B 说的"，能正确还原原贴+评论上下文，
    不会误以为是对自己说的。这是"人格不串戏"的关键，也是答辩"可解释、可追溯"的亮点。
13. **freshness seam（尚未进 unit 的 raw evidence）**：实现 `units_and_freshness` 真正语义 ——
    读 cursor 之后、未被 reconcile 消费的 raw evidence，按 event/age/token 三重预算注入，
    带 `freshness_truncated` 标记。当前最大空壳（memory_read.py:28），实现前不要对外暴露该 mode。

## 模块 C：人格不串戏

> 注意：边界本身**不需要再收紧**（公开跨帖跨人格共享是既定 feature，scope_policy + read SQL 已正确）。
> 本模块的工作是"归属 + legacy 替换 + discretion 落地"，不是加过滤。

14. 守住而非收紧边界：保持公开（public / thread:%）跨帖跨人格可读、私密只读 `private_self` 的现有行为；
    后续给 read 层做改造（向量索引、freshness seam）时，回归测试要确保**没有意外收紧公开、也没有放开别人的私密**。
    （撤销旧版本里"comment 只读当前 thread / 按 reply_soul 收紧 owner_scope"的设想——与产品意图相反。）
15. 移除 v2 回复里的 legacy 整块 soul_memory 注入：reply_router 三入口（reply_router.py:125/154/181）
    改为——公开场景用允许范围 unit（含别的人格的公开评论，带归属）+ 自己私密的 discretion 标记 unit，
    私聊用该 SOUL 自己的 private view。这才让 unit/view 分层与自主判断真正生效。
16. soft discretion 落地（提示词，非变量）：把"这是你和 TA 的私下对话，自己斟酌是否公开提"这类措辞
    在渲染层加到自己的私密记忆上，让 SOUL 自主判断；不要演变成持久化的 `is_private` 列或硬过滤。
17. 归属呈现：检索到的公开评论在 prompt 里明确标出"用户在帖 X 对 soul_B 说"（依赖模块 B 第 12 条的归属 hydration），
    避免 soul 把别人的对话当成对自己说的——这是"不串戏"在体验层的落点。
18. root comment rerun 也走 v2 memory section（comment_service.py:618 现直连 router 未带记忆），
    否则重答时人格记忆会突然消失。

## 模块 D：前端记忆工作台

19. 工作台后端从编辑 md 改为编辑 unit：`memory_review_service` 增加 unit 增/改/retract/置信调整 API，
    旧 user.md / soul_memory 编辑入口在 v2 下禁用或标只读，避免"保存成功但回复不读"。
20. 工作台展示三层：画像（view）→ 支撑 unit → unit 链接的 raw evidence，可逐层下钻。
    这也是答辩最好演示的界面。
21. 暴露生命周期控制：展示 view 的 fresh/stale 状态 + 手动重综合按钮；展示 freshness seam 里
    "尚未沉淀进 unit"的 evidence。
22. 用户编辑 unit 后触发对应 view 标 stale → 重综合，形成"我改记忆 → 画像随之更新"的闭环。

## 模块 E：答辩硬化（别省）

23. 端到端测试覆盖主链与边界：写入→evidence→reconcile→unit→view→prompt；
    删最后一条支撑证据→unit 不再可读；**soul_A 的私聊内容不出现在任何公开回复，也不被 soul_B 读到；
    公开评论可被别的人格跨帖读到且带正确归属**。一并修正现在"绿色的谎言"测试。
24. 评论归属迁移（db.py:106）补完整 bucket 迁移，加"已有 unit 库升级"的迁移测试，别只测空库。
25. 权威文档统一到 v2：README/architecture/database/overview 现仍是 v1 md 模型，且新文档内部对
    评论 owner、private gate 有自相矛盾版本 —— 答辩材料会直接引用，必须先收敛成一份
    （以本文件"记忆边界语义基线"为准）。
26. 准备一条确定跑通的 demo 数据 + 脚本，保留 v2 关闭时的 legacy 回退，作为答辩当天保险。

---

## 建议攻坚顺序

`3 → 4 → 6 → 10 → 15/17`（主干闭环：写路 + 生命周期 + legacy 替换 + 归属）
→ `11 / 12 / 13`（读路亮点：unit 索引 + 归属 hydration + freshness seam）
→ `7`（goal）
→ `19 / 20 / 21 / 22`（工作台）
→ `23 / 25`（测试与材料）

其中 goal(7) 与 freshness seam(13) 是两个最大的新增，需尽早排期。
边界（模块 C-14）无需开发，只需在改读路时用回归测试守住。
