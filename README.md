# TraceLog 拾迹

TraceLog 是一个本地优先的个人成长 AI 伴侣。用户像发动态一样记录生活，多个
SOUL 以不同人格回应；系统把公开 post、评论、私聊和图片理解统一沉淀为可追溯的
memory-v2。

## 核心能力

- Web timeline、评论线程与 SOUL 私聊
- JPEG/PNG 附件、Vision 图片摘要与网页搜索
- SQLite FTS5 + ChromaDB 混合检索
- TodoTool、GoalTool 与建议确认
- memory-v2：evidence event → memory unit → portrait view
- 记忆工作台：新增、编辑、撤回、禁用提示、画像纳入策略、证据追溯
- SQLite job worker、向量 outbox 与 JSONL 运行日志

## memory-v2

所有用户输入都会在业务写入的同一事务中追加到
`memory_ingest_events`。后台 `run_memory_reconcile` job 按
`owner_scope + visibility_scope` 消费新 evidence，输出
add / retain / confirm / revise / retract 操作并写入 `memory_units`。

稳定且重要的 core units 会综合成：

- `user_portrait`：全局用户画像
- `soul_relationship_memory`：每个 SOUL 与用户的关系叙事

回复时按四层注入：基线画像、当前状态、相关 units、尚未对账的近期 evidence。
当前消息会从 freshness 输入中排除，避免重复。公开内容可跨 SOUL 使用；私聊仅当前
SOUL 可读，在公开场景使用自己的私密信息时会附带谨慎披露规则。

## 安装

项目统一使用 conda 环境 `tracelog`：

```bash
conda run -n tracelog pip install -r requirements.txt
cd frontend && npm install
```

## 运行

```bash
conda run -n tracelog python main.py
conda run -n tracelog python main.py cli
```

Web 默认从 `127.0.0.1:5173` 启动，API 默认从 `127.0.0.1:8000`
启动；端口被占用时 launcher 会自动寻找下一个可用端口。

CLI 支持：

```text
/souls
/soul create <name> [description]
/soul enable <name>
/soul disable <name>
/soul reorder <name1> <name2> ...
/soul resync
/chat <soul>
/comment <post_id> <soul>
/tools
/tool todo on|off
/quit
```

CLI 启动、每次公开 post 后和退出时都会运行 memory-v2 对账。

## 公开 post 全链路

1. 保存 post、附件关系、SOUL 顺序快照和 evidence event。
2. 排入 embedding、SOUL 回复、TodoTool、memory reconcile jobs。
3. Vision 摘要写入 `vision_cache`、向量索引和 `post_vision` evidence。
4. 回复前执行 query rewrite、FTS5/ChromaDB 检索和 memory-v2 读模型组装。
5. reconcile 消费所有 pending buckets，刷新 portrait views，并同步 unit 向量。

## 测试

```bash
conda run -n tracelog python -m unittest discover tests
cd frontend && npm run build
```

## 文档

- [系统概览](docs/overview.md)
- [架构](docs/architecture.md)
- [数据库](docs/database.md)
- [API](docs/api.md)
- [memory-v2 全链路](docs/memory-v2-architecture.md)
