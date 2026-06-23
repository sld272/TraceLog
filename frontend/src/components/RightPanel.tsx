import { useEffect, useRef, useState } from 'react'
import {
  type MemoryOperation,
  type Todo,
  listMemoryOperations,
} from '@/api/client'
import { isTodoDone, getTodayKey } from '@/utils/todo'
import { formatDateLabel, formatSmartTime } from '@/utils/date'
import { ChevronRightIcon } from '@/components/icons'
import styles from './RightPanel.module.css'

interface PendingCompletedTodo {
  todo: Todo
  timeoutId: number
}

interface RightPanelProps {
  todos: Todo[]
  searchQuery: string
  onSearchQueryChange: (value: string) => void
  onTodoToggle: (todo: Todo) => Promise<void> | void
  onOpenTodos: () => void
  onOpenMemory: () => void
}

export function RightPanel({
  todos,
  searchQuery,
  onSearchQueryChange,
  onTodoToggle,
  onOpenTodos,
  onOpenMemory,
}: RightPanelProps) {
  return (
    <div className={styles.panel}>
      <div className={styles.panelSearch}>
        <SearchIcon />
        <input
          value={searchQuery}
          onChange={(event) => onSearchQueryChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') onSearchQueryChange('')
          }}
          placeholder="搜索动态…"
          aria-label="搜索动态"
        />
        {searchQuery && (
          <button className={styles.panelSearchClear} onClick={() => onSearchQueryChange('')} aria-label="清空搜索" title="清空搜索">
            ×
          </button>
        )}
      </div>
      <TodayTodosCard todos={todos} onTodoToggle={onTodoToggle} onOpenTodos={onOpenTodos} />
      <MemoryPulseCard onOpenMemory={onOpenMemory} />
    </div>
  )
}

function TodayTodosCard({
  todos,
  onTodoToggle,
  onOpenTodos,
}: {
  todos: Todo[]
  onTodoToggle: (todo: Todo) => Promise<void> | void
  onOpenTodos: () => void
}) {
  const [savingId, setSavingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pendingCompleted, setPendingCompleted] = useState<Record<string, PendingCompletedTodo>>({})
  const pendingTimeoutsRef = useRef<Record<string, number>>({})
  const todayTodos = selectTodayTodos(todos)
  const displayTodos = [
    ...todayTodos.filter((todo) => !pendingCompleted[todo.id]),
    ...Object.values(pendingCompleted).map((entry) => entry.todo),
  ]

  useEffect(() => {
    return () => {
      Object.values(pendingTimeoutsRef.current).forEach((timeoutId) => window.clearTimeout(timeoutId))
    }
  }, [])

  const removePendingCompleted = (todoId: string) => {
    setPendingCompleted((prev) => {
      const entry = prev[todoId]
      if (entry) window.clearTimeout(entry.timeoutId)
      delete pendingTimeoutsRef.current[todoId]
      const next = { ...prev }
      delete next[todoId]
      return next
    })
  }

  const holdCompletedTodo = (todo: Todo) => {
    const completedTodo = { ...todo, status: '已完成' }
    const timeoutId = window.setTimeout(() => {
      setPendingCompleted((prev) => {
        const next = { ...prev }
        delete next[todo.id]
        return next
      })
      delete pendingTimeoutsRef.current[todo.id]
    }, 3000)

    setPendingCompleted((prev) => {
      const existing = prev[todo.id]
      if (existing) window.clearTimeout(existing.timeoutId)
      pendingTimeoutsRef.current[todo.id] = timeoutId
      return {
        ...prev,
        [todo.id]: { todo: completedTodo, timeoutId },
      }
    })
  }

  const completeTodo = async (todo: Todo) => {
    setSavingId(todo.id)
    setError(null)
    try {
      await onTodoToggle(todo)
      holdCompletedTodo(todo)
    } catch {
      setError('更新失败，稍后再试')
    } finally {
      setSavingId(null)
    }
  }

  const undoCompleteTodo = async (todo: Todo) => {
    setSavingId(todo.id)
    setError(null)
    try {
      await onTodoToggle(todo)
      removePendingCompleted(todo.id)
    } catch {
      setError('撤销失败，稍后再试')
    } finally {
      setSavingId(null)
    }
  }

  return (
    <section className={styles.card}>
      <PanelHeader title="待办速览" onMore={onOpenTodos} />
      <div className={styles.itemList}>
        {displayTodos.length > 0 ? (
          displayTodos.map((todo) => {
            const meta = todoMeta(todo)
            const completedPending = Boolean(pendingCompleted[todo.id])

            return (
              <div
                key={todo.id}
                className={`${styles.todoItem} ${completedPending ? styles.todoItemCompleted : ''}`}
              >
                <button
                  type="button"
                  className={`${styles.todoCheckbox} ${completedPending ? styles.todoCheckboxDone : ''}`}
                  disabled={savingId === todo.id || completedPending}
                  aria-label={completedPending ? `已完成待办：${todo.task}` : `完成待办：${todo.task}`}
                  onClick={() => {
                    if (!completedPending) completeTodo(todo)
                  }}
                >
                  {savingId === todo.id ? '...' : completedPending ? '✓' : ''}
                </button>
                <div className={styles.todoBody}>
                  <p className={completedPending ? styles.todoDoneText : undefined}>{todo.task}</p>
                  {meta && <span>{meta}</span>}
                  {completedPending && (
                    <button
                      type="button"
                      className={styles.undoButton}
                      disabled={savingId === todo.id}
                      onClick={() => undoCompleteTodo(todo)}
                    >
                      撤销
                    </button>
                  )}
                </div>
              </div>
            )
          })
        ) : (
          <p className={styles.empty}>今天没有待办。记录里提到的事会自动出现在这里。</p>
        )}
        {error && <p className={styles.inlineError}>{error}</p>}
      </div>
    </section>
  )
}

function MemoryPulseCard({ onOpenMemory }: { onOpenMemory: () => void }) {
  const [operations, setOperations] = useState<MemoryOperation[]>([])

  useEffect(() => {
    let cancelled = false
    void listMemoryOperations(6)
      .then((data) => {
        if (!cancelled) setOperations(data.slice().reverse())
      })
      .catch(() => {
        /* 右栏保持安静：拿不到记忆变化时不打扰用户 */
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <section className={styles.card}>
      <PanelHeader title="最近记忆变化" onMore={onOpenMemory} />
      <div className={styles.itemList}>
        {operations.length > 0 ? (
          <div className={styles.pulseList}>
            {operations.map((operation) => (
              <button key={operation.id} type="button" className={styles.pulse} onClick={onOpenMemory}>
                <span className={styles.pulseTitle}>{operationTitle(operation)}</span>
                <span className={styles.pulseMeta}>{operationMeta(operation)}</span>
              </button>
            ))}
          </div>
        ) : (
          <p className={styles.empty}>还没有记忆变化。和 AI 好友多聊聊，TA 会慢慢记住你。</p>
        )}
      </div>
    </section>
  )
}

function operationTitle(operation: MemoryOperation): string {
  const content = operation.after?.content ?? operation.before?.content
  if (typeof content === 'string' && content.trim()) return content
  return `记忆 ${operation.unit_id}`
}

function operationMeta(operation: MemoryOperation): string {
  return `${operationLabel(operation.op, operation.actor)} · ${formatSmartTime(operation.created_at)}`
}

function operationLabel(op: string, actor: string): string {
  const labels: Record<string, string> = {
    add: '新增',
    confirm: '确认',
    revise: '修订',
    retract: '撤回',
    retain: '保留',
    challenge: '待复核',
    supersede: '合并',
    decay: '沉底',
    promote: '升为核心',
    relink: '重连证据',
    user_create: '手动新增',
    user_edit: '手动编辑',
    user_delete: '手动删除',
  }
  return labels[op] ?? (actor === 'user' ? '手动调整' : 'AI 对账')
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.35-4.35" />
    </svg>
  )
}

function PanelHeader({ title, onMore }: { title: string; onMore?: () => void }) {
  return (
    <div className={styles.cardHeader}>
      <h3 className={styles.cardTitle}>{title}</h3>
      {onMore && (
        <button className={styles.cardMore} type="button" onClick={onMore}>
          查看更多
          <ChevronRightIcon width={13} height={13} />
        </button>
      )}
    </div>
  )
}

function selectTodayTodos(todos: Todo[]): Todo[] {
  const active = todos.filter((todo) => !isTodoDone(todo))
  const today = active.filter((todo) => todo.date === getTodayKey())
  const undated = active.filter((todo) => !todo.date)
  return [...today, ...undated].slice(0, 3)
}

function todoMeta(todo: Todo): string {
  const time = [todo.start_time, todo.end_time].filter(Boolean).join(' - ')
  if (!todo.date) return time ? `无日期 · ${time}` : '无日期'
  const dateLabel = formatDateLabel(todo.date, getTodayKey())
  return time ? `${dateLabel} ${time}` : dateLabel
}
