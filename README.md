# TraceLog 拾迹

> 向内运行的 AI 社交媒体，也是一台陪你成长的记忆引擎。

TraceLog 是一个本地优先的个人成长 AI 伴侣。你可以像发动态一样写下日常碎片，让多个 SOUL 以不同人格回应你；系统会把公开记录、待办、反思、画像和 SOUL 相处记忆沉淀到本地 `workspace/`，并用 SQLite + ChromaDB 支撑检索与后续反思。

当前版本提供 Web + CLI 双入口。默认入口是 Web，CLI 仍保留完整命令能力，适合配置、调试和批量管理。

## 当前功能

- **Web timeline**：用类社交媒体的桌面端界面发布公开 post，查看 SOUL 首评，并在评论下方继续线程回复。
- **图片附件**：公开 post、评论追问和 SOUL 私聊都支持本地 JPEG/PNG 图片附件；图片写入 `workspace/attachments/`，后端会自动旋转方向、清理 EXIF，并把超过 5MB 的图片自动压缩到本地存储上限内。
- **多 SOUL 评论**：所有启用的 SOUL 会并发回复公开记录，首条回复写入 `comments`，排序由 `souls.sort_order` 控制。
- **评论线程**：每条 SOUL 首评下面都可以继续多轮回复，消息继续写入 `comments`，用 `(post_id, soul_name, seq)` 形成扁平会话流；线程不进入公开 post 的 FTS5 检索池，但会进入 ChromaDB 统一检索池和该 SOUL 的深反思材料。
- **SOUL 私聊**：可与单个 SOUL 进入私聊线程，消息写入 `chat_threads` / `chat_messages`；私聊不写入 posts、不参与公开 post 检索、不触发轻反思。
- **TodoTool**：Todo 是可开关工具，默认启用；它只从公开 post 中抽取明确待办，写入 `todos.source_post`，私聊和评论线程不会抽取待办。
- **混合检索**：公开 post 使用 SQLite FTS5（unicode61 + trigram）和 ChromaDB 向量检索，再通过动态权重、距离分数和内容命中 bonus 合并排序。
- **轻反思**：每条公开 post 会抽取实体、情绪、事件、关系和重要性，写入 SQLite 派生表；失败时记录 pending，下次启动会自动重试。
- **深反思**：全局深反思读取公开 posts + 当前 `user.md` + todos，对用户画像做 confirm / revise / retract / add；SOUL 深反思读取该 SOUL 的私聊和评论线程消息，更新对应 `soul_memories/<name>.md`。
- **本地 JSONL 日志**：运行日志写入 `workspace/logs/current.jsonl`；每次启动会把上一次 current 轮转到 `workspace/logs/history/`，默认最多保留 5 个历史日志。
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

TraceLog 使用 OpenAI SDK 调用主 LLM，因此主模型 provider 需要兼容 OpenAI Chat Completions 风格接口。ChromaDB 的 embedding function 会使用 `embedding_model` 和 embedding base/key 配置。

API 启动时会先读取 `config.json`。如果是第一次使用、配置文件还不存在，请先用 CLI 向导生成基础配置，或手动创建 `config.json`；应用能启动后，可以在 Web 设置页继续维护主模型、Embedding、日志和 SOUL 配置：

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

Web 与 CLI 最终共用同一组 core service。公开 post 的主链路是：

1. 保存公开 post 到 SQLite，并同步 FTS5 索引。
2. 尝试写入 ChromaDB 向量索引；失败时记录 `pending_embedding:<post_id>`，后续启动自动重试。
3. 如果 TodoTool 启用，抽取或更新明确待办。
4. 对用户输入执行 query rewrite + FTS5 / ChromaDB hybrid search，找出 raw 相关 posts。
5. `context_builder.build_context()` 组装共享上下文：`user.md`、raw 相关 posts、活跃 todos。
6. `reply_service.fanout()` 让启用的 SOUL 并发生成首条评论；每个 SOUL 的 soul Markdown 和 `soul_memories/<name>.md` 在对应 SOUL 调用时注入。
7. 对该 post 执行轻反思，写入 entities / emotions / events / relations / importance。

私聊和评论线程使用独立消息表与原生 multi-message 历史，不会保存为公开 post，也不会触发 TodoTool 或轻反思。图片附件当前只作为本地内容附件保存和展示；未启用识图能力时，LLM 只会收到“用户附带了图片但不能查看内容”的边界提示，不会被要求描述图片。

## 设计理念

TraceLog 的长期方向不是做一个普通聊天机器人，而是做一个由你掌控的个人记忆系统：

- 需要你直接阅读和修改的长期信息，优先保存在 Markdown 中。
- 需要查询、聚合、排序和留痕的数据，进入 SQLite。
- 语义检索由 ChromaDB 单独维护，和主数据库解耦。
- SOUL 对你的独立理解写入各自的 `soul_memories/`，不混进全局用户画像。
- 用户数据默认本地存储；外部 API 只接收完成当前 LLM/Embedding 调用所需的 prompt 或文本。

## 当前限制

- Web 设置页已支持模型、Embedding、日志、SOUL 和本地数据状态；但无配置文件时 API 仍需先通过 CLI 或手动 `config.json` 完成首次启动配置。
- 图片附件当前限制为单次最多 9 张、原图单张 50MB 以内，解码安全上限为 60MP / 单边 12000px，仅支持 JPEG/PNG；后端会将图片压缩到 5MB 以内，透明 PNG 会保留透明度，压缩后仍超限会拒绝上传。未发出的上传图片会暂存为孤儿附件，超过 24 小时后由 API 后台任务清理。识图、缩略图生成和附件删除 UI 尚未实现。
- 长期记忆编辑与 revision 审计已有 service / API 底座，但 Web UI 尚未完成。
- 数据导出命令尚未实现；临时备份请直接复制 `workspace/`。
- SOUL 独立记忆只在 SOUL 深反思时更新，不会在每次私聊或评论后即时写入。
- 目前没有本地开源 embedding provider；embedding 仍通过 OpenAI-compatible 外部接口调用。
- ChromaDB 初始化依赖可用 embedding 服务；初始化失败不会阻止 API 启动，但语义检索和索引会不可用，终端和 JSONL 日志会输出诊断信息。

---

**TraceLog 拾迹** - 致敬每一份不被遗忘的成长。
