# TraceLog 拾迹

> 向内运行的 AI 社交媒体，也是一台陪你长出自己的记忆引擎。

TraceLog 是一个本地优先的个人成长 AI 伴侣。你可以像发动态一样写下日常碎片，让多个 SOUL 以不同人格回应你；系统会把公开记录、待办、反思、画像和 SOUL 相处记忆沉淀到本地 `workspace/`，并用 SQLite + ChromaDB 支撑检索与后续反思。

当前版本是 CLI 原型，重点是跑通“记录 -> 检索上下文 -> 回应 -> 待办 -> 轻反思 -> 深反思 -> 画像/记忆更新”这条主链路。

## 当前功能

- **公开记录**：在 CLI 直接输入文字，会保存为一条 `post`。
- **多 SOUL 评论**：所有启用的 SOUL 会并发回复公开记录，评论写入 `comments`，终端按 `sort_order` 输出。
- **SOUL 私聊**：`/chat <soul>` 进入单个 SOUL 的私聊线程，消息写入 `chat_threads` / `chat_messages`；私聊不写入 posts、不参与公开 post 检索、不触发轻反思，但会进入该 SOUL 的深反思材料。
- **评论线程往返**：`/comment <post_id> <soul>` 在某条公开记录下与指定 SOUL 多轮对话，消息写入 `comment_threads` / `comment_messages`；同样不写入 posts，但会进入 SOUL 深反思。
- **TodoTool**：Todo 是可开关工具，当前默认启用；它只从公开 post 中抽取明确待办，写入 `todos.source_post`，私聊和评论线程不会抽取待办。可用 `/tool todo off` 关闭，`/tool todo on` 重新开启。
- **混合检索**：公开 post 使用 SQLite FTS5（unicode61 + trigram）和 ChromaDB 向量检索，再通过动态权重、距离分数和内容命中 bonus 合并排序。短中文查询会降级到 LIKE fallback。
- **轻反思**：每条公开 post 会抽取实体、情绪、事件、关系和重要性，写入 SQLite 派生表；失败时记录 pending，下次启动会自动重试。
- **全局深反思**：退出 CLI 时按游标读取尚未深反思的公开 post，生成反思记录，并按阈值把可靠画像变更 patch 到 `workspace/user.md`。
- **SOUL 深反思**：退出 CLI 时按 SOUL 分别读取新增的公开首评、私聊消息、评论线程消息，沉淀到 `workspace/soul_memories/<name>.md`，不污染全局用户画像。
- **本地 JSONL 日志**：运行日志写入 `workspace/logs/current.jsonl`；每次启动会把上一次 current 轮转到 `workspace/logs/history/`，默认最多保留 5 个历史日志。
- **结构化错误诊断**：LLM、Embedding、向量索引/检索失败时会写入结构化日志，包含 operation、model/base_url、异常类型、异常消息和排查建议。
- **SOUL 文件管理**：首次启动会创建默认 SOUL 文件；你也可以通过 CLI 创建、启用、禁用、排序，或手动编辑 `workspace/souls/*.md` 后 `/soul resync`。

## 安装

建议使用 Python 3.11。

```bash
pip install -r requirements.txt
```

当前依赖很少：

- `openai>=1.30,<2.0`
- `chromadb>=0.6.0,<1.0`
- Python 标准库 `sqlite3` 需要支持 FTS5 和 trigram tokenizer

## 运行

```bash
python main.py
```

首次运行会生成本地 `config.json`，并要求配置：

- `api_key`：主 LLM API Key
- `base_url`：主 LLM Base URL
- `model`：对话/反思/Todo 使用的模型名
- `embedding_model`：向量索引用的 embedding 模型名
- `embedding_api_key`：可选，单独的 embedding API Key
- `embedding_base_url`：可选，单独的 embedding Base URL

`base_url` 和 `embedding_base_url` 会按配置原样传给 provider，只会去掉末尾 `/`，不会自动补 `/v1`。如果你的主 LLM provider 不支持 embeddings，建议显式配置 `embedding_base_url` 和必要的 `embedding_api_key`。部分 OpenAI-compatible embedding endpoint 需要以 `/v1` 结尾，请按对应 provider 文档填写。

启动成功后，直接输入内容即可发布公开 post。输入：

```text
/quit
```

会退出程序，并依次尝试全局深反思和 SOUL 深反思。若检测到新内容但本次 SOUL 深反思没有生成有效结果，会保留游标，下次退出时继续重试。

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

当前 CLI 主流程大致如下：

1. 对用户输入执行 `Retrieval.hybrid_search(user_input, k=3)`，找出相关历史 post。
2. `ContextBuilder.build_context(relevant_post_ids=...)` 组装用户画像、SOUL 人格、SOUL 记忆、最近 post、相关 post 和活跃待办。
3. `RecordService.save_post(user_input)` 保存公开 post，并尝试写入 ChromaDB 向量索引；失败会进入 pending embedding。
4. 若 TodoTool 启用，先对该 post 抽取/更新待办。
5. `ReplyService.fanout(...)` 让启用的 SOUL 并发生成评论，并把结果写入 `comments`。
6. 对该 post 执行轻反思，抽取结构化记忆；失败会进入 pending light reflection。

私聊和评论线程使用独立消息表与原生 multi-message 历史，不会保存为公开 post，也不会触发 TodoTool 或轻反思。

## 数据存储

TraceLog 默认把运行数据放在 `workspace/` 下：

```text
workspace/
├── state.db              # SQLite 主数据库
├── logs/
│   ├── current.jsonl     # 当前运行日志
│   └── history/          # 历史日志，默认最多保留 5 个
├── user.md               # 用户画像，可直接阅读/编辑
├── souls/                # SOUL 人格 Markdown 文件
├── soul_memories/        # 每个 SOUL 对你的相处记忆
└── chroma_db/            # ChromaDB 向量索引
```

这些运行数据默认不提交到 Git。备份时直接备份整个 `workspace/` 目录即可。

主要数据库表包括：

- `posts` / `comments`
- `chat_threads` / `chat_messages`
- `comment_threads` / `comment_messages`
- `todos`
- `entities` / `emotions` / `events` / `relations`
- `reflections`
- `souls`
- `user_md_revisions` / `soul_memory_revisions`

## 日志

日志配置位于 `config.json` 的 `logging` 字段。默认配置等价于：

```json
{
  "logging": {
    "enabled": true,
    "level": "INFO",
    "preview_chars": 300,
    "history_retention": 5
  }
}
```

LLM 调用会默认记录完整 prompt、response 与解析结果；query rewrite、FTS 构造、hybrid retrieval、observation memory retrieval 和上下文组装也会写入结构化 debug 事件。日志会尽量脱敏 API key/token 形态的字符串。需要排查“LLM call failed or returned invalid JSON”、embedding 初始化失败、向量检索失败、检索命中异常等问题时，优先查看 `workspace/logs/current.jsonl` 中的 `llm_call`、`query_rewrite_result`、`fts_query_built`、`hybrid_retrieval_result`、`memory_retrieval_result`、`context_assembly_result`、`external_api_error`、`vector_query_failed`、`post_index_failed` 等事件。

## 配置注意事项

- TraceLog 使用 OpenAI SDK 调用主 LLM，因此主模型 provider 需要兼容 OpenAI Chat Completions 风格接口。
- ChromaDB 的 `OpenAIEmbeddingFunction` 会使用 `embedding_model` 和 embedding base/key 配置。如果未配置单独 embedding base/key，会回退使用主 `base_url` / `api_key`。
- 系统不会猜测 provider，也不会自动改写 URL。Base URL 是否需要 `/v1` 由 provider 决定。
- 启动时必须成功初始化 ChromaDB 和 embedding function；初始化失败会终止启动，并在终端和 JSONL 日志中输出诊断信息。
- post 索引失败不会丢 post，会记录 `pending_embedding:<post_id>`，下次启动后尝试补齐。

## 当前限制

- 目前仍是 CLI 原型，没有 Web 界面。
- 数据导出命令尚未实现；临时备份请直接复制 `workspace/`。
- 反思和 TodoTool 仍同步跑在 CLI 主线程，公开 post 流程可能被 LLM 调用阻塞。
- SOUL 独立记忆只在 SOUL 深反思时更新，不会在每次私聊或评论后即时写入。
- 目前没有本地开源 embedding provider；embedding 仍通过 OpenAI-compatible 外部接口调用。
- ChromaDB 初始化依赖可用 embedding 服务，初始化失败会终止启动。

## 项目结构

- `main.py`：CLI 入口。
- `core/cli/`：启动流程、配置创建、命令解析、交互 session。
- `core/workspace_service.py`：workspace、数据库、默认画像和 SOUL 初始化编排。
- `core/db.py`：SQLite 连接、schema 初始化、事务与查询 helper。
- `schema.sql`：SQLite schema、FTS5 表和触发器。
- `core/context_builder.py`：组装公开 post 回复上下文。
- `core/record_service.py`：post 保存、近期 post 读取、pending embedding 重试。
- `core/reply_service.py`：多 SOUL 并发公开评论。
- `core/chat_service.py`：SOUL 私聊线程。
- `core/comment_service.py`：post 下的 SOUL 评论线程。
- `core/todo_service.py` / `core/tool_config_service.py`：TodoTool 抽取、应用和开关。
- `core/soul_service.py` / `core/soul_memory_service.py`：SOUL 文件、启用状态、独立记忆。
- `core/profile_service.py`：`user.md` 读写、patch gate、revision 留痕。
- `core/reflector.py`：轻反思、全局深反思、SOUL 深反思。
- `core/retrieval.py`：FTS5 + ChromaDB 混合检索和评分。
- `core/vectorstore.py`：ChromaDB 初始化、索引、查询和 embedding 错误诊断。
- `core/logging_service.py`：本地 JSONL 日志、轮转、脱敏和 LLM 调用记录。
- `core/llm/`：公开回复、私聊/评论回复、TodoTool、反思 prompt 和 JSON 解析。
- `docs/architecture.md`：更细的架构说明。

## 设计理念

TraceLog 的长期方向不是做一个普通聊天机器人，而是做一个由你掌控的个人记忆系统：

- 需要你直接阅读和修改的长期信息，优先保存在 Markdown 中。
- 需要查询、聚合、排序和留痕的数据，进入 SQLite。
- 语义检索由 ChromaDB 单独维护，和主数据库解耦。
- SOUL 对你的独立理解写入各自的 `soul_memories/`，不混进全局用户画像。
- 用户数据默认本地存储；外部 API 只接收完成当前 LLM/Embedding 调用所需的 prompt 或文本。

---

**TraceLog 拾迹** — 致敬每一份不被遗忘的成长。
