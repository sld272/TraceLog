import { useEffect, useState } from 'react'
import {
  type Goal,
  type ScheduleEvent,
  ApiError,
  createScheduleEvent,
  linkGoalSchedule,
  unlinkGoalSchedule,
  updateScheduleEvent,
} from '@/api/client'
import { eventClock, eventDateKey, todayKey } from '@/utils/schedule'
import workspaceStyles from '@/pages/WorkspacePages.module.css'
import styles from './ScheduleEventDrawer.module.css'

interface ScheduleEventDrawerProps {
  /** 可绑定的目标列表（进行中）。 */
  goals: Goal[]
  /** 预设并锁定绑定的目标（「为此目标创建日程」入口）。 */
  presetGoalId?: string
  /** 可写日历账号（顺序即默认优先级，Outlook 在前）。缺省视为仅 Outlook。 */
  accounts?: { id: string; label: string }[]
  /** 空白时段新建时的预填（日期 + 起止时间）。 */
  prefill?: { date: string; start_time?: string; end_time?: string }
  /** 传入则进入编辑态（预填现有事件字段）。 */
  event?: ScheduleEvent
  onClose: () => void
  /** 创建 / 编辑成功后的回调（返回保存后的事件）。 */
  onSaved?: (event: ScheduleEvent) => void
  /** @deprecated onSaved 的别名，保留兼容现有调用点。 */
  onCreated?: (event: ScheduleEvent) => void
}

export function ScheduleEventDrawer({
  goals,
  presetGoalId,
  accounts,
  prefill,
  event,
  onClose,
  onSaved,
  onCreated,
}: ScheduleEventDrawerProps) {
  const editing = event != null
  const handleSaved = onSaved ?? onCreated ?? (() => undefined)
  const writable = accounts ?? []

  const [subject, setSubject] = useState(() => event?.subject ?? '')
  const [date, setDate] = useState(() => (event ? eventDateKey(event) : prefill?.date ?? todayKey()))
  const [allDay, setAllDay] = useState(() => event?.all_day ?? false)
  const [startTime, setStartTime] = useState(() =>
    event ? eventClock(event.start_local) : prefill?.start_time ?? '09:00',
  )
  const [endTime, setEndTime] = useState(() =>
    event ? eventClock(event.end_local) : prefill?.end_time ?? '10:00',
  )
  const [goalId, setGoalId] = useState(
    () => presetGoalId ?? event?.goal_links[0]?.goal_id ?? '',
  )
  /** 保存到哪个账号（仅创建态；编辑态事件的家不可变）。 */
  const [accountId, setAccountId] = useState(() => writable[0]?.id ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const handleKey = (keyEvent: KeyboardEvent) => {
      if (keyEvent.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  const presetGoal = presetGoalId ? goals.find((goal) => goal.id === presetGoalId) ?? null : null

  const save = async () => {
    if (!subject.trim()) {
      setError('日程标题不能为空')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const fields = {
        subject: subject.trim(),
        date,
        all_day: allDay,
        start_time: allDay ? undefined : startTime,
        end_time: allDay ? undefined : endTime,
      }
      if (editing) {
        const updated = await updateScheduleEvent(event.id, fields)
        // 目标绑定：只 diff UI 表示的那一条绑定，避免误删未展示的其它绑定。
        const initialGoalId = event.goal_links[0]?.goal_id ?? ''
        if (initialGoalId !== goalId) {
          if (initialGoalId) await unlinkGoalSchedule(initialGoalId, event.id)
          if (goalId) await linkGoalSchedule(goalId, event.id)
        }
        handleSaved(updated)
      } else {
        const created = await createScheduleEvent({
          ...fields,
          goal_id: goalId || undefined,
          account_id: accountId || undefined,
        })
        handleSaved(created)
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError('没有可用的日历账号：请先在设置中登录 Microsoft，或创建本地日历。')
      } else {
        setError(err instanceof Error ? err.message : editing ? '保存日程失败' : '创建日程失败')
      }
      setSaving(false)
    }
  }

  const savingToLocal = editing ? event.provider === 'local' : accountId === 'local'
  const title = editing ? '编辑日程' : '新建日程'
  const bindHint = presetGoal ? `，并绑定到「${presetGoal.title}」` : ''
  const subtitle = editing
    ? savingToLocal
      ? '改动保存在本地日历（仅这台设备）。'
      : '改动写回 Outlook 日历。'
    : savingToLocal
      ? `保存到本地日历（仅这台设备）${bindHint}。`
      : accountId === 'outlook'
        ? `写入 Outlook 日历${bindHint}。`
        : `保存到你的日历${bindHint}。`

  return (
    <div className={styles.overlay} onClick={onClose}>
      <aside
        className={styles.drawer}
        onClick={(clickEvent) => clickEvent.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className={styles.header}>
          <div>
            <h2>{title}</h2>
            <p>{subtitle}</p>
          </div>
          <button className={workspaceStyles.ghostButton} type="button" onClick={onClose} disabled={saving}>关闭</button>
        </div>

        {error && <div className={styles.error}>{error}</div>}

        <label className={styles.field}>
          <span>标题</span>
          <input value={subject} onChange={(changeEvent) => setSubject(changeEvent.target.value)} placeholder="例如：健身 · 练背" autoFocus />
        </label>

        <label className={styles.field}>
          <span>日期</span>
          <input type="date" value={date} onChange={(changeEvent) => setDate(changeEvent.target.value)} />
        </label>

        <label className={styles.checkboxField}>
          <input type="checkbox" checked={allDay} onChange={(changeEvent) => setAllDay(changeEvent.target.checked)} />
          <span>全天</span>
        </label>

        {!allDay && (
          <div className={styles.grid}>
            <label className={styles.field}>
              <span>开始时间</span>
              <input type="time" value={startTime} onChange={(changeEvent) => setStartTime(changeEvent.target.value)} />
            </label>
            <label className={styles.field}>
              <span>结束时间</span>
              <input type="time" value={endTime} onChange={(changeEvent) => setEndTime(changeEvent.target.value)} />
            </label>
          </div>
        )}

        {!editing && writable.length > 1 && (
          <label className={styles.field}>
            <span>保存到</span>
            <select value={accountId} onChange={(changeEvent) => setAccountId(changeEvent.target.value)}>
              {writable.map((account) => (
                <option key={account.id} value={account.id}>{account.label}</option>
              ))}
            </select>
          </label>
        )}

        <label className={styles.field}>
          <span>绑定目标（可选）</span>
          <select
            value={goalId}
            disabled={presetGoalId !== undefined}
            onChange={(changeEvent) => setGoalId(changeEvent.target.value)}
          >
            <option value="">不绑定</option>
            {goals.map((goal) => (
              <option key={goal.id} value={goal.id}>{goal.title}</option>
            ))}
          </select>
        </label>

        <div className={styles.actions}>
          <button className={workspaceStyles.ghostButton} type="button" onClick={onClose} disabled={saving}>取消</button>
          <button className={workspaceStyles.button} type="button" onClick={() => void save()} disabled={saving}>
            {saving ? (editing ? '保存中...' : '创建中...') : editing ? '保存改动' : '创建日程'}
          </button>
        </div>
      </aside>
    </div>
  )
}
