# memory-v2 全链路

## Evidence

post/comment/chat 的 create/edit/rerun/delete 都追加不可变 event。图片摘要使用
`post_vision` source。每个 source 有独立递增 revision，delete 也保留版本记录。

## Reconcile

runner 找出 cursor 之后仍有 event，或存在 challenged unit 的 buckets。LLM 只接收当前
用户 evidence、当前 units 和 tombstones，并输出 add/retain/confirm/revise/retract。
系统验证 event 属于当前批次且不跨 bucket，然后原子写入 unit、operation、cursor 和
reconcile run。

用户编辑 unit 时，旧 evidence 进入 `review_pending`，独立 relink judge 决定保留或
移除链接。用户创建的 unit 以用户断言本身成立，不会因原始 source 删除而自动消失。

## Portrait

`tier=core`、重要度和置信度达到阈值的 units 才能进入画像。`portrait_policy` 可强制
纳入或排除，`prompt_policy=no_prompt` 会从所有回复 prompt 中禁用该 unit。

## Read model

- 用户基线画像：global/public `user_portrait`
- 当前状态：有时效窗口的 state units
- 相关记忆：scope-filtered unit retrieval
- 对话回想：命中 unit 的原始 evidence 周边
- 最新动态：cursor 之后的近期 user evidence
- SOUL 关系：聚合当前 SOUL 的 thread/private relationship units

## 删除与修订

source edit/delete 会 challenge 所有引用该 source 当前版本的 units。下一轮必须为每个
challenged unit 给出唯一决定。撤回后的 unit 保留 operation history 和 tombstone，
防止错误信念被重复生成。

用户侧的撤回叫「忘记」：只翻 unit 状态并更新画像，原始 post/chat 证据永不删除。
两种原因语义不同——`outdated`（默认）表示"曾经成立、现在过时"，允许新证据重建同一
信念；`false` 表示"从来不成立"，tombstone 永久阻止再生。UI 默认落在 outdated，
因为误标代价不对称：误标过时可逆，误标 false 是静默且永久的封杀。
