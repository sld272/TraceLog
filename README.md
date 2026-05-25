# TraceLog 拾迹

> 向内运行的 AI 社交媒体，也是一台陪你长出自己的记忆引擎。

TraceLog 是一个本地优先的个人成长 AI 伴侣。你可以像发动态一样写下日常碎片，AI 会回应你；开启 Todo 工具后，系统会从公开 post 中提取待办，并把记录沉淀成可检索、可导出、可由你掌控的个人记忆。

当前版本仍是 CLI 原型，核心目标是先把“记录 -> 回应 -> 记忆 -> 待办 -> 画像”这条链路跑通。

## 当前功能

- **日常记录**：在命令行输入一段文字，TraceLog 会保存为一条 post。
- **AI 回应**：结合近期记录、相关历史和待办，返回一段中文回应。
- **可选 Todo 工具**：开启后只从公开 post 中提取明确表达的任务，并支持后续更新状态。
- **语义记忆检索**：使用 ChromaDB + embedding 检索相关历史记录。
- **本地结构化存储**：新记录、待办、SOUL 状态等写入 `workspace/state.db`。
- **可读画像文件**：用户画像保存在 `workspace/user.md`，可以直接打开查看。
- **轻反思抽取**：每条公开记录会抽取实体、情绪、事件和重要性，写入 SQLite 派生表。
- **画像 Patch**：深反思会生成 `user.md` patch，normal 章节自动落盘，high 章节使用更高阈值谨慎落盘。
- **默认 SOUL 初始化**：首次启动会生成 `workspace/souls/默认.md`、`workspace/souls/毒舌好友.md` 以及对应的 `workspace/soul_memories/` 文件。
- **SOUL 管理命令**：CLI 支持查看、创建、启用、禁用、排序和重新扫描 SOUL。
- **SOUL 私聊**：CLI 支持进入单个 SOUL 的私聊线程，私聊消息独立写入 `chat_threads` / `chat_messages`。
- **退出深反思**：退出 CLI 时会结合轻反思摘要，对上次深反思后的公开记录生成一次全局深反思，并保存到 `reflections` 表。

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

私聊命令：

```text
/chat list
/chat <soul>
```

进入私聊后，输入 `/back` 返回发帖模式，输入 `/quit` 退出程序。

## 数据存储

TraceLog 默认把运行数据放在 `workspace/` 下：

```text
workspace/
├── state.db              # SQLite 主数据库
├── user.md               # 用户画像
├── souls/                # AI 好友人格文件
├── soul_memories/        # 每个 AI 好友对你的相处记忆
└── chroma_db/            # ChromaDB 向量索引
```

这些文件都在本地，不会提交到 Git。你可以直接备份整个 `workspace/` 目录。

## 当前限制

- 目前还是 CLI 原型，没有 Web 界面。
- 多 SOUL 管理、并发评论与私聊已接入 CLI，但还没有 Web 管理页。
- 私聊摘要还不会自动沉淀到 `soul_memories/<name>.md`。
- ChromaDB 初始化需要可用的 embedding 服务。

## 项目结构

- `main.py`：CLI 入口
- `core/memory.py`：当前存储与上下文组装层
- `core/router.py`：LLM 回复与深反思 prompt
- `core/reflector.py`：轻反思、深反思触发与落库服务
- `core/profile_service.py`：`user.md` patch、阈值校验与内部写入留痕
- `core/vectorstore.py`：ChromaDB 向量索引
- `schema.sql`：SQLite 初始化 schema
- `core/db.py`：SQLite 连接、初始化与查询 helper
- `docs/architecture.md`：项目架构设计

## 设计理念

TraceLog 的长期方向不是做一个普通聊天机器人，而是做一个由你掌控的个人记忆系统：

- 进 prompt 的长期信息尽量保存在 Markdown 中，方便你直接阅读和修改。
- 需要查询、聚合、统计的数据进入 SQLite。
- 语义检索由 ChromaDB 单独维护。
- 用户数据默认本地存储，优先保证可读、可导出、可删除。

---

**TraceLog 拾迹** — 致敬每一份不被遗忘的成长。
