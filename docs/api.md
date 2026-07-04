# API 概览

## Memory

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/memory/status` | pending events/reviews/relinks、stale views、active jobs |
| POST | `/memory/reconcile` | 排入去重后的 memory reconcile job |
| GET | `/memory/reconcile-runs` | 对账运行历史 |
| GET | `/memory/operations` | unit 操作审计 |
| GET | `/memory/units` | 按 owner/visibility/status/type 查询 units |
| POST | `/memory/units` | 创建 user-authored unit |
| GET | `/memory/units/{id}` | unit 与 evidence 详情 |
| PATCH | `/memory/units/{id}` | 编辑 unit 并排入 relink |
| DELETE | `/memory/units/{id}` | 用户「忘记」：按 outdated（默认，可凭新证据重建）/ false（永久压制）撤回 |
| POST | `/memory/units/{id}/restore` | 找回用户忘记的记忆（仅 retracted_by_user） |
| GET | `/memory/source-impact` | 某条源内容当前支撑的记忆数（编辑/删除预告用） |
| POST | `/memory/units/{id}/prompt-policy` | allow/no_prompt |
| POST | `/memory/units/{id}/portrait-policy` | auto/force_include/force_exclude |
| GET | `/memory/views` | 列出画像视图 |
| POST | `/memory/views/resynthesize` | 手动重综合指定视图 |

## 其他资源

- `/posts`、`/comments`、`/chat`
- `/attachments`
- `/souls`
- `/todos`、`/goals`、`/suggestions`
- `/jobs`
- `/feedback/evidence`
- `/settings`

公开 post SSE 事件包括 post_created、embedding、reply、todo 和 pipeline_done。
memory reconcile 状态通过 `/memory/status` 与 `/jobs/{id}` 查询。
