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
}

export function RightPanel({
  profileContent,
  todos,
  globalReflection,
  soulReflections,
}: RightPanelProps) {
  const focusItems = extractFocusItems(profileContent).slice(0, 3)
  const todayTodos = todos
    .filter((todo) => isTodayTodo(todo) && !isDone(todo))
    .slice(0, 3)
  const reflectionItems = buildReflectionItems(globalReflection, soulReflections).slice(0, 3)

  return (
    <div className={styles.panel}>
      <section className={styles.card}>
        <PanelHeader eyebrow="Focus" title="当前关注" />
        <div className={styles.itemList}>
          {focusItems.length > 0 ? (
            focusItems.map((item, index) => (
              <div key={`${item}-${index}`} className={styles.textItem}>
                {item}
              </div>
            ))
          ) : (
            <p className={styles.empty}>user.md 里还没有可展示的当前关注</p>
          )}
        </div>
      </section>

      <section className={styles.card}>
        <PanelHeader eyebrow="Today" title="今日待办" />
        <div className={styles.itemList}>
          {todayTodos.length > 0 ? (
            todayTodos.map((todo) => (
              <div key={todo.id} className={styles.todoItem}>
                <span className={styles.todoDot} aria-hidden="true" />
                <div className={styles.todoBody}>
                  <p>{todo.task}</p>
                  {(todo.start_time || todo.end_time) && (
                    <span>{[todo.start_time, todo.end_time].filter(Boolean).join(' - ')}</span>
                  )}
                </div>
              </div>
            ))
          ) : (
            <p className={styles.empty}>今天没有未完成待办</p>
          )}
        </div>
      </section>

      <section className={styles.card}>
        <PanelHeader eyebrow="Reflection" title="最近反思" />
        <div className={styles.itemList}>
          {reflectionItems.length > 0 ? (
            reflectionItems.map((item) => (
              <div key={item.label} className={styles.reflectionItem}>
                <span className={styles.reflectionCount}>{item.count}</span>
                <div>
                  <p>{item.label}</p>
                  <span>{item.detail}</span>
                </div>
              </div>
            ))
          ) : (
            <p className={styles.empty}>暂无新的反思范围</p>
          )}
        </div>
      </section>
    </div>
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

function extractFocusItems(content: string | null): string[] {
  if (!content) return []
  const lines = content
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)

  const focusIndex = lines.findIndex((line) => /当前关注|关注|focus/i.test(line))
  const sourceLines = focusIndex >= 0 ? lines.slice(focusIndex + 1) : lines
  const items: string[] = []

  for (const line of sourceLines) {
    if (items.length >= 3) break
    if (/^#{1,6}\s/.test(line) && items.length > 0) break
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

function isTodayTodo(todo: Todo): boolean {
  if (!todo.date) return true
  return todo.date === getTodayKey()
}

function getTodayKey(): string {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function buildReflectionItems(
  globalReflection: ReflectionScope | null,
  soulReflections: SoulReflectionScope[],
) {
  const items: Array<{ label: string; detail: string; count: string }> = []
  const postCount = globalReflection?.post_ids.length ?? 0
  if (postCount > 0) {
    items.push({
      label: '全局画像等待整理',
      detail: formatScope(globalReflection?.scope_start, globalReflection?.scope_end),
      count: String(postCount),
    })
  }

  soulReflections
    .filter((scope) => scope.interaction_count > 0)
    .sort((a, b) => b.interaction_count - a.interaction_count)
    .forEach((scope) => {
      items.push({
        label: `${scope.soul_name} 的对话线索`,
        detail: formatUnixScope(scope.scope_start, scope.scope_end),
        count: String(scope.interaction_count),
      })
    })

  return items
}

function formatScope(start: string | null | undefined, end: string | null | undefined): string {
  const startText = formatDate(start)
  const endText = formatDate(end)
  if (startText === '-' && endText === '-') return '新的记录已进入反思范围'
  return `${startText} - ${endText}`
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
