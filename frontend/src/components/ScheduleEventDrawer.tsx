import { useEffect, useRef, useState } from 'react'
import {
  type CreateScheduleEventInput,
  type Goal,
  type ScheduleEvent,
} from '@/api/client'
import { eventClock, eventDateKey, todayKey } from '@/utils/schedule'
import workspaceStyles from '@/pages/WorkspacePages.module.css'
import styles from './ScheduleEventDrawer.module.css'

/** Drawer 确认后交给调用方的提交意图（不在 Drawer 内触碰任何 API）。 */
export type ScheduleDrawerSubmission =
  | { kind: 'create'; input: CreateScheduleEventInput }
  | {
      kind: 'update'
      /** 编辑前的原事件（含原 goal_links，供乐观回滚与 diff）。 */
      event: ScheduleEvent
      /** 与旧 save() 相同的字段集：subject/date/all_day/start_time/end_time。 */
      fields: Partial<CreateScheduleEventInput>
      /** 目标绑定 diff；from === to 表示无绑定改动。 */
      goalDiff: { from: string | null; to: string | null }
    }

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
  /** 校验通过后同步交出提交意图；关闭由调用方负责（在 onSubmit 里 setDrawer(null)）。 */
  onSubmit: (submission: ScheduleDrawerSubmission) => void
}

export function ScheduleEventDrawer({
  goals,
  presetGoalId,
  accounts,
  prefill,
  event,
  onClose,
  onSubmit,
}: ScheduleEventDrawerProps) {
  const editing = event != null
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
  const [error, setError] = useState<string | null>(null)
  /** 防双击：交出提交意图后忽略后续点击（Drawer 随即被调用方关闭）。 */
  const submittedRef = useRef(false)

  useEffect(() => {
    const handleKey = (keyEvent: KeyboardEvent) => {
      if (keyEvent.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  const presetGoal = presetGoalId ? goals.find((goal) => goal.id === presetGoalId) ?? null : null

  const submit = () => {
    if (!subject.trim()) {
      setError('日程标题不能为空')
      return
    }
    if (submittedRef.current) return
    submittedRef.current = true
    const fields = {
      subject: subject.trim(),
      date,
      all_day: allDay,
      start_time: allDay ? undefined : startTime,
      end_time: allDay ? undefined : endTime,
    }
    if (editing) {
      onSubmit({
        kind: 'update',
        event,
        fields,
        goalDiff: { from: event.goal_links[0]?.goal_id ?? null, to: goalId || null },
      })
    } else {
      onSubmit({
        kind: 'create',
        input: { ...fields, goal_id: goalId || undefined, account_id: accountId || undefined },
      })
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
          <button className={workspaceStyles.ghostButton} type="button" onClick={onClose}>关闭</button>
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
          <button className={workspaceStyles.ghostButton} type="button" onClick={onClose}>取消</button>
          <button className={workspaceStyles.button} type="button" onClick={submit}>
            {editing ? '保存改动' : '创建日程'}
          </button>
        </div>
      </aside>
    </div>
  )
}
