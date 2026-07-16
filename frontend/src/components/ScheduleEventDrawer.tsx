import { useEffect, useState } from 'react'
import {
  type Goal,
  type ScheduleEvent,
  ApiError,
  createScheduleEvent,
} from '@/api/client'
import { todayKey } from '@/utils/schedule'
import workspaceStyles from '@/pages/WorkspacePages.module.css'
import styles from './ScheduleEventDrawer.module.css'

interface ScheduleEventDrawerProps {
  /** 可绑定的目标列表（进行中）。 */
  goals: Goal[]
  /** 预设并锁定绑定的目标（「为此目标创建日程」入口）。 */
  presetGoalId?: string
  onClose: () => void
  onCreated: (event: ScheduleEvent) => void
}

export function ScheduleEventDrawer({ goals, presetGoalId, onClose, onCreated }: ScheduleEventDrawerProps) {
  const [subject, setSubject] = useState('')
  const [date, setDate] = useState(todayKey())
  const [allDay, setAllDay] = useState(false)
  const [startTime, setStartTime] = useState('09:00')
  const [endTime, setEndTime] = useState('10:00')
  const [goalId, setGoalId] = useState(presetGoalId ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
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
      const created = await createScheduleEvent({
        subject: subject.trim(),
        date,
        all_day: allDay,
        start_time: allDay ? undefined : startTime,
        end_time: allDay ? undefined : endTime,
        goal_id: goalId || undefined,
      })
      onCreated(created)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError('Microsoft 日历尚未连接，请先在设置中登录。')
      } else {
        setError(err instanceof Error ? err.message : '创建日程失败')
      }
      setSaving(false)
    }
  }

  return (
    <div className={styles.overlay} onClick={onClose}>
      <aside className={styles.drawer} onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label="新建日程">
        <div className={styles.header}>
          <div>
            <h2>新建日程</h2>
            <p>写入 Outlook 日历{presetGoal ? `，并绑定到「${presetGoal.title}」` : ''}。</p>
          </div>
          <button className={workspaceStyles.ghostButton} type="button" onClick={onClose} disabled={saving}>关闭</button>
        </div>

        {error && <div className={styles.error}>{error}</div>}

        <label className={styles.field}>
          <span>标题</span>
          <input value={subject} onChange={(event) => setSubject(event.target.value)} placeholder="例如：健身 · 练背" autoFocus />
        </label>

        <label className={styles.field}>
          <span>日期</span>
          <input type="date" value={date} onChange={(event) => setDate(event.target.value)} />
        </label>

        <label className={styles.checkboxField}>
          <input type="checkbox" checked={allDay} onChange={(event) => setAllDay(event.target.checked)} />
          <span>全天</span>
        </label>

        {!allDay && (
          <div className={styles.grid}>
            <label className={styles.field}>
              <span>开始时间</span>
              <input type="time" value={startTime} onChange={(event) => setStartTime(event.target.value)} />
            </label>
            <label className={styles.field}>
              <span>结束时间</span>
              <input type="time" value={endTime} onChange={(event) => setEndTime(event.target.value)} />
            </label>
          </div>
        )}

        <label className={styles.field}>
          <span>绑定目标（可选）</span>
          <select
            value={goalId}
            disabled={presetGoalId !== undefined}
            onChange={(event) => setGoalId(event.target.value)}
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
            {saving ? '创建中...' : '创建日程'}
          </button>
        </div>
      </aside>
    </div>
  )
}
