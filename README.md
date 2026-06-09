# TraceLog 拾迹

> 向内运行的 AI 社交媒体，也是一台陪你成长的记忆引擎。

TraceLog 是一个本地优先的个人成长 AI 伴侣。你可以像发动态一样写下日常碎片，让多个 SOUL 以不同人格回应你；系统会把公开记录、待办、反思、画像和 SOUL 相处记忆沉淀到本地 `workspace/`，并用 SQLite + ChromaDB 支撑检索与后续反思。

当前版本提供 Web + CLI 双入口。默认入口是 Web，完整的图片附件、图片识别、网页搜索、后台任务事件流和记忆工作台主要在 Web/API 路径中使用；CLI 仍保留文本公开记录、SOUL 管理、私聊、评论线程、工具开关与退出反思能力，适合首次配置、调试和批量管理。

## 当前功能

- **Web timeline**：用类社交媒体的桌面端界面发布公开 post，查看 SOUL 首评，并在评论下方继续线程回复。
- **图片附件**：公开 post、评论追问和 SOUL 私聊都支持本地 JPEG/PNG 图片附件；图片写入 `workspace/attachments/`，后端会自动旋转方向、清理 EXIF，并把超过 5MB 的图片自动压缩到本地存储上限内。
- **图片识别**：可在 Web 设置中启用独立 Vision 模型；图片摘要写入 `vision_cache`，会进入当前回复上下文、轻/深反思输入和 `post_vision` 向量索引。未启用或配置不可用时，系统只提示“有图片但不能查看内容”，避免模型假装看图。
- **多 SOUL 评论**：所有启用的 SOUL 会并发回复公开记录，首条回复写入 `comments`，排序由 `souls.sort_order` 控制。
- **评论线程**：每条 SOUL 首评下面都可以继续多轮回复，消息继续写入 `comments`，用 `(post_id, soul_name, seq)` 形成扁平会话流；线程不进入公开 post 的 FTS5 检索池，但会进入 ChromaDB 统一检索池和该 SOUL 的深反思材料。
- **SOUL 私聊**：可与单个 SOUL 进入私聊线程，消息写入 `chat_threads` / `chat_messages`；私聊不写入 posts、不参与公开 post 检索、不触发轻反思。
- **TodoTool**：Todo 是可开关工具，默认启用；它只从公开 post 中抽取明确待办，写入 `todos.source_post`，私聊和评论线程不会抽取待办。
- **混合检索**：公开 post 使用 SQLite FTS5（unicode61 + trigram）和 ChromaDB 向量检索；私聊/评论使用统一 ChromaDB 检索池读取公开 posts、公开评论对话、图片摘要和当前 SOUL 的私聊片段，再通过动态权重、距离分数和内容命中 bonus 合并排序。
- **网页搜索**：可在 Web 设置中启用 DuckDuckGo 或 Tavily；回复前由 LLM gate 判断是否需要搜索，搜索结果作为外部资料注入上下文，不写入长期记忆。
- **轻反思**：每条公开 post 会抽取实体、情绪、事件、关系和重要性，写入 SQLite 派生表；失败时记录 pending，下次启动会自动重试。
- **深反思**：全局深反思读取公开 posts + 当前 `user.md` + todos，对用户画像做 confirm / revise / retract / add；SOUL 深反思读取该 SOUL 的私聊和评论线程消息，更新对应 `soul_memories/<name>.md`。
- **本地 JSONL 日志**：运行日志写入 `workspace/logs/current.jsonl`；每次启动会把上一次 current 轮转到 `workspace/logs/history/`，默认最多保留 5 个历史日志。
- **记忆工作台**：Web 设置页可以结构化或以 Markdown 全文编辑 `user.md` 和 `soul_memories/<name>.md`，写入 revision 审计；revision 浏览 API 已有，Web 浏览界面尚未完成。
- **SOUL 文件管理**：首次初始化会创建默认 SOUL 文件；你可以在 Web 设置里用 AI 生成或手写 Markdown 新建 SOUL、启用/禁用和排序，也可以通过 CLI 管理，或手动编辑 `workspace/souls/*.md` 后 `/soul resync`。

## 安装

建议使用 Python 3.11。

```bash
pip install -r requirements.txt
```

Web 前端需要 Node.js 与 npm。首次启动 Web 时，TraceLog 会在 `frontend/node_modules/` 不存在时自动执行 `npm install`；你也可以手动安装：

```bash
cd frontend
npm install
```

## 配置

TraceLog 使用 OpenAI SDK 调用主 LLM，因此主模型 provider 需要兼容 OpenAI Chat Completions 风格接口。ChromaDB 的 embedding function 会使用 `embedding_model` 和 embedding base/key 配置，并按 embedding 模型与 base URL 派生独立 collection，避免不同 embedding 空间混用。

API 启动时会读取 `config.json`。如果是第一次使用、配置文件还不存在，Web 仍会启动，并自动引导你进入设置页配置主模型和 Embedding；保存后后端会尝试立即热加载配置，无需先退出到 CLI。CLI 向导仍可作为备用配置路径：

```bash
python main.py cli
```

## 运行

### Web 前端和 API

```bash
python main.py
```

默认会从以下端口启动；如果端口被占用，launcher 会自动向后寻找可用端口，并在终端打印实际地址：

- Web: `http://127.0.0.1:5173/`
- API health check: `http://127.0.0.1:8000/health`

也可以显式指定端口起点：

```bash
python main.py web --backend-port 8001 --frontend-port 5174
```

### CLI

```bash
python main.py cli
```

CLI 模式可以直接输入公开 post，也支持 SOUL 管理、私聊、评论线程和工具开关。输入`/quit`会退出 CLI，并依次尝试全局深反思和 SOUL 深反思。若检测到新内容但本次 SOUL 深反思没有生成有效结果，会保留游标，下次退出时继续重试。

## CLI 命令

SOUL 管理：

```text
/souls
/soul create <name> [description]
/soul enable <name>
/soul disable <name>
/soul reorder <name1> <name2> ...
/soul resync
```

工具开关：

```text
/tools
/tool todo on
/tool todo off
```

私聊与评论线程：

```text
/chat list
/chat <soul>
/comment <post_id> <soul>
```

进入私聊或评论线程后：

```text
/back   # 返回发帖模式
/quit   # 退出程序，并触发退出反思流程
```

## 一次公开 post 会发生什么

Web 与 CLI 最终共用同一组 core service，但调度方式不同：Web/API 会先保存 post 并把后续处理排入 SQLite `jobs`，后台 worker 串行领取；CLI 的文本发帖路径则同步执行保存、检索、回复、TodoTool 和轻反思。Web/API 公开 post 的当前主链路是：

1. 保存公开 post 到 SQLite，并同步 FTS5 索引。
2. 为正文索引、SOUL 首评、TodoTool、轻反思和可能的全局深反思排入后台 jobs；图片-only post 不会排正文 embedding job，但仍会排回复与反思 jobs。
3. worker 串行领取 pending jobs；运行时通过 `post_events` 向前端报告 embedding、reply、todo、reflection 与 pipeline_done 状态。
4. 正文 embedding job 通过 SQLite `vector_docs` / `vector_outbox` 账本同步到 ChromaDB；失败会保留 outbox 状态，后续启动或重试会继续补齐。
5. 回复 job 中，如果带图片且 Vision 可用，生成图片理解摘要，写入 `vision_cache`，并把公开 post 的摘要作为 `post_vision` 文档写入 ChromaDB。
6. 回复 job 对用户输入执行 query rewrite + FTS5 / ChromaDB hybrid search，找出 raw 相关 posts 和图片摘要。
7. `context_builder.build_context()` 组装共享上下文：`user.md`、raw 相关 posts、活跃 todos，以及 Web/API 路径上的可选网页搜索结果。
8. `reply_service.fanout()` 让启用的 SOUL 并发生成首条评论；每个 SOUL 的 soul Markdown 和 `soul_memories/<name>.md` 在对应 SOUL 调用时注入。
9. TodoTool job 抽取或更新明确待办；轻反思 job 写入 entities / emotions / events / relations / importance；累计到阈值后可触发全局深反思 job。

私聊和评论线程使用独立消息表与原生 multi-message 历史，不会保存为公开 post，也不会触发 TodoTool 或轻反思。图片附件会保存和展示；识图可用时当前图片会被摘要后注入 LLM 上下文，识图不可用时只注入“有图片但不能查看内容”的边界提示。

## 设计理念

TraceLog 的长期方向不是做一个普通聊天机器人，而是做一个由你掌控的个人记忆系统：

- 需要你直接阅读和修改的长期信息，优先保存在 Markdown 中。
- 需要查询、聚合、排序和留痕的数据，进入 SQLite。
- 语义检索由 ChromaDB 承载，但 SQLite 保存向量文档账本、outbox 与 collection 同步状态；当当前 collection 滞后或失败时，系统会暂时跳过向量检索，避免使用已知不同步的索引。
- SOUL 对你的独立理解写入各自的 `soul_memories/`，不混进全局用户画像。
- 用户数据默认本地存储；外部 API 只接收完成当前 LLM/Embedding 调用所需的 prompt 或文本。

## 当前限制

- Web 设置页已支持模型、Embedding、图片识别、网页搜索、日志、SOUL、长期记忆编辑和本地数据状态；无配置文件时也能启动 Web，并在保存模型配置后热加载后端运行时。
- 图片附件当前限制为单次最多 9 张、原图单张 50MB 以内，解码安全上限为 60MP / 单边 12000px，仅支持 JPEG/PNG；后端会将图片压缩到 5MB 以内，透明 PNG 会保留透明度，压缩后仍超限会拒绝上传。未发出的上传图片会暂存为孤儿附件，超过 24 小时后由 API 后台任务清理。缩略图生成、附件删除 UI 和图片摘要人工校正 UI 尚未实现。
- 长期记忆编辑已有 Web 工作台，revision 审计已有 service / API 底座；revision 浏览与回滚 UI 尚未完成。
- 数据导出命令尚未实现；临时备份请直接复制 `workspace/`。
- SOUL 独立记忆只在 SOUL 深反思时更新，不会在每次私聊或评论后即时写入。
- 目前没有本地开源 embedding provider；embedding 仍通过 OpenAI-compatible 外部接口调用。
- ChromaDB 初始化依赖可用 embedding 服务；初始化失败不会阻止 API 启动，但语义检索和索引会不可用，终端和 JSONL 日志会输出诊断信息。当前 collection 只有在 SQLite 向量账本确认 ready 后才参与检索。

---

**TraceLog 拾迹** - 致敬每一份不被遗忘的成长。
