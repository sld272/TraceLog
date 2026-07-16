import { useMemo } from 'react'
import { type ScheduleEvent } from '@/api/client'
import {
  type CalendarCell,
  WEEKDAY_HEADERS,
  eventClock,
  eventDateKey,
} from '@/utils/schedule'
import styles from './ScheduleMonthGrid.module.css'

interface ScheduleMonthGridProps {
  cells: CalendarCell[]
  today: string
  events: ScheduleEvent[]
  onEventClick: (event: ScheduleEvent, anchor: { x: number; y: number }) => void
  onDayClick: (dateKey: string) => void
}

const MAX_CHIPS = 3

export function ScheduleMonthGrid({ cells, today, events, onEventClick, onDayClick }: ScheduleMonthGridProps) {
  const eventsByDay = useMemo(() => {
    const map = new Map<string, ScheduleEvent[]>()
    for (const event of events) {
      const key = eventDateKey(event)
      const list = map.get(key)
      if (list) list.push(event)
      else map.set(key, [event])
    }
    for (const list of map.values()) {
      list.sort((a, b) => {
        if (a.all_day !== b.all_day) return a.all_day ? -1 : 1
        return a.start_local < b.start_local ? -1 : a.start_local > b.start_local ? 1 : 0
      })
    }
    return map
  }, [events])

  return (
    <div className={styles.card}>
      <div className={styles.headRow}>
        {WEEKDAY_HEADERS.map((label) => (
          <div key={label} className={styles.headCell}>周{label}</div>
        ))}
      </div>
      <div className={styles.grid}>
        {cells.map((cell: CalendarCell, index) => {
          const dayEvents = eventsByDay.get(cell.key) ?? []
          const shown = dayEvents.slice(0, MAX_CHIPS)
          const more = dayEvents.length - shown.length
          return (
            <button
              key={cell.key || `cell-${index}`}
              type="button"
              className={`${styles.cell} ${cell.outside ? styles.outside : ''} ${cell.key === today ? styles.todayCell : ''}`}
              onClick={() => onDayClick(cell.key)}
            >
              <span className={styles.date}>{cell.day}</span>
              {shown.map((event) => (
                <span
                  key={event.id}
                  className={`${styles.chip} ${event.goal_links.length > 0 ? styles.chipGoal : ''} ${event.provider === 'local' ? styles.chipLocal : ''}`}
                  onClick={(clickEvent) => {
                    clickEvent.stopPropagation()
                    onEventClick(event, { x: clickEvent.clientX, y: clickEvent.clientY })
                  }}
                >
                  {event.all_day ? event.subject || '(无标题)' : `${eventClock(event.start_local)} ${event.subject || '(无标题)'}`}
                </span>
              ))}
              {more > 0 && <span className={styles.more}>还有 {more} 项</span>}
            </button>
          )
        })}
      </div>
    </div>
  )
}
