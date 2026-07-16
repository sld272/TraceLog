import { type ScheduleEvent, type ScheduleProgress } from '@/api/client'
import { formatEventTime, goalChipLabel } from '@/utils/schedule'
import styles from './ScheduleList.module.css'

interface ScheduleListProps {
  events: ScheduleEvent[]
  /** goal_id → 本周进度，用于 goalChip 的「· 本周 N/M」。 */
  progressByGoal?: Record<string, ScheduleProgress>
  emptyText: string
  /** 是否展示地点行（右栏紧凑态可关闭）。 */
  showLocation?: boolean
}

export function ScheduleList({ events, progressByGoal = {}, emptyText, showLocation = false }: ScheduleListProps) {
  if (events.length === 0) {
    return <p className={styles.schedEmpty}>{emptyText}</p>
  }
  return (
    <div className={styles.schedList}>
      {events.map((event) => {
        const bound = event.goal_links.length > 0
        return (
          <div key={event.id} className={`${styles.schedItem} ${bound ? styles.goalBound : ''}`}>
            <span className={styles.schedTime}>{formatEventTime(event)}</span>
            <span className={styles.schedRail} aria-hidden="true" />
            <div className={styles.schedBody}>
              <div className={styles.schedTitleRow}>
                <span className={styles.schedTitle}>{event.subject || '(无标题)'}</span>
                {event.web_link && (
                  <a
                    className={styles.schedLink}
                    href={event.web_link}
                    target="_blank"
                    rel="noreferrer"
                    title="在 Outlook 中打开"
                    aria-label="在 Outlook 中打开"
                  >
                    <ExternalLinkIcon />
                  </a>
                )}
              </div>
              {showLocation && event.location && <p className={styles.schedLoc}>{event.location}</p>}
              {event.goal_links.map((link) => (
                <span key={link.goal_id} className={styles.goalChip}>
                  <TargetIcon />
                  {goalChipLabel(link.goal_title, progressByGoal[link.goal_id])}
                </span>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function ExternalLinkIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  )
}

function TargetIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="3.5" />
    </svg>
  )
}
