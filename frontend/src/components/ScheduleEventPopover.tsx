import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { type ScheduleEvent, type ScheduleProgress, deleteScheduleEvent } from '@/api/client'
import { eventClock, eventDateKey, monthDayLabel, weekdayShortLabel } from '@/utils/schedule'
import styles from './ScheduleEventPopover.module.css'

const POPOVER_WIDTH = 312

interface ScheduleEventPopoverProps {
  event: ScheduleEvent
  anchor: { x: number; y: number }
  /** goal_id → 本周进度，用于目标进度块。 */
  progressByGoal: Record<string, ScheduleProgress>
  onClose: () => void
  onEdit: (event: ScheduleEvent) => void
  /** 删除成功后回调，页面据此刷新当前区间。 */
  onDeleted: () => void
}

export function ScheduleEventPopover({ event, anchor, progressByGoal, onClose, onEdit, onDeleted }: ScheduleEventPopoverProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: anchor.x, top: anchor.y })
  const [armed, setArmed] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /* 定位：贴近点击点，收敛到视口内。 */
  useLayoutEffect(() => {
    const height = ref.current?.offsetHeight ?? 240
    const left = Math.max(16, Math.min(anchor.x + 10, window.innerWidth - POPOVER_WIDTH - 16))
    const top = Math.max(16, Math.min(anchor.y - 10, window.innerHeight - height - 16))
    setPos({ left, top })
  }, [anchor.x, anchor.y, event.id])

  /* 外点关闭（在渲染后挂载 mousedown，避开触发时的那次点击）。 */
  useEffect(() => {
    const handleDown = (downEvent: MouseEvent) => {
      if (ref.current && !ref.current.contains(downEvent.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handleDown)
    return () => document.removeEventListener('mousedown', handleDown)
  }, [onClose])

  const dateKey = eventDateKey(event)
  const when = event.all_day
    ? `${monthDayLabel(dateKey)} ${weekdayShortLabel(dateKey)} · 全天`
    : `${monthDayLabel(dateKey)} ${weekdayShortLabel(dateKey)} · ${eventClock(event.start_local)} – ${eventClock(event.end_local)}`

  const remove = async () => {
    if (!armed) {
      setArmed(true)
      return
    }
    setDeleting(true)
    setError(null)
    try {
      await deleteScheduleEvent(event.id)
      onDeleted()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
      setDeleting(false)
      setArmed(false)
    }
  }

  return (
    <div ref={ref} className={styles.popover} style={{ left: pos.left, top: pos.top }} role="dialog" aria-label={event.subject || '日程详情'}>
      <div className={styles.title}>{event.subject || '(无标题)'}</div>

      <div className={styles.row}><span className={styles.icon} aria-hidden="true">🕐</span>{when}</div>
      {event.location && (
        <div className={styles.row}><span className={styles.icon} aria-hidden="true">📍</span>{event.location}</div>
      )}
      {event.body_preview && <p className={styles.preview}>{event.body_preview}</p>}

      {event.goal_links.map((link) => {
        const progress = progressByGoal[link.goal_id]
        const target = progress?.target ?? null
        const pct = target && target > 0 ? Math.min(progress!.current / target, 1) * 100 : 0
        return (
          <div key={link.goal_id} className={styles.goalBlock}>
            <div className={styles.goalHead}>
              <span className={styles.goalName}>◆ {link.goal_title}</span>
              {target != null && <span className={styles.goalNum}>本周 {progress!.current}/{target}</span>}
            </div>
            {target != null && (
              <div className={styles.goalBar}><i style={{ width: `${pct}%` }} /></div>
            )}
            <div className={styles.goalHint}>
              {progress?.expectation
                ? `期望 ${progress.expectation.label} · 这场日程计入本周进度`
                : '这场日程已绑定该目标'}
            </div>
          </div>
        )
      })}

      {error && <div className={styles.error}>{error}</div>}

      <div className={styles.actions}>
        <button className={styles.actBtn} type="button" onClick={() => onEdit(event)} disabled={deleting}>编辑</button>
        <button
          className={`${styles.actBtn} ${armed ? styles.actDanger : ''}`}
          type="button"
          onClick={() => void remove()}
          disabled={deleting}
        >
          {deleting ? '删除中...' : armed ? '确认删除？' : '删除'}
        </button>
        {event.web_link && (
          <a className={styles.actBtn} href={event.web_link} target="_blank" rel="noreferrer">在 Outlook 打开 ↗</a>
        )}
      </div>
    </div>
  )
}
