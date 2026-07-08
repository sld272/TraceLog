import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  type Goal,
  type GoalHorizon,
  type GoalStatus,
  createGoal,
  deleteGoal,
  listGoals,
  markGoalProgress,
  updateGoal,
} from '@/api/client'
import { CollapsibleGroup } from '@/components/CollapsibleGroup'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Notice } from '@/components/Notice'
import { PlusIcon } from '@/components/icons'
import { formatAbsoluteTime } from '@/utils/date'
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
  onChange,
  onClose,
  onSave,
  onDelete,
}: {
  mode: 'create' | 'edit'
  form: GoalForm
  saving: boolean
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
