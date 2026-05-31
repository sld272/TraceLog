import { useCallback, useEffect, useState } from 'react'
import { type Todo, listTodos, updateTodo } from '@/api/client'
import styles from './WorkspacePages.module.css'

export function TodosPage() {
  const [todos, setTodos] = useState<Todo[]>([])
  const [loading, setLoading] = useState(true)
  const [savingId, setSavingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const fetchTodos = useCallback(async () => {
    try {
      setLoading(true)
      const data = await listTodos()
      setTodos(data)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchTodos()
  }, [fetchTodos])

  const toggleTodo = async (todo: Todo) => {
    const nextStatus = isDone(todo) ? '未完成' : '已完成'
    setSavingId(todo.id)
    try {
      const updated = await updateTodo(todo.id, { status: nextStatus })
      setTodos((prev) => prev.map((item) => (item.id === todo.id ? updated : item)))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新失败')
    } finally {
      setSavingId(null)
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleGroup}>
          <h1 className={styles.title}>待办</h1>
          <p className={styles.subtitle}>{todos.filter((todo) => !isDone(todo)).length} 个未完成</p>
        </div>
        <button className={styles.ghostButton} onClick={fetchTodos} disabled={loading}>
          刷新
        </button>
      </header>

      {error && <div className={styles.notice}>{error}</div>}

      {loading ? (
        <div className={styles.empty}>加载中...</div>
      ) : todos.length === 0 ? (
        <div className={styles.empty}>暂无待办</div>
      ) : (
        <div className={styles.stack}>
          {todos.map((todo) => {
            const done = isDone(todo)
            return (
              <article key={todo.id} className={styles.todoItem}>
                <button
                  className={`${styles.todoCheckbox} ${done ? styles.done : ''}`}
                  onClick={() => toggleTodo(todo)}
                  disabled={savingId === todo.id}
                  aria-label={done ? '标记为未完成' : '标记为已完成'}
                >
                  <CheckIcon />
                </button>
                <div className={styles.todoBody}>
                  <p className={`${styles.todoTask} ${done ? styles.done : ''}`}>{todo.task}</p>
                  <div className={styles.todoMeta}>
                    <span className={styles.pill}>{todo.status}</span>
                    {todo.date && <span>{formatDateTime(todo)}</span>}
                    {todo.source_post && <span>来自 {todo.source_post}</span>}
                  </div>
                </div>
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

function isDone(todo: Todo): boolean {
  return ['已完成', '完成', 'done', 'completed'].includes(todo.status)
}

function formatDateTime(todo: Todo): string {
  const time = [todo.start_time, todo.end_time].filter(Boolean).join(' - ')
  return time ? `${todo.date} ${time}` : todo.date ?? ''
}

function CheckIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6L9 17l-5-5" />
    </svg>
  )
}
