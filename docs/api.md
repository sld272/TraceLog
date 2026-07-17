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

## 日程 `/schedule`

日程以 Microsoft Graph 为真相源，本组端点负责设备码登录、本地缓存读取、写穿和手动同步。

| 方法 | 路径 | 请求 / 说明 |
|---|---|---|
| GET | `/schedule/status` | 返回 `configured`、`connected`、账户、上次同步时间与当前同步窗口 |
| GET | `/schedule/auth/client-id` | 返回是否已配置及 client ID 尾 4 位，不回显完整 ID |
| POST | `/schedule/auth/client-id` | `{client_id}`；保存新 ID，替换已有 ID 时先退出旧登录并清缓存 |
| POST | `/schedule/auth/device-start` | 启动设备码流，返回 `user_code`、`verification_uri`、`expires_in`；已有登录流程时返回 409 |
| GET | `/schedule/auth/device-status` | 返回 `pending / ok / error`，成功时附账户，失败时附安全错误信息 |
| POST | `/schedule/auth/logout` | 取消等待中的登录，清 token、delta 状态和事件缓存 |
| GET | `/schedule/events?start=YYYY-MM-DD&end=YYYY-MM-DD` | 从本地缓存返回与闭区间相交的未取消事件数组；每项含 `goal_links`。连接状态放在 `X-Schedule-Configured`、`X-Schedule-Connected` 响应头 |
| POST | `/schedule/events` | `{subject,date,start_time?,end_time?,all_day?,goal_id?,account_id?,client_request_id?}`；写穿 Graph，可在创建时绑定目标；前端会在同一次创建及其重试中复用 `client_request_id`，避免 Outlook 重复创建；未连接返回 409 |
| PATCH | `/schedule/events/{event_id}` | 可更新 `subject / date / start_time / end_time / all_day`，写穿 Graph 后刷新缓存 |
| DELETE | `/schedule/events/{event_id}` | 从 Graph 删除事件，并清本地缓存与目标链接 |
| POST | `/schedule/sync` | 手动执行 delta 同步，返回 `ok`、连接状态、`upserted`、`deleted`、`last_sync_at`；未连接时 `ok=false` |

日期校验和日程的本地时间语义统一使用 `Asia/Shanghai`。创建非全天事件时，未给开始时间则默认 09:00，未给结束时间则默认开始后 1 小时；全天事件不需要时间。

## 日期活跃度 `/posts/activity`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/posts/activity?start=YYYY-MM-DD&end=YYYY-MM-DD` | 返回区间内的 `[{id, ts}]`；服务端不做日期分桶，前端按浏览器本地时区统计每日帖子密度 |

## 目标日程 `/goals/{goal_id}/schedule`

目标属于 TraceLog，本组端点只维护本地链接和期望，不把目标写入 Exchange。

| 方法 | 路径 | 请求 / 说明 |
|---|---|---|
| GET | `/goals/{goal_id}/schedule` | 返回已绑定、未取消的事件列表和本周进度 |
| POST | `/goals/{goal_id}/schedule/links` | `{event_id}`；将已有缓存事件绑定到目标，幂等写入 |
| DELETE | `/goals/{goal_id}/schedule/links/{event_id}` | 解除目标与事件的链接 |
| PUT | `/goals/{goal_id}/schedule/expectation` | `{period:"week",target,label}`；更新每周期望并返回最新进度 |

## 其他资源

- `/posts`、`/comments`、`/chat`：发帖、评论、私聊
- `/attachments`：图片上传
- `/souls`：人格管理
- `/goals`、`/suggestions`：目标与目标建议
- `/jobs`：后台任务状态
- `/feedback/evidence`：引用记忆的点踩反馈
- `/settings`：模型与运行配置

发帖后的进度通过 SSE 推送（`post_created`、embedding / reply 的 started / succeeded / failed、`pipeline_done`）；记忆整理进度查 `/memory/status` 与 `/jobs/{id}`。
