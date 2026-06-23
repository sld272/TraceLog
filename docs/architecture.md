# TraceLog 架构

```text
Web / CLI
  -> API routes / CLI commands
  -> core services
  -> SQLite truth + vector outbox
  -> background jobs
       -> embedding
       -> replies
       -> todo
       -> memory reconcile
```

## memory-v2 写链

```text
business mutation
  -> append immutable evidence event in same transaction
  -> enqueue one deduplicated reconcile job
  -> scan pending owner/visibility buckets
  -> LLM emits unit operations outside transaction
  -> validate evidence and boundary
  -> commit units + operation log + cursor together
  -> refresh portrait views
  -> rebuild unit vector documents
```

## memory-v2 读链

```text
reply request
  -> global user portrait
  -> admissible recent state
  -> query-relevant units
  -> raw conversation recall around matched evidence
  -> pending recent evidence
  -> precedence and disclosure rules
```

当前 prompt 中已有的消息 source 会从 pending evidence 中排除。

## 模块

- `core/memory_events_service.py`：evidence ledger 与 cursor
- `core/memory_reconciler.py`：单 bucket 原子对账
- `core/memory_reconcile_runner.py`：全局调度与 relink
- `core/memory_unit_service.py`：unit、evidence link、operation log
- `core/memory_view_service.py`：用户画像
- `core/soul_relationship_memory.py`：SOUL 关系画像
- `core/memory_read.py`：scope-filtered prompt 读模型
- `core/llm/memory_router.py`：对账、relink、画像综合提示词
- `api/routes/memory.py`：记忆工作台 API
