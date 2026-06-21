import { useEffect, useRef, useState } from 'react'
import {
  type ReflectionScope,
  type SoulReflectionScope,
  type Todo,
} from '@/api/client'
import { isTodoDone, getTodayKey } from '@/utils/todo'
import { formatDateLabel, formatDateScope, formatUnixScope } from '@/utils/date'
import { ChevronRightIcon } from '@/components/icons'
import styles from './RightPanel.module.css'

interface PendingCompletedTodo {
  todo: Todo
  timeoutId: number
}

interface RightPanelProps {
  todos: Todo[]
  globalReflection: ReflectionScope | null
  soulReflections: SoulReflectionScope[]
  searchQuery: string
  onSearchQueryChange: (value: string) => void
  onTodoToggle: (todo: Todo) => Promise<void> | void
  onOpenTodos: () => void
  onOpenReflections: () => void
}

export function RightPanel({
  todos,
  globalReflection,
  soulReflections,
  searchQuery,
  onSearchQueryChange,
  onTodoToggle,
  onOpenTodos,
  onOpenReflections,
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
      <ReflectionQueueCard
        globalReflection={globalReflection}
        soulReflections={soulReflections}
        onOpenReflections={onOpenReflections}
      />
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
      <PanelHeader title="待办速览" />
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
        <button type="button" className={styles.queueAction} onClick={onOpenTodos}>
          查看更多
          <ChevronRightIcon width={13} height={13} />
        </button>
        {error && <p className={styles.inlineError}>{error}</p>}
      </div>
    </section>
  )
}

function ReflectionQueueCard({
  globalReflection,
  soulReflections,
  onOpenReflections,
}: {
  globalReflection: ReflectionScope | null
  soulReflections: SoulReflectionScope[]
  onOpenReflections: () => void
}) {
  const globalCount = globalReflection?.post_ids.length ?? 0
  const soulCount = soulReflections.reduce(
    (total, scope) => total + Math.max(scope.interaction_count, 0),
    0,
  )
  const hasQueue = globalCount > 0 || soulCount > 0

  return (
    <section className={styles.card}>
      <PanelHeader title="待整理" />
      <div className={styles.itemList}>
        {hasQueue ? (
          <div className={styles.queueRows}>
            <QueueRow
              label="公开记录"
              count={globalCount}
              detail={formatDateScope(globalReflection?.scope_start, globalReflection?.scope_end)}
            />
            <QueueRow label="人格回应" count={soulCount} detail={formatSoulScope(soulReflections)} />
          </div>
        ) : (
          <p className={styles.empty}>没有待整理内容</p>
        )}
        <p className={styles.queueHint}>让 AI 阅读新增记录，更新对你的长期理解。</p>
        <button type="button" className={styles.queueAction} onClick={onOpenReflections}>
          查看更多
          <ChevronRightIcon width={13} height={13} />
        </button>
      </div>
    </section>
  )
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.35-4.35" />
    </svg>
  )
}

function PanelHeader({ title }: { title: string }) {
  return (
    <div className={styles.cardHeader}>
      <h3 className={styles.cardTitle}>{title}</h3>
    </div>
  )
}

function QueueRow({ label, count, detail }: { label: string; count: number; detail: string }) {
  return (
    <div className={styles.queueRow}>
      <div>
        <p>{label}</p>
        <span>{detail}</span>
      </div>
      <strong>{count} 条</strong>
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

function formatSoulScope(soulReflections: SoulReflectionScope[]): string {
  const activeScopes = soulReflections.filter((scope) => scope.interaction_count > 0)
  if (activeScopes.length === 0) return '等待整理'
  const start = Math.min(...activeScopes.map((scope) => scope.scope_start))
  const end = Math.max(...activeScopes.map((scope) => scope.scope_end))
  return formatUnixScope(start, end)
}
