import { useState } from 'react'
import {
  type ReflectionScope,
  type SoulReflectionScope,
  type Todo,
} from '@/api/client'
import { isTodoDone, getTodayKey } from '@/utils/todo'
import { formatDateScope, formatUnixScope } from '@/utils/date'
import styles from './RightPanel.module.css'

interface RightPanelProps {
  todos: Todo[]
  globalReflection: ReflectionScope | null
  soulReflections: SoulReflectionScope[]
  onTodoToggle: (todo: Todo) => Promise<void> | void
  onOpenTodos: () => void
  onOpenReflections: () => void
}

export function RightPanel({
  todos,
  globalReflection,
  soulReflections,
  onTodoToggle,
  onOpenTodos,
  onOpenReflections,
}: RightPanelProps) {
  return (
    <div className={styles.panel}>
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
  const todayTodos = selectTodayTodos(todos)

  const completeTodo = async (todo: Todo) => {
    setSavingId(todo.id)
    setError(null)
    try {
      await onTodoToggle(todo)
    } catch {
      setError('更新失败，稍后再试')
    } finally {
      setSavingId(null)
    }
  }

  return (
    <section className={styles.card}>
      <PanelHeader title="今日待办" />
      <div className={styles.itemList}>
        {todayTodos.length > 0 ? (
          todayTodos.map((todo) => {
            const meta = todoMeta(todo)

            return (
              <div key={todo.id} className={styles.todoItem}>
                <button
                  type="button"
                  className={styles.todoCheckbox}
                  disabled={savingId === todo.id}
                  aria-label={`完成待办：${todo.task}`}
                  onClick={() => completeTodo(todo)}
                >
                  {savingId === todo.id ? '...' : ''}
                </button>
                <div className={styles.todoBody}>
                  <p>{todo.task}</p>
                  {meta && <span>{meta}</span>}
                </div>
              </div>
            )
          })
        ) : (
          <p className={styles.empty}>今天没有待办</p>
        )}
        <button type="button" className={styles.queueAction} onClick={onOpenTodos}>
          查看
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
      <PanelHeader title="待整理线索" />
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
          <p className={styles.empty}>没有待整理线索</p>
        )}
        <button type="button" className={styles.queueAction} onClick={onOpenReflections}>
          查看
        </button>
      </div>
    </section>
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
  if (todo.date && time) return `${todo.date} ${time}`
  return todo.date || time
}

function formatSoulScope(soulReflections: SoulReflectionScope[]): string {
  const activeScopes = soulReflections.filter((scope) => scope.interaction_count > 0)
  if (activeScopes.length === 0) return '等待整理'
  const start = Math.min(...activeScopes.map((scope) => scope.scope_start))
  const end = Math.max(...activeScopes.map((scope) => scope.scope_end))
  return formatUnixScope(start, end)
}
