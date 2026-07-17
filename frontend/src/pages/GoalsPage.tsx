import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  type CreateScheduleEventInput,
  type Goal,
  type GoalHorizon,
  type GoalSchedule,
  type GoalStatus,
  type ScheduleEvent,
  createGoal,
  createScheduleEvent,
  deleteGoal,
  getGoalSchedule,
  linkGoalSchedule,
  listGoals,
  listScheduleEvents,
  markGoalProgress,
  unlinkGoalSchedule,
  updateGoal,
  updateGoalScheduleExpectation,
} from '@/api/client'
import { CollapsibleGroup } from '@/components/CollapsibleGroup'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Notice } from '@/components/Notice'
import { ScheduleEventDrawer } from '@/components/ScheduleEventDrawer'
import { PlusIcon } from '@/components/icons'
import { formatAbsoluteTime } from '@/utils/date'
import { eventDateKey, formatEventTime, localDateKey, monthDayLabel, todayKey } from '@/utils/schedule'
import styles from './WorkspacePages.module.css'

interface GoalForm {
  title: string
  detail: string
  horizon: GoalHorizon
  status: GoalStatus
  focus: boolean
}

const EMPTY_FORM: GoalForm = {
  title: '',
  detail: '',
  horizon: 'short',
  status: 'active',
  focus: true,
}

export function GoalsPage() {
  const [goals, setGoals] = useState<Goal[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [drawerMode, setDrawerMode] = useState<'create' | 'edit' | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [form, setForm] = useState<GoalForm>(EMPTY_FORM)
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const selectedGoal = selectedId ? goals.find((goal) => goal.id === selectedId) ?? null : null
  const groups = useMemo(() => groupGoals(goals), [goals])

  const refresh = useCallback(async () => {
    try {
      setLoading(true)
      setGoals(await listGoals())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '目标加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const openCreate = () => {
    setDrawerMode('create')
    setSelectedId(null)
    setForm(EMPTY_FORM)
    setError(null)
  }

  const openEdit = (goal: Goal) => {
    setDrawerMode('edit')
    setSelectedId(goal.id)
    setForm(formFromGoal(goal))
    setError(null)
  }

  const closeDrawer = () => {
    setDrawerMode(null)
    setSelectedId(null)
    setForm(EMPTY_FORM)
    setDeleteConfirmOpen(false)
  }

  const saveDrawer = async () => {
    if (!form.title.trim()) {
      setError('目标标题不能为空')
      return
    }
    setSaving(true)
    setError(null)
    try {
      if (drawerMode === 'create') {
        await createGoal({
          title: form.title.trim(),
          detail: form.detail.trim() || null,
          horizon: form.horizon,
          focus: form.focus,
        })
      } else if (selectedGoal) {
        await updateGoal(selectedGoal.id, {
          title: form.title.trim(),
          detail: form.detail.trim() || null,
          horizon: form.horizon,
          status: form.status,
          focus: form.focus,
        })
      }
      closeDrawer()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const patchGoal = async (goal: Goal, changes: Partial<Goal>) => {
    setBusyId(goal.id)
    setError(null)
    try {
      const updated = await updateGoal(goal.id, changes)
      setGoals((current) => replaceGoal(current, updated))
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新失败')
    } finally {
      setBusyId(null)
    }
  }

  const progressGoal = async (goal: Goal) => {
    setBusyId(goal.id)
    setError(null)
    try {
      const updated = await markGoalProgress(goal.id)
      setGoals((current) => replaceGoal(current, updated))
    } catch (err) {
      setError(err instanceof Error ? err.message : '记录推进失败')
    } finally {
      setBusyId(null)
    }
  }

  const removeSelected = async () => {
    if (!selectedGoal) return
    setSaving(true)
    try {
      await deleteGoal(selectedGoal.id)
      closeDrawer()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setSaving(false)
    }
  }

  const activeCount = goals.filter((goal) => goal.status === 'active').length

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleGroup}>
          <h1 className={styles.title}>目标</h1>
          <p className={styles.subtitle}>{activeCount} 个进行中</p>
        </div>
        <div className={styles.headerActions}>
          <button className={styles.button} onClick={openCreate}>
            <PlusIcon />
            新增目标
          </button>
        </div>
      </header>

      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}

      {loading ? (
        <div className={styles.empty}>加载中...</div>
      ) : (
        <div className={`${styles.goalWorkspace} ${drawerMode ? styles.drawerOpen : ''}`}>
          <section className={styles.goalListPanel}>
            {groups.map((group) => (
              <CollapsibleGroup
                key={group.key}
                title={group.title}
                count={group.goals.length}
                defaultCollapsed={group.key === 'paused' || group.key === 'done' || group.key === 'abandoned'}
              >
                {group.goals.length === 0 ? (
                  <p className={styles.groupEmpty}>暂无</p>
                ) : (
                  <div className={styles.col}>
                    {group.goals.map((goal) => (
                      <GoalRow
                        key={goal.id}
                        goal={goal}
                        busy={busyId === goal.id}
                        onEdit={() => openEdit(goal)}
                        onPatch={(changes) => void patchGoal(goal, changes)}
                        onProgress={() => void progressGoal(goal)}
                      />
                    ))}
                  </div>
                )}
              </CollapsibleGroup>
            ))}
          </section>

          {drawerMode && (
            <div className={styles.todoDrawerLayer} onClick={closeDrawer}>
              <GoalDrawer
                mode={drawerMode}
                form={form}
                saving={saving}
                goalId={selectedGoal?.id ?? null}
                goals={goals}
                onChange={setForm}
                onClose={closeDrawer}
                onSave={() => void saveDrawer()}
                onDelete={() => setDeleteConfirmOpen(true)}
              />
            </div>
          )}
        </div>
      )}

      <ConfirmDialog
        isOpen={deleteConfirmOpen}
        title="删除目标"
        message="删除后不会自动恢复。确定删除吗？"
        confirmText="删除"
        cancelText="取消"
        danger
        onConfirm={async () => {
          setDeleteConfirmOpen(false)
          await removeSelected()
        }}
        onCancel={() => setDeleteConfirmOpen(false)}
      />
    </div>
  )
}

function GoalRow({
  goal,
  busy,
  onEdit,
  onPatch,
  onProgress,
}: {
  goal: Goal
  busy: boolean
  onEdit: () => void
  onPatch: (changes: Partial<Goal>) => void
  onProgress: () => void
}) {
  return (
    <article className={styles.itemCard}>
      <div className={styles.itemRow}>
        <div className={styles.goalBody}>
          <div className={styles.goalTitleLine}>
            <h3>{goal.title}</h3>
            {goal.focus && <span className={styles.focusBadge}>当前关注</span>}
          </div>
          {goal.detail && <p className={styles.itemDetail}>{goal.detail}</p>}
          <div className={styles.goalMeta}>
            <span className={styles.chip}>{goal.horizon === 'long' ? '长期' : '短期'}</span>
            <span className={styles.chip}>{statusLabel(goal.status)}</span>
            {goal.last_progress_at && (
              <span className={styles.chip}>最近推进 {formatAbsoluteTime(goal.last_progress_at)}</span>
            )}
            <span className={styles.chip}>{goal.source === 'user' ? '手动新增' : '采纳建议'}</span>
          </div>
        </div>
        <div className={styles.rowActs}>
          {goal.status === 'active' && (
            <>
              {goal.horizon === 'short' && (
                <button disabled={busy} onClick={() => onPatch({ focus: !goal.focus })}>
                  {goal.focus ? '取消关注' : '设为关注'}
                </button>
              )}
              <button disabled={busy} onClick={onProgress}>
                记录推进
              </button>
              <button disabled={busy} onClick={() => onPatch({ status: 'done' })}>
                完成
              </button>
            </>
          )}
          {goal.status === 'paused' && (
            <button disabled={busy} onClick={() => onPatch({ status: 'active' })}>
              恢复
            </button>
          )}
          {(goal.status === 'done' || goal.status === 'abandoned') && (
            <button disabled={busy} onClick={() => onPatch({ status: 'active' })}>
              重新激活
            </button>
          )}
          <button disabled={busy} onClick={onEdit}>编辑</button>
        </div>
      </div>
    </article>
  )
}

function GoalDrawer({
  mode,
  form,
  saving,
  goalId,
  goals,
  onChange,
  onClose,
  onSave,
  onDelete,
}: {
  mode: 'create' | 'edit'
  form: GoalForm
  saving: boolean
  goalId: string | null
  goals: Goal[]
  onChange: (form: GoalForm) => void
  onClose: () => void
  onSave: () => void
  onDelete: () => void
}) {
  const setField = <K extends keyof GoalForm>(key: K, value: GoalForm[K]) => {
    onChange({ ...form, [key]: value })
  }

  return (
    <aside className={styles.todoDrawer} onClick={(event) => event.stopPropagation()}>
      <div className={styles.drawerHeader}>
        <div><h2>{mode === 'create' ? '新增目标' : '编辑目标'}</h2></div>
        <button className={styles.ghostButton} onClick={onClose} disabled={saving}>关闭</button>
      </div>
      <label className={styles.field}>
        <span>目标标题</span>
        <input value={form.title} onChange={(event) => setField('title', event.target.value)} />
      </label>
      <label className={styles.field}>
        <span>细节 / 成功标准</span>
        <textarea value={form.detail} onChange={(event) => setField('detail', event.target.value)} rows={4} />
      </label>
      <div className={styles.drawerFieldGrid}>
        <label className={styles.field}>
          <span>周期</span>
          <select value={form.horizon} onChange={(event) => setField('horizon', event.target.value as GoalHorizon)}>
            <option value="short">短期</option>
            <option value="long">长期</option>
          </select>
        </label>
        <label className={styles.field}>
          <span>状态</span>
          <select
            value={form.status}
            disabled={mode === 'create'}
            onChange={(event) => setField('status', event.target.value as GoalStatus)}
          >
            <option value="active">进行中</option>
            <option value="paused">暂停</option>
            <option value="done">已完成</option>
            <option value="abandoned">已放弃</option>
          </select>
        </label>
      </div>
      <label className={styles.checkboxField}>
        <input
          type="checkbox"
          checked={form.focus}
          disabled={form.horizon === 'long'}
          onChange={(event) => setField('focus', event.target.checked)}
        />
        <span>作为当前关注（仅短期目标）</span>
      </label>
      {mode === 'edit' && goalId && <GoalScheduleSection goalId={goalId} goals={goals} />}
      <div className={styles.drawerActions}>
        {mode === 'edit' ? (
          <button className={styles.dangerButton} onClick={onDelete} disabled={saving}>删除</button>
        ) : <span />}
        <button className={styles.button} onClick={onSave} disabled={saving}>
          {saving ? '保存中...' : '保存'}
        </button>
      </div>
    </aside>
  )
}

function shiftDays(key: string, delta: number): string {
  const date = new Date(`${key}T00:00:00`)
  date.setDate(date.getDate() + delta)
  return localDateKey(date)
}

function GoalScheduleSection({ goalId, goals }: { goalId: string; goals: Goal[] }) {
  const [data, setData] = useState<GoalSchedule | null>(null)
  const [recentEvents, setRecentEvents] = useState<ScheduleEvent[]>([])
  const [selectedRecent, setSelectedRecent] = useState('')
  const [expLabel, setExpLabel] = useState('')
  const [expTarget, setExpTarget] = useState(3)
  const [busy, setBusy] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const today = todayKey()
      const [sched, recent] = await Promise.all([
        getGoalSchedule(goalId),
        listScheduleEvents(shiftDays(today, -14), today).catch(() => ({
          events: [] as ScheduleEvent[],
          configured: false,
          connected: false,
        })),
      ])
      setData(sched)
      setRecentEvents(recent.events)
      const expectation = sched.progress.expectation
      setExpLabel(expectation?.label ?? '')
      setExpTarget(expectation?.target ?? 3)
    } catch (err) {
      setError(err instanceof Error ? err.message : '日程信息加载失败')
    }
  }, [goalId])

  useEffect(() => {
    void load()
  }, [load])

  const linkedIds = new Set((data?.events ?? []).map((event) => event.id))
  const recentOptions = recentEvents.filter((event) => !linkedIds.has(event.id))

  const saveExpectation = async () => {
    if (!expLabel.trim()) {
      setError('期望描述不能为空')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await updateGoalScheduleExpectation(goalId, {
        period: 'week',
        target: expTarget,
        label: expLabel.trim(),
      })
      setData((prev) => (prev ? { ...prev, progress: result.progress } : prev))
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存期望失败')
    } finally {
      setBusy(false)
    }
  }

  const bindRecent = async () => {
    if (!selectedRecent) return
    setBusy(true)
    setError(null)
    try {
      await linkGoalSchedule(goalId, selectedRecent)
      setSelectedRecent('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '绑定失败')
    } finally {
      setBusy(false)
    }
  }

  const unbind = async (eventId: string) => {
    setBusy(true)
    setError(null)
    try {
      await unlinkGoalSchedule(goalId, eventId)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '解绑失败')
    } finally {
      setBusy(false)
    }
  }

  /* 后台创建（非阻塞）：此入口无日历网格，不做乐观渲染，成功后刷新本节。 */
  const submitGoalScheduleCreate = async (input: CreateScheduleEventInput) => {
    setError(null)
    try {
      await createScheduleEvent(input)
      await load()
    } catch (err) {
      setError(`日程创建失败：${err instanceof Error ? err.message : '未知错误'}`)
    }
  }

  const progressText = data?.progress.expectation
    ? `${data.progress.expectation.label} · 本周 ${data.progress.text ?? '0/0'}`
    : '尚未设置期望'

  return (
    <section className={styles.goalSchedBlock}>
      <div className={styles.goalSchedTitle}>日程</div>
      {error && <p className={styles.goalSchedError}>{error}</p>}

      <div className={styles.goalSchedProgress}>{progressText}</div>
      <div className={styles.goalSchedExpGrid}>
        <label className={styles.field}>
          <span>期望描述</span>
          <input value={expLabel} placeholder="例如：每周健身 3 次" onChange={(event) => setExpLabel(event.target.value)} />
        </label>
        <label className={styles.field}>
          <span>每周次数</span>
          <input
            type="number"
            min={1}
            value={expTarget}
            onChange={(event) => setExpTarget(Math.max(1, Number(event.target.value) || 1))}
          />
        </label>
      </div>
      <button className={styles.ghostButton} type="button" onClick={() => void saveExpectation()} disabled={busy}>
        保存期望
      </button>

      <div className={styles.goalSchedSub}>已绑定日程</div>
      {data && data.events.length > 0 ? (
        <div className={styles.goalSchedList}>
          {data.events.map((event) => (
            <div key={event.id} className={styles.goalSchedItem}>
              <span className={styles.goalSchedItemTime}>{monthDayLabel(eventDateKey(event))} {formatEventTime(event)}</span>
              <span className={styles.goalSchedItemTitle}>{event.subject || '(无标题)'}</span>
              <button className={styles.goalSchedUnbind} type="button" onClick={() => void unbind(event.id)} disabled={busy}>解绑</button>
            </div>
          ))}
        </div>
      ) : (
        <p className={styles.groupEmpty}>还没有绑定日程。</p>
      )}

      {recentOptions.length > 0 && (
        <div className={styles.goalSchedBind}>
          <select value={selectedRecent} onChange={(event) => setSelectedRecent(event.target.value)}>
            <option value="">从近 14 天事件选择…</option>
            {recentOptions.map((event) => (
              <option key={event.id} value={event.id}>
                {monthDayLabel(eventDateKey(event))} {formatEventTime(event)} · {event.subject || '(无标题)'}
              </option>
            ))}
          </select>
          <button className={styles.ghostButton} type="button" onClick={() => void bindRecent()} disabled={busy || !selectedRecent}>
            绑定
          </button>
        </div>
      )}

      <button className={styles.ghostButton} type="button" onClick={() => setCreateOpen(true)} disabled={busy}>
        为此目标创建日程
      </button>

      {createOpen && (
        <ScheduleEventDrawer
          goals={goals}
          presetGoalId={goalId}
          onClose={() => setCreateOpen(false)}
          onSubmit={(submission) => {
            setCreateOpen(false)
            /* 此入口只会是 create；防御性忽略 update。 */
            if (submission.kind === 'create') void submitGoalScheduleCreate(submission.input)
          }}
        />
      )}
    </section>
  )
}

function groupGoals(goals: Goal[]) {
  return [
    {
      key: 'long',
      title: '长期目标',
      description: '持续数月或更久，始终进入对话背景',
      goals: goals.filter((goal) => goal.status === 'active' && goal.horizon === 'long'),
    },
    {
      key: 'short',
      title: '短期目标',
      description: '当前阶段可推进的目标；关注项会进入当前状态',
      goals: goals.filter((goal) => goal.status === 'active' && goal.horizon === 'short'),
    },
    {
      key: 'paused',
      title: '已暂停',
      description: '暂时不推进，但保留目标',
      goals: goals.filter((goal) => goal.status === 'paused'),
    },
    {
      key: 'done',
      title: '已完成',
      description: '达成的目标',
      goals: goals.filter((goal) => goal.status === 'done'),
    },
    {
      key: 'abandoned',
      title: '已放弃',
      description: '不再追求的目标',
      goals: goals.filter((goal) => goal.status === 'abandoned'),
    },
  ]
}

function formFromGoal(goal: Goal): GoalForm {
  return {
    title: goal.title,
    detail: goal.detail ?? '',
    horizon: goal.horizon,
    status: goal.status,
    focus: goal.focus,
  }
}

function replaceGoal(goals: Goal[], updated: Goal): Goal[] {
  return goals.map((goal) => goal.id === updated.id ? updated : goal)
}

function statusLabel(status: GoalStatus): string {
  if (status === 'active') return '进行中'
  if (status === 'paused') return '暂停'
  if (status === 'done') return '已完成'
  return '已放弃'
}
