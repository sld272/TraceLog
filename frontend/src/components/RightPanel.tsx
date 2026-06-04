import { useState } from 'react'
import {
  type ReflectionScope,
  type SoulReflectionScope,
  type Todo,
} from '@/api/client'
import styles from './RightPanel.module.css'

interface RightPanelProps {
  profileContent: string | null
  todos: Todo[]
  globalReflection: ReflectionScope | null
  soulReflections: SoulReflectionScope[]
  onTodoToggle: (todo: Todo) => Promise<void> | void
  onOpenReflections: () => void
}

export function RightPanel({
  profileContent,
  todos,
  globalReflection,
  soulReflections,
  onTodoToggle,
  onOpenReflections,
}: RightPanelProps) {
  return (
    <div className={styles.panel}>
      <FocusCard profileContent={profileContent} />
      <TodayTodosCard todos={todos} onTodoToggle={onTodoToggle} />
      <ReflectionQueueCard
        globalReflection={globalReflection}
        soulReflections={soulReflections}
        onOpenReflections={onOpenReflections}
      />
    </div>
  )
}

function FocusCard({ profileContent }: { profileContent: string | null }) {
  const focusItems = extractCurrentFocusItems(profileContent)

  return (
    <section className={styles.card}>
      <PanelHeader eyebrow="Focus" title="当前关注" />
      <div className={styles.itemList}>
        {focusItems.length > 0 ? (
          focusItems.map((item, index) => (
            <div key={`${item}-${index}`} className={styles.focusItem}>
              <span className={styles.focusMarker} aria-hidden="true" />
              <span>{item}</span>
            </div>
          ))
        ) : (
          <p className={styles.empty}>还没有当前关注</p>
        )}
      </div>
    </section>
  )
}

function TodayTodosCard({
  todos,
  onTodoToggle,
}: {
  todos: Todo[]
  onTodoToggle: (todo: Todo) => Promise<void> | void
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
      <PanelHeader eyebrow="Today" title="今日待办" />
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
      <PanelHeader eyebrow="Queue" title="待整理线索" />
      <div className={styles.itemList}>
        {hasQueue ? (
          <>
            <div className={styles.queueRows}>
              <QueueRow
                label="公开记录"
                count={globalCount}
                detail={formatScope(globalReflection?.scope_start, globalReflection?.scope_end)}
              />
              <QueueRow label="人格互动" count={soulCount} detail={formatSoulScope(soulReflections)} />
            </div>
            <button type="button" className={styles.queueAction} onClick={onOpenReflections}>
              查看
            </button>
          </>
        ) : (
          <p className={styles.empty}>没有待整理线索</p>
        )}
      </div>
    </section>
  )
}

function PanelHeader({ eyebrow, title }: { eyebrow: string; title: string }) {
  return (
    <div className={styles.cardHeader}>
      <span className={styles.eyebrow}>{eyebrow}</span>
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

function extractCurrentFocusItems(content: string | null): string[] {
  if (!content) return []
  const lines = content.split('\n')
  const focusIndex = lines.findIndex((line) => line.trim() === '## 当前状态与关注')
  if (focusIndex < 0) return []
  const items: string[] = []

  for (const rawLine of lines.slice(focusIndex + 1)) {
    if (items.length >= 3) break
    const line = rawLine.trim()
    if (/^##\s+/.test(line)) break
    if (!line || /^#{1,6}\s/.test(line)) continue
    const cleaned = line
      .replace(/^[-*+]\s+/, '')
      .replace(/^\d+[.)、]\s*/, '')
      .replace(/^\[[ xX]\]\s*/, '')
      .trim()
    if (cleaned && !/^#{1,6}\s/.test(cleaned)) items.push(cleaned)
  }

  return items
}

function isDone(todo: Todo): boolean {
  return ['已完成', '完成', 'done', 'completed'].includes(todo.status)
}

function selectTodayTodos(todos: Todo[]): Todo[] {
  const active = todos.filter((todo) => !isDone(todo))
  const today = active.filter((todo) => todo.date === getTodayKey())
  const undated = active.filter((todo) => !todo.date)
  return [...today, ...undated].slice(0, 3)
}

function todoMeta(todo: Todo): string {
  const time = [todo.start_time, todo.end_time].filter(Boolean).join(' - ')
  if (todo.date && time) return `${todo.date} ${time}`
  return todo.date || time
}

function getTodayKey(): string {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function formatScope(start: string | null | undefined, end: string | null | undefined): string {
  const startText = formatDate(start)
  const endText = formatDate(end)
  if (startText === '-' && endText === '-') return '等待整理'
  return `${startText} - ${endText}`
}

function formatSoulScope(soulReflections: SoulReflectionScope[]): string {
  const activeScopes = soulReflections.filter((scope) => scope.interaction_count > 0)
  if (activeScopes.length === 0) return '等待整理'
  const start = Math.min(...activeScopes.map((scope) => scope.scope_start))
  const end = Math.max(...activeScopes.map((scope) => scope.scope_end))
  return formatUnixScope(start, end)
}

function formatUnixScope(start: number, end: number): string {
  return `${formatDateFromMs(start * 1000)} - ${formatDateFromMs(end * 1000)}`
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '-'
  return formatDateFromMs(date.getTime())
}

function formatDateFromMs(value: number): string {
  return new Date(value).toLocaleDateString('zh-CN', {
    month: 'short',
    day: 'numeric',
  })
}
