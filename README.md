# TraceLog 拾迹

> 向内运行的 AI 社交媒体，也是一台陪你长出自己的记忆引擎。

TraceLog 是一个本地优先的个人成长 AI 伴侣。你可以像发动态一样写下日常碎片，AI 会回应你；开启 Todo 工具后，系统会从公开 post 中提取待办，并把记录沉淀成可检索、可导出、可由你掌控的个人记忆。

当前版本仍是 CLI 原型，核心目标是先把“记录 -> 回应 -> 记忆 -> 待办 -> 画像”这条链路跑通。

## 当前功能

- **日常记录**：在命令行输入一段文字，TraceLog 会保存为一条 post。
- **多 SOUL 评论**：所有启用的 SOUL 对每条 post 并发生成一条评论，写入 `comments`，CLI 按 sort_order 输出。
- **可选 Todo 工具**：默认关闭，`/tool todo on` 后才从公开 post 中提取明确表达的任务，写入 `todos.source_post`；私聊与评论线程一律不抽取待办。
- **SOUL 私聊**：`/chat <soul>` 进入单个 SOUL 的私聊线程，消息独立写入 `chat_threads` / `chat_messages`，不写 posts、不进检索池、不触发反思。
- **评论线程往返**：`/comment <post_id> <soul>` 进入单 SOUL 的评论线程（`comment_threads` / `comment_messages`），用于在某条 post 下与该 SOUL 多轮对话；同样不写 posts、不触发反思。
- **语义记忆检索**：FTS5 双 tokenizer（unicode61 + trigram）+ ChromaDB 向量索引 + RRF 融合的双轨检索。
- **本地结构化存储**：post、评论、待办、SOUL 状态、反思、画像 revision 等都写入 `workspace/state.db`。
- **可读画像文件**：用户画像保存在 `workspace/user.md`，可以直接打开查看与编辑。
- **轻反思抽取**：每条公开记录会抽取实体、情绪、事件和重要性，写入 SQLite 派生表；失败的会进 pending 队列下次启动重试。
- **画像 Patch**：CLI 退出触发全局深反思，生成 `user.md` patch；normal 章节自动落盘，high 章节使用更高阈值谨慎落盘，所有写入留痕到 `user_md_revisions`。
- **SOUL 深反思**：按 SOUL 增量游标读对应私聊与评论线程原文，沉淀到 `soul_memories/<name>.md` 与 `soul_memory_revisions`；不污染全局画像。
- **本地运行日志**：启动后写入 `workspace/logs/current.jsonl`，每次启动轮转上次日志到 `workspace/logs/history/`，默认保留最近 5 个历史文件；LLM 调用会记录结构化调试信息。
- **默认 SOUL 初始化**：首次启动会生成 `workspace/souls/默认.md`、`workspace/souls/毒舌好友.md` 以及对应的 `workspace/soul_memories/` 文件。
- **SOUL 管理命令**：CLI 支持查看、创建、启用、禁用、排序和重新扫描 SOUL。

## 安装

建议使用 Python 3.11。

```bash
pip install -r requirements.txt
```

当前依赖包括 OpenAI SDK 和 ChromaDB。你的 Python 发行版需要带有支持 FTS5/trigram 的 `sqlite3`。

## 运行

```bash
python main.py
```

首次运行会要求配置：

- API Key
- API Base URL
- 对话模型名称
- Embedding 模型名称
- 可选的单独 Embedding API Key / Base URL

配置会保存在本地 `config.json`。这个文件已被 `.gitignore` 忽略。

进入交互后，直接输入你想记录的内容即可。输入：

```text
/quit
```

即可退出；退出时 TraceLog 会触发一次全局深反思，并把可靠的画像变更写入 `workspace/user.md`；high sensitivity 章节会使用更高 confidence 阈值。

SOUL 管理命令：

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

私聊与评论线程命令：

```text
/chat list
/chat <soul>
/comment <post_id> <soul>
```

进入私聊或评论线程后，输入 `/back` 返回发帖模式，输入 `/quit` 退出程序。

## 数据存储

TraceLog 默认把运行数据放在 `workspace/` 下：

```text
workspace/
├── state.db              # SQLite 主数据库
├── logs/                 # 当前日志与最近 5 份历史日志
├── user.md               # 用户画像
├── souls/                # AI 好友人格文件
├── soul_memories/        # 每个 AI 好友对你的相处记忆
└── chroma_db/            # ChromaDB 向量索引
```

这些文件都在本地，不会提交到 Git。你可以直接备份整个 `workspace/` 目录。

日志默认只保存 LLM payload 摘要。需要完整 prompt/response 时，可在 `config.json` 中设置：

```json
{
  "logging": {
    "enabled": true,
    "llm_payload": "full"
  }
}
```

`llm_payload` 也可以设为 `"summary"` 或 `"off"`。日志会尽量脱敏 API key/token 形态的字符串，但完整 payload 仍可能包含你的私密记录。

## 当前限制

- 目前还是 CLI 原型，没有 Web 界面（FastAPI 后端与 Web 前端在 5/31 报名前的 P0 中）。
- 数据导出（`tracelog export`）尚未实现，临时备份请直接拷贝 `workspace/`。
- 反思仍同步跑在 CLI 主线程，每帖会阻塞一次轻反思；CLI 退出时还会跑全局深反思。后台异步队列在第二期处理。
- SOUL 独立画像只在 SOUL 深反思时（CLI 退出时）更新，不会在每次私聊或评论后即时改写。
- ChromaDB 初始化需要可用的 embedding 服务；初始化失败会直接终止启动。

## 项目结构

- `main.py`：CLI 入口
- `core/cli/`：CLI 启动、配置、命令解析与交互会话
- `core/workspace_service.py`：workspace、数据库、默认画像与 SOUL 初始化编排
- `core/profile_service.py`：`user.md` 读写、patch、阈值校验与内部写入留痕
- `core/record_service.py`：post 写入、格式化与近期历史读取
- `core/reply_service.py`：多 SOUL 并发评论
- `core/comment_service.py` / `core/chat_service.py`：评论线程与私聊
- `core/todo_service.py` / `core/tool_config_service.py`：可选 TodoTool 抽取与开关
- `core/llm/`：回复、TodoTool、轻/深反思 prompt 与 JSON 解析；`types.LLMClient` Protocol
- `core/reflector.py`：轻反思、全局深反思、SOUL 深反思
- `core/retrieval.py`：FTS5 + ChromaDB 动态权重 hybrid scoring
- `core/vectorstore.py`：ChromaDB 向量索引 provider
- `schema.sql`：SQLite 初始化 schema
- `core/db.py`：SQLite 连接、初始化、查询 helper 与 `require_lastrowid`
- `docs/architecture.md`：项目架构设计

## 设计理念

TraceLog 的长期方向不是做一个普通聊天机器人，而是做一个由你掌控的个人记忆系统：

- 进 prompt 的长期信息尽量保存在 Markdown 中，方便你直接阅读和修改。
- 需要查询、聚合、统计的数据进入 SQLite。
- 语义检索由 ChromaDB 单独维护。
- 用户数据默认本地存储，优先保证可读、可导出、可删除。

---

**TraceLog 拾迹** — 致敬每一份不被遗忘的成长。
