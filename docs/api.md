# TraceLog API 文档

本文档描述 TraceLog 当前 FastAPI 后端的 HTTP/SSE 接口。它面向前端联调、脚本调用和后续 API 维护；自动生成的交互式 OpenAPI 页面仍可在后端服务的 `/docs` 查看。

## 1. 基础约定

- 后端默认地址：`http://127.0.0.1:8000`
- 前端开发代理地址：`http://127.0.0.1:5173/api`
- 直连后端时路径不带 `/api`；前端代码里的 `/api` 是 Vite proxy 前缀，会转发并重写到后端根路径。
- 普通 JSON 请求使用 `Content-Type: application/json`。
- 图片上传使用 `multipart/form-data`，字段名优先使用 `file`。
- 当前 API 没有登录态和鉴权；默认假设只在本机开发环境访问。
- 需要 LLM runtime 的接口在模型未配置时返回 `409`，错误信息为 `请先在设置页完成模型配置`。
- 常见错误响应形态：

```json
{
  "detail": "错误说明"
}
```

常见状态码：

| 状态码 | 含义 |
| --- | --- |
| `400` | 请求格式错误，例如非法 multipart form |
| `404` | 资源不存在 |
| `409` | 模型配置未完成，无法调用 LLM |
| `422` | 参数校验失败或业务规则不满足 |
| `500` | 服务端处理失败 |
| `502` | 上游 LLM 调用或生成失败 |

## 2. 通用对象

### Attachment

```json
{
  "id": "att_...",
  "file_path": "workspace/attachments/images/...",
  "mime_type": "image/jpeg",
  "file_size": 123456,
  "width": 1280,
  "height": 720,
  "sha256": "...",
  "original_filename": "photo.jpg",
  "linked_at": 1710000000.0,
  "created_at": 1710000000.0,
  "url": "/attachments/att_..."
}
```

### PipelineStatus

```json
{
  "state": "running",
  "pending_count": 2,
  "running_count": 1,
  "retrying_count": 0,
  "failed_jobs": []
}
```

`state` 取值：`idle` / `running` / `retrying` / `failed` / `done`。

### Job

```json
{
  "id": 1,
  "type": "generate_post_replies",
  "status": "pending",
  "payload_json": "{\"post_id\":\"...\"}",
  "payload": { "post_id": "..." },
  "attempts": 0,
  "max_attempts": 3,
  "error": null,
  "created_at": 1710000000.0,
  "updated_at": 1710000000.0,
  "started_at": null,
  "finished_at": null
}
```

`status` 取值：`pending` / `running` / `succeeded` / `failed` / `cancelled`。

### Revision

Revision summary：

```json
{
  "id": 12,
  "target_type": "user",
  "target_name": null,
  "source": "user",
  "patch": { "op": "overwrite_user_memory" },
  "created_at": 1710000000.0
}
```

Revision detail 会额外包含完整快照：

```json
{
  "snapshot": "# User Memory\n..."
}
```

## 3. 健康检查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 检查数据库、模型配置和向量索引初始化状态 |

响应：

```json
{
  "ok": true,
  "db": "ok",
  "configured": true,
  "vectorstore_initialized": true
}
```

## 4. 公开 Post

公开 post 是 timeline、FTS5、向量索引、轻反思、TodoTool 和全局深反思的主要事实源。Web/API 发帖后会先写入 SQLite，再把 embedding、SOUL 首评、TodoTool、轻反思等处理排入 `jobs`。每个 post 会保存发帖当时启用 SOUL 的排序快照；后续读取 root comments 时按该快照展示。

### 创建 post

`POST /posts`

请求：

```json
{
  "content": "今天终于把论文大纲写完了",
  "attachment_ids": ["att_..."]
}
```

约束：

- `content` 最大 20000 字符。
- `attachment_ids` 最多 9 个。
- `content` 和 `attachment_ids` 不能同时为空。
- 需要模型配置完成。

响应：

```json
{
  "post_id": "20260612-001",
  "status": "queued",
  "job_ids": [1, 2, 3, 4]
}
```

### 列表、搜索、详情和删除

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/posts` | `limit=20`，`offset=0`，可选 `before_ts` + `before_id` | 按 `ts desc, id desc` 获取 timeline。`before_ts` 与 `before_id` 必须同时提供 |
| `GET` | `/posts/search` | `q`，`limit=20`，`mode=keyword\|hybrid` | 搜索公开 post；`hybrid` 会尝试语义检索 |
| `GET` | `/posts/{post_id}` | 无 | 获取 post、root comments、关联 jobs 和 events |
| `DELETE` | `/posts/{post_id}` | 无 | 删除 post、comments、排序快照、附件关联，并取消 pending jobs |

`GET /posts` 单项响应：

```json
{
  "post_id": "20260612-001",
  "ts": "2026-06-12 10:00:00",
  "content": "今天终于把论文大纲写完了",
  "importance": 3,
  "comment_count": 2,
  "latest_event_type": "pipeline_done",
  "pipeline_status": {
    "state": "done",
    "pending_count": 0,
    "running_count": 0,
    "retrying_count": 0,
    "failed_jobs": []
  },
  "attachments": []
}
```

`GET /posts/search` 响应：

```json
{
  "items": [
    {
      "post_id": "20260612-001",
      "ts": "2026-06-12 10:00:00",
      "content": "今天终于把论文大纲写完了",
      "importance": 3,
      "comment_count": 2,
      "latest_event_type": null,
      "pipeline_status": { "state": "done", "pending_count": 0, "running_count": 0, "retrying_count": 0, "failed_jobs": [] },
      "attachments": [],
      "match": "both"
    }
  ],
  "semantic_available": true,
  "mode": "hybrid"
}
```

`match` 取值：`keyword` / `semantic` / `both`。

`GET /posts/{post_id}` 响应：

```json
{
  "post": {
    "post_id": "20260612-001",
    "ts": "2026-06-12 10:00:00",
    "content": "今天终于把论文大纲写完了",
    "importance": 3,
    "created_at": 1710000000.0,
    "updated_at": 1710000000.0,
    "attachments": [],
    "latest_event_type": "pipeline_done",
    "pipeline_status": { "state": "done", "pending_count": 0, "running_count": 0, "retrying_count": 0, "failed_jobs": [] }
  },
  "comments": [],
  "jobs": [],
  "events": []
}
```

`comments` 只包含每个 SOUL 的 `seq=0` 首评，顺序来自该 post 的 `post_soul_orders` 快照；无快照的旧数据回退到评论创建时间和 id。

删除响应：

```json
{
  "ok": true,
  "post_id": "20260612-001",
  "deleted_comments": 2,
  "cancelled_jobs": 1
}
```

### Post 事件流

`GET /posts/{post_id}/events`

查询参数：

- `after_id`：只推送该 event id 之后的新事件。
- 也支持 SSE 标准请求头 `Last-Event-ID`。

响应类型：`text/event-stream`

事件格式：

```text
id: 10
event: reply_succeeded
data: {"id":10,"post_id":"20260612-001","job_id":2,"event_type":"reply_succeeded","payload":{},"created_at":1710000000.0}
```

事件类型：

`post_created`、`embedding_started`、`embedding_succeeded`、`embedding_failed`、`reply_started`、`reply_succeeded`、`reply_failed`、`todo_started`、`todo_succeeded`、`todo_failed`、`light_reflection_started`、`light_reflection_succeeded`、`light_reflection_failed`、`deep_reflection_queued`、`deep_reflection_succeeded`、`deep_reflection_failed`、`pipeline_done`。

## 5. 图片附件

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/attachments/upload` | 上传 JPEG/PNG 图片，返回 Attachment |
| `GET` | `/attachments/{attachment_id}` | 读取图片文件 |

上传请求：

```bash
curl -F "file=@photo.jpg" http://127.0.0.1:8000/attachments/upload
```

限制：

- 单张原图最大 50MB。
- 解码安全上限为 60MP / 单边 12000px。
- 后端会自动旋转方向、清理 EXIF，并尽量压缩到本地存储上限内。
- 附件上传后还只是孤儿附件；只有出现在 `attachment_ids` 中随 post、chat message 或 comment message 提交后才会绑定。

## 6. 评论会话

评论会话使用 `(post_id, soul_name)` 作为会话键，不存在独立 thread 表。`seq=0` 是 SOUL 首评；后续用户追问和 SOUL 回复使用递增 `seq` 存在同一条流里。评论消息不进入 posts/FTS5，不触发 TodoTool 或轻反思，但会进入 ChromaDB 和对应 SOUL 深反思材料。

### Conversation 和 Message 对象

```json
{
  "post_id": "20260612-001",
  "soul_name": "默认",
  "root_comment_id": 1,
  "created_at": 1710000000.0,
  "updated_at": 1710000000.0,
  "last_message_at": 1710000000.0
}
```

```json
{
  "id": 3,
  "post_id": "20260612-001",
  "soul_name": "默认",
  "role": "user",
  "content": "你觉得下一步应该怎么拆？",
  "seq": 1,
  "metadata": null,
  "created_at": 1710000000.0,
  "edited_at": null,
  "rerun_at": null,
  "attachments": []
}
```

### 接口

| 方法 | 路径 | 参数/请求 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/comments/posts/{post_id}/conversations` | 无 | 列出该 post 下所有 SOUL 会话，按 post 的 SOUL 顺序快照排列 |
| `GET` | `/comments/posts/{post_id}/souls/{soul_name}` | `limit=30` | 获取单个 SOUL 的评论会话和最近消息 |
| `POST` | `/comments/posts/{post_id}/souls/{soul_name}/messages` | `{ "content": "...", "attachment_ids": [] }` | 发送用户追问并同步生成 SOUL 回复 |
| `DELETE` | `/comments/messages/{comment_id}` | 无 | 删除一条评论消息；可能级联删除后续回复 |
| `POST` | `/comments/messages/{comment_id}/rerun` | 无 | 重跑最近一条 assistant 评论 |
| `GET` | `/comments/posts/{post_id}/souls/{soul_name}/events` | SSE | 监听该评论会话中新消息 |

发送消息响应：

```json
{
  "conversation": { "post_id": "20260612-001", "soul_name": "默认", "root_comment_id": 1 },
  "result": {
    "post_id": "20260612-001",
    "soul_name": "默认",
    "ok": true,
    "reply": "可以先把下一步切成三块...",
    "user_message_id": 3,
    "assistant_message_id": 4,
    "error": null
  },
  "messages": []
}
```

评论 SSE 事件格式：

```text
id: 4
event: comment_message
data: {"id":4,"post_id":"20260612-001","soul_name":"默认","role":"assistant","content":"..."}
```

## 7. SOUL 私聊

私聊绑定单个 SOUL，使用 `chat_threads` / `chat_messages`。私聊不写 posts、不进 FTS5、不触发 TodoTool 或轻反思；会进入 ChromaDB，但只允许当前 SOUL 检索自己的私聊。

| 方法 | 路径 | 参数/请求 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/chat/{soul_name}/threads` | `all_souls=false` | 列出某个 SOUL 的私聊线程；`all_souls=true` 时忽略路径中的 SOUL 名 |
| `GET` | `/chat/threads/{thread_id}` | `limit=30`，可选 `before_message_id` | 获取线程和消息 |
| `POST` | `/chat/{soul_name}/messages` | `{ "content": "...", "attachment_ids": [] }` | 发送消息；没有线程时自动创建 |
| `PATCH` | `/chat/messages/{message_id}` | `{ "content": "...", "attachment_ids": [] }` | 编辑用户消息并重新生成回复 |
| `POST` | `/chat/messages/{message_id}/rerun` | 无 | 重跑 assistant 消息 |
| `GET` | `/chat/threads/{thread_id}/events` | SSE，可选 `after_id` 或 `Last-Event-ID` | 监听新私聊消息 |

发送消息响应：

```json
{
  "thread": {
    "id": 1,
    "soul_name": "默认",
    "title": null,
    "created_at": 1710000000.0,
    "updated_at": 1710000000.0,
    "last_message_at": 1710000000.0
  },
  "result": {
    "thread_id": 1,
    "soul_name": "默认",
    "ok": true,
    "reply": "我在。",
    "user_message_id": 10,
    "assistant_message_id": 11,
    "error": null
  },
  "messages": []
}
```

私聊 SSE 事件格式：

```text
id: 11
event: chat_message
data: {"id":11,"thread_id":1,"role":"assistant","content":"我在。"}
```

## 8. SOUL 管理与 SOUL 记忆

### SOUL 管理

| 方法 | 路径 | 请求/参数 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/souls` | `enabled_only=false` | 列出 SOUL |
| `POST` | `/souls` | `{ "name": "...", "soul": "...", "description": "...", "enabled": true }` | 创建 SOUL |
| `POST` | `/souls/generate-soul` | `{ "name": "...", "inspiration": "..." }` | 用 LLM 生成 SOUL Markdown |
| `PATCH` | `/souls/{name}` | `{ "soul": "...", "description": "...", "enabled": true }` | 更新 SOUL 文本、描述或启用状态 |
| `PATCH` | `/souls/{name}` | `{ "order": ["默认", "毒舌好友"] }` | 重排 SOUL；路径名只用于匹配 route，实际以 `order` 为准 |

SOUL 对象：

```json
{
  "name": "默认",
  "file_path": "workspace/souls/默认.md",
  "enabled": true,
  "sort_order": 0,
  "description": "温和稳定的陪伴者",
  "created_at": 1710000000.0,
  "updated_at": 1710000000.0
}
```

`/souls/generate-soul` 需要模型配置完成，响应：

```json
{
  "soul": "# 默认\n..."
}
```

### SOUL 记忆

| 方法 | 路径 | 参数/请求 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/souls/{name}/memory` | 无 | 读取 `soul_memories/<name>.md` |
| `PUT` | `/souls/{name}/memory` | `{ "content": "# ..." }` | 覆盖 SOUL 记忆并写 revision |
| `GET` | `/souls/{name}/memory/revisions` | `limit=20` | 列出 SOUL 记忆 revisions |
| `GET` | `/souls/{name}/memory/revisions/{revision_id}` | 无 | 获取 revision detail |

## 9. 用户长期记忆 Profile

| 方法 | 路径 | 参数/请求 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/profile` | 无 | 读取 `user.md` |
| `PUT` | `/profile` | `{ "content": "# ..." }` | 覆盖 `user.md` 并写 revision |
| `GET` | `/profile/revisions` | `limit=20`，可选 `source` | 列出 `user.md` revisions |
| `GET` | `/profile/revisions/{revision_id}` | 无 | 获取 revision detail |

更新响应：

```json
{
  "ok": true,
  "content": "# User Memory\n..."
}
```

## 10. Todo

Todo 由公开 post 中的 TodoTool 自动抽取，也可以通过 API 手动管理。私聊和评论不会自动抽取 Todo。

| 方法 | 路径 | 请求 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/todos` | 无 | 列出 todos |
| `POST` | `/todos` | `{ "task": "...", "date": "2026-06-12", "start_time": null, "end_time": null, "status": "open" }` | 创建 todo |
| `PATCH` | `/todos/{todo_id}` | 任意可更新字段 | 更新 todo |
| `DELETE` | `/todos/{todo_id}` | 无 | 删除 todo |

Todo 对象：

```json
{
  "id": "todo_...",
  "task": "改论文大纲",
  "date": "2026-06-12",
  "start_time": null,
  "end_time": null,
  "status": "open",
  "source_post": "20260612-001",
  "created_at": 1710000000.0,
  "updated_at": 1710000000.0,
  "completed_at": null
}
```

## 11. 反思

反思触发接口只负责预览范围或排后台 job；实际执行由 API worker 领取 `jobs`。

| 方法 | 路径 | 参数/请求 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/reflections/global/preview` | `limit=100`，范围 1-500 | 预览全局深反思将处理的公开 posts |
| `POST` | `/reflections/global` | `{ "limit": 100 }` | 排全局深反思 job |
| `GET` | `/reflections/souls/preview` | `limit_per_soul=100`，范围 1-500 | 预览各 SOUL 深反思将处理的互动 |
| `POST` | `/reflections/souls` | `{ "limit_per_soul": 100 }` | 排 SOUL 深反思 job |

全局预览响应：

```json
{
  "post_ids": ["20260612-001"],
  "scope_start": "2026-06-12 10:00:00",
  "scope_end": "2026-06-12 10:05:00"
}
```

SOUL 预览响应：

```json
[
  {
    "soul_name": "默认",
    "interaction_count": 12,
    "scope_start": 1710000000.0,
    "scope_end": 1710000100.0
  }
]
```

触发响应：

```json
{
  "job_id": 12,
  "status": "queued"
}
```

## 12. Jobs

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/jobs` | 可选 `status`，`job_type`，`limit=50`，`offset=0` | 列出后台 jobs |
| `GET` | `/jobs/{job_id}` | 无 | 获取单个 job |
| `POST` | `/jobs/{job_id}/retry` | 无 | 将 failed job 复制为新 pending job |
| `POST` | `/jobs/{job_id}/cancel` | 无 | 取消 pending job |

重试响应：

```json
{
  "job_id": 13,
  "status": "queued"
}
```

取消响应：

```json
{
  "job_id": 12,
  "status": "cancelled"
}
```

## 13. 设置与 Workspace 状态

### 模型配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/settings/model` | 读取模型、Embedding、日志、Vision、网页搜索配置状态 |
| `PUT` | `/settings/model` | 保存配置，并尝试热重载 API runtime |

保存请求：

```json
{
  "api_key": "sk-...",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "embedding_model": "text-embedding-3-small",
  "embedding_api_key": null,
  "embedding_base_url": null,
  "reuse_embedding_config": true,
  "logging": {
    "enabled": true,
    "level": "INFO",
    "history_retention": 100
  },
  "vision": {
    "enabled": false,
    "model": null,
    "api_key": null,
    "base_url": null
  },
  "web_search": {
    "enabled": false,
    "provider": "duckduckgo",
    "tavily_api_key": null,
    "max_results": 5,
    "timeout_s": 8,
    "cache_ttl_s": 1800
  }
}
```

注意：

- `api_key` 为空时会沿用已有值；如果没有已有值则返回 `422`。
- `reuse_embedding_config=true` 时，Embedding 复用主模型的 key/base URL。
- `web_search.max_results` 范围 1-8，`timeout_s` 范围 3-20，`cache_ttl_s` 范围 0-86400。
- 保存后响应会带 `runtime_reloaded`、`restart_required`、`config_reloaded`，失败时带 `reload_error`。

### Workspace 状态和向量索引

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/settings/workspace` | 读取 workspace 路径、表计数、日志状态、向量索引状态 |
| `POST` | `/settings/vector-index/retry` | 重试 pending/failed 向量文档同步 |
| `POST` | `/settings/vector-index/reconcile` | 重建向量文档账本并对账 ChromaDB |

向量索引操作响应：

```json
{
  "processed": 3,
  "vector_index": {
    "collection_name": "tracelog_...",
    "embedding_config_hash": "...",
    "source_revision": 42,
    "synced_revision": 42,
    "ready": true,
    "pending_count": 0,
    "failed_count": 0,
    "missing_count": 0,
    "stale_count": 0
  }
}
```

## 14. Evidence Feedback

回复消息的 `metadata` 里可能包含 evidence snapshot。前端引用记忆面板可以把某条 evidence 标记为不相关。

`POST /feedback/evidence`

请求：

```json
{
  "channel": "comment",
  "message_id": 4,
  "doc_id": "post:20260612-001",
  "verdict": "irrelevant"
}
```

字段：

- `channel`：`chat` / `comment` / `public_post`
- `message_id`：对应消息 id，必须大于 0
- `doc_id`：evidence 文档 id
- `verdict`：当前只支持 `irrelevant`

响应：

```json
{
  "id": 1,
  "channel": "comment",
  "message_id": 4,
  "doc_id": "post:20260612-001",
  "verdict": "irrelevant",
  "created_at": 1710000000.0,
  "created": true
}
```

## 15. 调用顺序参考

### 首次配置

1. `GET /health` 检查 `configured=false`。
2. `GET /settings/model` 读取默认配置状态。
3. `PUT /settings/model` 保存主模型和 Embedding。
4. `GET /health` 确认 `configured=true`。

### 公开发帖

1. 可选：`POST /attachments/upload` 上传图片。
2. `POST /posts` 创建 post，拿到 `post_id` 和 `job_ids`。
3. `GET /posts/{post_id}/events` 监听 pipeline。
4. `GET /posts/{post_id}` 读取首评、jobs 和事件历史。
5. 如果某个 job failed，可用 `POST /jobs/{job_id}/retry` 重试。

### 评论追问

1. `GET /comments/posts/{post_id}/conversations` 读取 SOUL 会话入口。
2. `GET /comments/posts/{post_id}/souls/{soul_name}` 读取消息。
3. `POST /comments/posts/{post_id}/souls/{soul_name}/messages` 发送追问并生成回复。
4. 可选：`GET /comments/posts/{post_id}/souls/{soul_name}/events` 监听增量消息。

### 私聊

1. `GET /souls?enabled_only=true` 选择 SOUL。
2. `GET /chat/{soul_name}/threads` 读取线程。
3. `POST /chat/{soul_name}/messages` 发送消息；没有线程时自动创建。
4. `GET /chat/threads/{thread_id}/events` 监听增量消息。
