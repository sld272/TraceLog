import { useCallback, useEffect, useMemo, useState } from 'react'
import { type Todo, createTodo, deleteTodo, listTodos, updateTodo } from '@/api/client'
import styles from './WorkspacePages.module.css'

type TodoStatus = '未完成' | '已完成'
type DrawerMode = 'create' | 'edit'

interface TodoForm {
  task: string
  date: string
  start_time: string
  end_time: string
  status: TodoStatus
}

interface TodoGroup {
  key: string
  title: string
  countLabel?: string
  todos: Todo[]
}

const EMPTY_FORM: TodoForm = {
  task: '',
  date: '',
  start_time: '',
  end_time: '',
  status: '未完成',
}

export function TodosPage() {
  const [todos, setTodos] = useState<Todo[]>([])
  const [loading, setLoading] = useState(true)
  const [savingId, setSavingId] = useState<string | null>(null)
  const [savingDrawer, setSavingDrawer] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteArmed, setDeleteArmed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [drawerMode, setDrawerMode] = useState<DrawerMode | null>(null)
  const [selectedTodoId, setSelectedTodoId] = useState<string | null>(null)
  const [form, setForm] = useState<TodoForm>(EMPTY_FORM)
  const groups = useMemo(() => groupTodos(todos), [todos])
  const selectedTodo = selectedTodoId ? todos.find((todo) => todo.id === selectedTodoId) ?? null : null
  const activeCount = todos.filter((todo) => !isDone(todo)).length
  const todayCount = groups.find((group) => group.key === 'today')?.todos.length ?? 0
  const undatedCount = groups.find((group) => group.key === 'undated')?.todos.length ?? 0
  const completedCount = groups.find((group) => group.key === 'completed')?.todos.length ?? 0
  const drawerOpen = drawerMode !== null

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

  const openCreateDrawer = () => {
    setDrawerMode('create')
    setSelectedTodoId(null)
    setForm(EMPTY_FORM)
    setDeleteArmed(false)
    setError(null)
  }

  const openEditDrawer = (todo: Todo) => {
    setDrawerMode('edit')
    setSelectedTodoId(todo.id)
    setForm(formFromTodo(todo))
    setDeleteArmed(false)
    setError(null)
  }

  const closeDrawer = () => {
    setDrawerMode(null)
    setSelectedTodoId(null)
    setForm(EMPTY_FORM)
    setDeleteArmed(false)
  }

  const toggleTodo = async (todo: Todo) => {
    const nextStatus = isDone(todo) ? '未完成' : '已完成'
    setSavingId(todo.id)
    setError(null)
    try {
      const updated = await updateTodo(todo.id, { status: nextStatus })
      setTodos((prev) => replaceTodo(prev, updated))
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新失败')
    } finally {
      setSavingId(null)
    }
  }

  const saveDrawer = async () => {
    const payload = formToPayload(form)
    if (!payload.task) {
      setError('任务内容不能为空')
      return
    }
    setSavingDrawer(true)
    setError(null)
    try {
      if (drawerMode === 'create') {
        const created = await createTodo(payload)
        setTodos((prev) => [...prev, created])
      } else if (drawerMode === 'edit' && selectedTodo) {
        const updated = await updateTodo(selectedTodo.id, payload)
        setTodos((prev) => replaceTodo(prev, updated))
      }
      closeDrawer()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSavingDrawer(false)
    }
  }

  const removeSelectedTodo = async () => {
    if (!selectedTodo) return
    if (!deleteArmed) {
      setDeleteArmed(true)
      return
    }
    setDeleting(true)
    setError(null)
    try {
      await deleteTodo(selectedTodo.id)
      setTodos((prev) => prev.filter((todo) => todo.id !== selectedTodo.id))
      closeDrawer()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleGroup}>
          <h1 className={styles.title}>待办</h1>
          <p className={styles.subtitle}>{activeCount} 个未完成</p>
        </div>
        <div className={styles.headerActions}>
          <button className={styles.ghostButton} onClick={fetchTodos} disabled={loading}>
            刷新
          </button>
          <button className={styles.button} onClick={openCreateDrawer}>
            <PlusIcon />
            新增待办
          </button>
        </div>
      </header>

      {error && <div className={styles.notice}>{error}</div>}

      {loading ? (
        <div className={styles.empty}>加载中...</div>
      ) : (
        <div className={`${styles.todoWorkspace} ${drawerOpen ? styles.drawerOpen : ''}`}>
          <section className={styles.todoListPanel}>
            <div className={styles.todoSummaryBar}>
              <div className={styles.todoSummaryPills}>
                <span className={styles.pill}>{activeCount} 个未完成</span>
                <span className={styles.pill}>{todayCount} 个今天</span>
                <span className={styles.pill}>{undatedCount} 个无日期</span>
              </div>
              <span className={styles.meta}>{completedCount} 个已完成</span>
            </div>

            <div className={styles.todoGroups}>
              {groups.map((group) => (
                <TodoGroupSection
                  key={group.key}
                  group={group}
                  savingId={savingId}
                  onToggle={toggleTodo}
                  onEdit={openEditDrawer}
                />
              ))}
            </div>
          </section>

          {drawerOpen && (
            <TodoDrawer
              mode={drawerMode}
              form={form}
              selectedTodo={selectedTodo}
              saving={savingDrawer}
              deleting={deleting}
              deleteArmed={deleteArmed}
              onChange={setForm}
              onClose={closeDrawer}
              onSave={saveDrawer}
              onDelete={removeSelectedTodo}
            />
          )}
        </div>
      )}
    </div>
  )
}

function TodoGroupSection({
  group,
  savingId,
  onToggle,
  onEdit,
}: {
  group: TodoGroup
  savingId: string | null
  onToggle: (todo: Todo) => void
  onEdit: (todo: Todo) => void
}) {
  const [expanded, setExpanded] = useState(group.key !== 'completed')
  const isCollapsible = group.key === 'completed' && group.todos.length > 0

  useEffect(() => {
    if (group.key === 'completed' && group.todos.length === 0) setExpanded(false)
  }, [group.key, group.todos.length])

  return (
    <section className={styles.todoGroup}>
      <div className={styles.todoGroupHeader}>
        <div>
          <h2>{group.title}</h2>
          {group.countLabel && <span>{group.countLabel}</span>}
        </div>
        {isCollapsible ? (
          <button className={styles.textButton} onClick={() => setExpanded((value) => !value)}>
            {expanded ? '折叠' : '展开'}
          </button>
        ) : group.key === 'completed' ? (
          null
        ) : (
          <span className={styles.groupCount}>{group.todos.length}</span>
        )}
      </div>

      {expanded && (
        group.todos.length > 0 ? (
          <div className={styles.todoRows}>
            {group.todos.map((todo) => (
              <TodoRow
                key={todo.id}
                todo={todo}
                saving={savingId === todo.id}
                onToggle={() => onToggle(todo)}
                onEdit={() => onEdit(todo)}
              />
            ))}
          </div>
        ) : (
          <p className={styles.groupEmpty}>没有待办</p>
        )
      )}
    </section>
  )
}

function TodoRow({
  todo,
  saving,
  onToggle,
  onEdit,
}: {
  todo: Todo
  saving: boolean
  onToggle: () => void
  onEdit: () => void
}) {
  const done = isDone(todo)
  const meta = todoMeta(todo)

  return (
    <article className={styles.todoItem}>
      <button
        className={`${styles.todoCheckbox} ${done ? styles.done : ''}`}
        onClick={onToggle}
        disabled={saving}
        aria-label={done ? '标记为未完成' : '标记为已完成'}
      >
        <CheckIcon />
      </button>
      <div className={styles.todoBody}>
        <p className={`${styles.todoTask} ${done ? styles.done : ''}`}>{todo.task}</p>
        <div className={styles.todoMeta}>
          {meta.map((item) => (
            <span key={item} className={item === '已过期' ? styles.overdueMeta : undefined}>
              {item}
            </span>
          ))}
        </div>
      </div>
      <button className={styles.todoEditButton} onClick={onEdit}>
        编辑
      </button>
    </article>
  )
}

function TodoDrawer({
  mode,
  form,
  selectedTodo,
  saving,
  deleting,
  deleteArmed,
  onChange,
  onClose,
  onSave,
  onDelete,
}: {
  mode: DrawerMode | null
  form: TodoForm
  selectedTodo: Todo | null
  saving: boolean
  deleting: boolean
  deleteArmed: boolean
  onChange: (form: TodoForm) => void
  onClose: () => void
  onSave: () => void
  onDelete: () => void
}) {
  const setField = <K extends keyof TodoForm>(key: K, value: TodoForm[K]) => {
    onChange({ ...form, [key]: value })
  }

  return (
    <aside className={styles.todoDrawer}>
      <div className={styles.drawerHeader}>
        <div>
          <h2>{mode === 'create' ? '新增待办' : '编辑待办'}</h2>
          {selectedTodo?.source_post && <p>来自记录</p>}
        </div>
        <button className={styles.ghostButton} onClick={onClose} disabled={saving || deleting}>
          关闭
        </button>
      </div>

      <label className={styles.field}>
        <span>任务内容</span>
        <textarea
          value={form.task}
          onChange={(event) => setField('task', event.target.value)}
          onInput={(event) => setField('task', event.currentTarget.value)}
          rows={4}
        />
      </label>
      <label className={styles.field}>
        <span>日期</span>
        <input
          type="date"
          value={form.date}
          onChange={(event) => setField('date', event.target.value)}
          onInput={(event) => setField('date', event.currentTarget.value)}
        />
      </label>
      <div className={styles.drawerFieldGrid}>
        <label className={styles.field}>
          <span>开始时间</span>
          <input
            type="time"
            value={form.start_time}
            onChange={(event) => setField('start_time', event.target.value)}
            onInput={(event) => setField('start_time', event.currentTarget.value)}
          />
        </label>
        <label className={styles.field}>
          <span>结束时间</span>
          <input
            type="time"
            value={form.end_time}
            onChange={(event) => setField('end_time', event.target.value)}
            onInput={(event) => setField('end_time', event.currentTarget.value)}
          />
        </label>
      </div>
      <label className={styles.field}>
        <span>状态</span>
        <select
          value={form.status}
          onChange={(event) => setField('status', event.target.value as TodoStatus)}
          onInput={(event) => setField('status', event.currentTarget.value as TodoStatus)}
        >
          <option value="未完成">未完成</option>
          <option value="已完成">已完成</option>
        </select>
      </label>

      <div className={styles.drawerActions}>
        {mode === 'edit' ? (
          <button
            className={styles.dangerButton}
            onClick={onDelete}
            disabled={saving || deleting}
          >
            {deleting ? '删除中...' : deleteArmed ? '确认删除' : '删除'}
          </button>
        ) : (
          <span />
        )}
        <button className={styles.button} onClick={onSave} disabled={saving || deleting}>
          {saving ? '保存中...' : '保存'}
        </button>
      </div>
    </aside>
  )
}

function groupTodos(todos: Todo[]): TodoGroup[] {
  const todayKey = getTodayKey()
  const active = todos.filter((todo) => !isDone(todo))
  const today = sortTodos(active.filter((todo) => todo.date === todayKey))
  const upcoming = sortTodos(active.filter((todo) => todo.date && todo.date !== todayKey), true)
  const undated = sortTodos(active.filter((todo) => !todo.date))
  const completed = sortTodos(todos.filter(isDone), true)

  return [
    { key: 'today', title: '今天', todos: today },
    { key: 'upcoming', title: '接下来', todos: upcoming },
    { key: 'undated', title: '无日期', todos: undated },
    { key: 'completed', title: '已完成', countLabel: `${completed.length} 个`, todos: completed },
  ]
}

function sortTodos(todos: Todo[], datedFirst = false): Todo[] {
  return [...todos].sort((a, b) => {
    const dateA = a.date ?? (datedFirst ? '9999-99-99' : '')
    const dateB = b.date ?? (datedFirst ? '9999-99-99' : '')
    if (dateA !== dateB) return dateA.localeCompare(dateB)
    const createdA = a.created_at ?? 0
    const createdB = b.created_at ?? 0
    if (createdA !== createdB) return createdA - createdB
    return a.id.localeCompare(b.id)
  })
}

function replaceTodo(todos: Todo[], updated: Todo): Todo[] {
  return todos.map((todo) => (todo.id === updated.id ? updated : todo))
}

function formFromTodo(todo: Todo): TodoForm {
  return {
    task: todo.task,
    date: todo.date ?? '',
    start_time: todo.start_time ?? '',
    end_time: todo.end_time ?? '',
    status: isDone(todo) ? '已完成' : '未完成',
  }
}

function formToPayload(form: TodoForm): Partial<Todo> & { task: string } {
  return {
    task: form.task.trim(),
    date: cleanOptional(form.date),
    start_time: cleanOptional(form.start_time),
    end_time: cleanOptional(form.end_time),
    status: form.status,
  }
}

function cleanOptional(value: string): string | null {
  const text = value.trim()
  return text || null
}

function isDone(todo: Todo): boolean {
  return ['已完成', '完成', 'done', 'completed'].includes(todo.status)
}

function todoMeta(todo: Todo): string[] {
  const items: string[] = []
  if (todo.date) {
    items.push(formatDateLabel(todo.date))
    if (!isDone(todo) && todo.date < getTodayKey()) items.push('已过期')
  } else {
    items.push('无日期')
  }
  const time = [todo.start_time, todo.end_time].filter(Boolean).join(' - ')
  if (time) items.push(time)
  items.push(isDone(todo) ? '已完成' : todo.status)
  items.push(todo.source_post ? '来自记录' : '手动新增')
  return items
}

function formatDateLabel(date: string): string {
  if (date === getTodayKey()) return '今天'
  const parsed = new Date(`${date}T00:00:00`)
  if (Number.isNaN(parsed.getTime())) return date
  return parsed.toLocaleDateString('zh-CN', {
    month: 'short',
    day: 'numeric',
  })
}

function getTodayKey(): string {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function CheckIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6L9 17l-5-5" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </svg>
  )
}
