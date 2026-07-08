# API 概览

后端是 FastAPI，默认 `127.0.0.1:8000`。这里只列记忆工作台的完整端点和其他资源的入口；请求/响应细节看 `api/routes/` 源码。

## 记忆工作台 `/memory`

前端"记忆"页面的全部能力都来自这组端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/memory/status` | 待整理证据数、待重判数、视图新鲜度、进行中任务 |
| POST | `/memory/reconcile` | 手动触发一次记忆整理（自动去重） |
| GET | `/memory/reconcile-runs` | 整理运行历史 |
| GET | `/memory/operations` | unit 操作审计 |
| GET | `/memory/units` | 按桶 / 状态 / 类型查询记忆 |
| POST | `/memory/units` | 用户手动创建一条记忆 |
| GET | `/memory/units/{id}` | 记忆详情与其证据 |
| PATCH | `/memory/units/{id}` | 编辑记忆（后台自动重挂证据） |
| DELETE | `/memory/units/{id}` | 「忘记」：outdated（默认，可凭新证据重建）或 false（永久压制） |
| POST | `/memory/units/{id}/restore` | 找回用户忘记的记忆 |
| GET | `/memory/source-impact` | 某条内容当前支撑几条记忆（编辑/删除前预告） |
| GET/POST | `/memory/revisit-policy` | 回访开关（「以后不用问这类差异」） |
| POST | `/memory/units/{id}/prompt-policy` | 是否允许在回复中提起 |
| POST | `/memory/units/{id}/portrait-policy` | 画像纳入：自动 / 强制包含 / 强制排除 |
| GET | `/memory/views` | 列出画像视图 |
| POST | `/memory/views/resynthesize` | 手动重新综合某个画像 |

## 其他资源

- `/posts`、`/comments`、`/chat`：发帖、评论、私聊
- `/attachments`：图片上传
- `/souls`：人格管理
- `/todos`、`/goals`、`/suggestions`：待办 / 目标 / 建议
- `/jobs`：后台任务状态
- `/feedback/evidence`：引用记忆的点踩反馈
- `/settings`：模型与运行配置

发帖后的进度通过 SSE 推送（post_created、embedding、reply、todo、pipeline_done）；记忆整理进度查 `/memory/status` 与 `/jobs/{id}`。
