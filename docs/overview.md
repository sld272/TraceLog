# TraceLog 系统概览

先用一分钟建立整体图景，细节见[架构文档](architecture.md)。

## 四条主链

1. **表达链**：用户发帖、评论、私聊，写入 SQLite。
2. **回应链**：查询改写 → 混合检索 → 上下文组装 → SOUL 生成回复。
3. **记忆链**：每次输入记成证据事件 → 后台整理成记忆单元 → 综合成画像。
4. **派生链**：待办/目标抽取、图片理解、向量索引、运行日志。

## 隐私边界模型

每条记忆都归属一个"桶"，由两个正交维度决定：

- `owner_scope`：这条记忆归谁管——`global`（关于用户本人的主记忆）或 `soul:<名>`（某个 AI 人格与用户的相处记忆）。
- `visibility_scope`：在哪些场合可见——`public`（公开）或 `private:soul:<名>`（只有该人格的私聊可用）。

举例：你在公开帖里说"在准备考研"，这个事实进 `global/public`，所有人格都能引用；你只在和某个人格私聊时说过的心事，留在它的私密桶里，别的人格永远看不到。数据库边界校验、检索策略和 prompt 组装三层共同保证不会串。

## 数据从哪里读

| 内容 | 真相源 |
|---|---|
| 原始表达 | posts / comments / chat_messages |
| 证据事件 | memory_ingest_events |
| 长期信念 | memory_units |
| 画像缓存 | memory_views |
| 目标 / 待办 | goals / todos |
| 向量账本 | vector_docs / vector_outbox |

SOUL 的 Markdown 文件只定义人格性格，不存储任何用户记忆。
