# goaltool 实施计划（完整方案 A）

> 状态：**已完整实施**（2026-06-19，feat/memory-v2 分支）。本文是把 [memory-v2-state-goals-suggestions-design.md](./memory-v2-state-goals-suggestions-design.md) 落成可执行步骤的施工计划，采用完整方案 A：reconcile 停产 `type=goal` unit、目标全部归 goaltool、引入统一建议机制（系统提议 + 对话当下确认）。当前权威现状见 [memory-v2-architecture.md](./memory-v2-architecture.md)。
>
> 已与代码核对的现状见文末「附录：现状基线」。已实现部分的权威说明见 [memory-v2-architecture.md](./memory-v2-architecture.md)。

## 0. 核心决策（已定）

1. **目标的唯一真相源是 goaltool**，不再是 memory unit。reconcile 不再产 `type=goal` unit。
2. **目标 = 用户承诺，必须用户确认才 active**；其他 unit（identity/preference/...）仍是被动抽取、无需确认。这是 goal 与普通 unit 唯一的特殊待遇。
3. **职责分层，不存在「降级」**：
   - 记忆轨（被动印象）：reconcile 把"用户想考研"抽成 `preference`/`insight` unit——这是系统印象，会衰减、低调。它的存在与用户接不接受目标建议**无关**，本来就该在。
   - 目标轨（用户承诺）：另一条 suggestion 机制判断"这像个可追踪目标" → 生成建议 → 对话当下问用户。
4. **拒绝永久理解为「我不想正式追踪它」**：只作用于目标轨（建议标 `dismissed` + 墓碑防反复建议），**记忆轨的 preference/insight unit 不动**。不区分"理解错了"这种意图——那走独立的删 unit 流程，不在 goaltool 范围。

> 推论：被识别的目标线索在记忆里的归宿天然就是 preference/insight，**不是从 goal 降级来的，而是它本来的形态**。goaltool 在其上加一条"用户确认才成立"的承诺轨。

## 1. 数据模型

### 1.1 `goals` 表（承诺目标）

```sql
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,            -- g_<ulid>
    title       TEXT NOT NULL,
    detail      TEXT,
    horizon     TEXT NOT NULL CHECK(horizon IN ('short','long')),
    status      TEXT NOT NULL DEFAULT 'active'
                  CHECK(status IN ('active','done','abandoned','paused')),
    source      TEXT NOT NULL DEFAULT 'user'
                  CHECK(source IN ('user','suggested_accepted')),
    focus            INTEGER NOT NULL DEFAULT 0,   -- 是否「当前关注」
    last_progress_at REAL,                          -- 最近推进时间（喂 30 天关注窗口）
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goals_status_horizon ON goals(status, horizon, id);
```

### 1.2 `suggestions` 表（统一建议机制，goal + todo 共用）

```sql
CREATE TABLE IF NOT EXISTS suggestions (
    id           TEXT PRIMARY KEY,            -- s_<ulid>
    kind         TEXT NOT NULL CHECK(kind IN ('todo','goal')),
    payload_json TEXT NOT NULL,               -- 拟建对象内容（含 horizon/deadline 等）
    evidence_ref TEXT,                         -- 来源 post/comment/chat 或 evidence event
    confidence   REAL NOT NULL DEFAULT 0.6,
    status       TEXT NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','accepted','dismissed')),
    normalized_key TEXT,                       -- 防诈尸去重键（归一化内容+来源）
    created_at   REAL NOT NULL,
    decided_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_suggestions_kind_status ON suggestions(kind, status, id);
CREATE INDEX IF NOT EXISTS idx_suggestions_normkey ON suggestions(normalized_key);
```

> schema 落地方式与全库一致：写进 `schema.sql`，`init_db` 的 `executescript` + `IF NOT EXISTS` 自动补建老库。CHECK 枚举一次写全（`paused`、`dismissed` 等预留值现在就写，避免日后重建表）。

## 2. 后端 service

### 2.1 `core/goal_service.py`（蓝本：`core/todo_service.py`）

- `list_goals(*, status=None, horizon=None)` / `list_active_long_term()` / `list_active_short_term()`。
- `create_goal(title, detail, horizon, *, source='user')` → `g_<ulid>`。
- `update_goal(id, **fields)` / `set_status(id, status)` / `set_focus(id, bool)` / `mark_progress(id)`（更新 `last_progress_at`）。
- `format_goal_for_context(goal)` —— 注入用文本（蓝本 `todo_service.format_todo_for_context`）。
- 短期目标 30 天无推进移出「当前关注」的惰性 GC（读时顺手按 `last_progress_at` 判断，不设定时任务，蓝本 state 块的 7 天惰性过期）。

### 2.2 `core/suggestion_service.py`

- `create_suggestion(kind, payload, evidence_ref, confidence)` —— 落库前按 `normalized_key` 查 `dismissed` 墓碑，命中则不再建议（防诈尸）。
- `list_pending(kind=None)` / `accept(id)` / `dismiss(id)`。
- `accept(goal)` → 调 `goal_service.create_goal(source='suggested_accepted')` 并回填 `status='accepted'`；`accept(todo)` → 调 todo 创建。
- `dismiss(id)` → `status='dismissed'` + 写墓碑（按 `normalized_key`）。**这是用户拒绝目标的落点；记忆 unit 一律不碰。**

### 2.3 `core/llm/goal_router.py`（蓝本：`core/llm/todo_router.py`）

- 一个 LLM 抽取 prompt：从一批 evidence/对话里识别「可追踪目标」候选（带 horizon、title、detail、置信），产 suggestion 候选而非直接建目标。
- 严格区分「目标」与「愿望」：随口"想做游戏开发"→ 不一定产 goal 建议（低置信），它已由 reconcile 留成 preference/insight；明确"我决定考研/这学期要提 GPA"→ 高置信目标候选。

## 3. reconcile 停产 goal（最敏感的一步）

1. `core/llm/reflection_router.py`：`MEMORY_RECONCILE_PROMPT` 的 op schema 里 `type` 去掉 `goal`；`_RECONCILE_TYPES` 集合移除 `goal`；prompt 增一句"目标类信息不在此抽取，留给目标管理；用户对某方向的兴趣/倾向抽成 preference 或 insight"。
2. `core/memory_read.py`：`list_goals`（读 `type=goal` unit）**删除或改为读 `goal_service`**（见 §5 注入改造）。
3. **解析兜底**：老库里已存在的 `type=goal` unit 仍合法（CHECK 仍含 goal，不动），由迁移（§6）处理；新 reconcile 不再产出。
4. 改 `tests/test_memory_reconcile_producer.py` 等固化 goal 抽取的断言（如有）。

> 时序要求：**reconcile 去掉 goal 与 goaltool 上线必须同批**，否则中间出现「目标无人抽取」空窗。

## 4. 对话当下确认（建议机制接入回复路）

设计要求 todo/goal 统一在对话当下确认，而非默默丢托盘。落地：

1. 回复生成后（chat / comment / public 三路），用 `goal_router` 对本轮用户输入做一次目标候选抽取（轻量、可 gate 一个开关、可异步入 job）。
2. 命中候选 → `suggestion_service.create_suggestion`（经墓碑去重）。
3. **把 pending 建议挂到回复返回结构上**（`ChatReplyResult` 等增一个 `suggestions` 字段），前端在对话流里渲染"要记进目标吗？[采纳]/[忽略]"。
4. 用户点采纳 → `suggestion_service.accept`；忽略 → `dismiss`（永久=不再追踪）。
5. **工作台托盘**作复核兜底：展示历史 pending、可补确认、可手动新增（处理对话当下没确认的）。

> MVP 可裁剪点：对话当下确认 UI 较重，可先只落「建议入库 + 托盘复核 + 手动确认」，对话流内联确认作为紧随的一步。但**带 deadline 的 todo** 尤其需要对话当下确认（避免 deadline 已过才在托盘看到）；goal 无此急迫性，可托盘起步。

## 5. 注入改造

当前 `context_builder.py` 注入 `# 待办事项`（自动）。goaltool 上线后：

1. **长期目标 always-on 注入**：新增一段 `# 长期目标`，读 `goal_service.list_active_long_term()`，类比现 todo 注入位置（`context_builder.py:56` 附近）。`user.md` 画像不再承担"列目标"。
2. **当前关注**（短期目标）进「当前状态」块：`memory_read` 的 `[当前状态]` 块目前是 `type=state` unit；增一个只读投影——active + horizon=short + 近期推进的 goal。单一真相源是 goaltool，块只开窗看。
3. **注入去重**：用户接受目标后，记忆里那条 preference/insight 仍在（分层正确，非冗余），但回复注入层要避免"你对考研有兴趣"和"你的目标是考研"同时出现——按主题去重。
4. 边界：goal 默认 `global`，按 `memory_scope_policy` 与现有注入一致（公开/私聊均可见用户自己的目标）。

## 6. 迁移：现有 goal unit → goaltool

1. 扫 `memory_units` 中 `type='goal' AND status='active'` 的 unit。
2. 每条建一个 `goals` 行（`source='suggested_accepted'` 或专门标 `migrated`，horizon 按 content 粗判 long/short，保守落 long）。
3. 原 goal unit：**retract（`retracted_by_model`, reason=outdated）**，记忆库只保留"用户正在为某目标努力"这类 relationship/state 痕迹（若 reconcile 已另产则不重复造）。
4. 幂等：用一次性 `meta` marker（蓝本 `memory_v2_rechallenge_v1`），重复启动不重复迁移。
5. 迁移后触发受影响画像 view 重综合（goal 退出 user.md 画像）。

## 7. 前端

- `GoalsPage.tsx`（蓝本 `TodosPage.tsx`）：长/短期目标列表、增删改、调 status（active/done/abandoned/paused）、设 focus。
- 建议托盘：复核 pending suggestion（采纳/忽略），goal + todo 共用一个面板。
- （对话当下确认）回复气泡下内联"要记进目标/待办吗？"——依赖 §4 第 3 步的返回结构。

## 8. API 路由

- `api/routes/goals.py`（蓝本 `api/routes/` 现有 todo/profile 路由）：goals CRUD + status/focus。
- `api/routes/suggestions.py`：list pending / accept / dismiss。

## 9. 落地顺序（解耦，避免返工）

1. **schema**：`goals` + `suggestions` 两表进 `schema.sql`。
2. **goal_service + suggestion_service + goal_router**：纯后端，可单测。
3. **迁移脚本**（§6）：现有 goal unit → goals，幂等 marker。
4. **reconcile 停产 goal**（§3）+ goaltool 注入（§5）**同批**上，杜绝空窗。
5. **前端 GoalsPage + 建议托盘**（§7）。
6. **对话当下确认**（§4）：回复路抽取 + 返回结构 + 内联 UI，behind flag。
7. **todo 接入建议机制**（行为变更，behind flag）：todo 从"自动写"改为"建议→确认"。这是改已在跑的功能，放最后、可独立开关。

> 1–4 是 goaltool 的最小可用闭环（目标能被识别、确认、追踪、注入回复）；5 给它一个管理面；6–7 是把建议机制做完整、并把 todo 收敛进来。每一档都可独立验收。

## 10. 验收要点

- reconcile 不再产 `type=goal` unit；"想做某事"被抽成 preference/insight。
- 目标候选 → 建议 → 用户确认才进 `goals` active；拒绝只 dismiss 建议 + 墓碑，**记忆 unit 不变**。
- 同一被拒目标不被反复建议（墓碑生效）。
- 长期目标 always-on 注入；短期 active 目标进「当前状态/关注」块；注入无主题重复。
- 迁移幂等；迁移后 user.md 画像不再含目标条目。
- 全程 behind flag 可回退；todo 旧"自动写"行为在未开新开关时不变。

## 11. 不在本计划

- goal ↔ todo 父子关联与进度滚动。
- 建议机制的"信任度自动采纳"分档（当前统一对话当下确认）。
- `memory_reviews` 统一复核面（与建议机制收敛是方向，非本期）。
- 当前关注块的多信号打分（用 recency × last_progress 简单排序）。

---

## 附录：实现前历史基线（已被本计划替代）

- **goal 现状**：reconcile 仍产 `type=goal` unit（`reflection_router._RECONCILE_TYPES` 含 goal）；`memory_read.list_goals` 读 `type=goal AND status=active` unit。无 `goals` / `suggestions` 表。
- **todo 三件套（goaltool 蓝本）**：`core/todo_service.py`（load/list/apply/format）、`core/app_services/todo_editor.py`、`core/llm/todo_router.py`、`frontend/src/pages/TodosPage.tsx`；`todos` 表见 `schema.sql:269`。
- **todo 注入**：`context_builder.py:56` 自动注入 `# 待办事项`（`list_active_todos`），**非**建议+确认。
- **todo 入队**：`public_post_pipeline` 的 `TYPE_RUN_TODO_TOOL` job（`run_for_post_safely`）。
- **当时的当前状态块**：`memory_read` 的 `[当前状态]` 只读 `type=state` unit（recency×importance，7 天窗口），尚无 goaltool 当前关注投影。
- **当时的回复返回结构**：`chat_service.ChatReplyResult` 等尚无 suggestions 字段。
- **工作台后端**：`memory_review_service` 仅读写 legacy md，无 unit/goal 编辑 API。
