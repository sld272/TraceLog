import { useEffect, useMemo, useState } from 'react'
import { type ScheduleEvent } from '@/api/client'
import {
  type ScheduleBlock,
  dateFromKey,
  eventClock,
  eventDateKey,
  layoutDayBlocks,
  minutesToTime,
  weekdayShortLabel,
} from '@/utils/schedule'
import styles from './ScheduleWeekGrid.module.css'

const HOUR_PX = 52

interface ScheduleWeekGridProps {
  weekDays: string[]
  today: string
  events: ScheduleEvent[]
  onEventClick: (event: ScheduleEvent, anchor: { x: number; y: number }) => void
  onCreateSlot: (date: string, startTime: string, endTime: string) => void
}

function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

/** 每 60s 刷新一次的当前时间（用于时刻线）。 */
function useNow(intervalMs = 60000): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), intervalMs)
    return () => window.clearInterval(id)
  }, [intervalMs])
  return now
}

export function ScheduleWeekGrid({ weekDays, today, events, onEventClick, onCreateSlot }: ScheduleWeekGridProps) {
  const now = useNow()
  const [hover, setHover] = useState<{ date: string; min: number } | null>(null)

  const weekKey = weekDays.join(',')
  const { timedByDay, allDayByDay, startH, endH } = useMemo(() => {
    const timed = new Map<string, ScheduleBlock[]>()
    const allDay = new Map<string, ScheduleEvent[]>()
    let minH = 7
    let maxH = 23
    for (const key of weekDays) {
      const dayEvents = events.filter((event) => eventDateKey(event) === key)
      allDay.set(key, dayEvents.filter((event) => event.all_day))
      const blocks = layoutDayBlocks(dayEvents.filter((event) => !event.all_day))
      timed.set(key, blocks)
      for (const block of blocks) {
        minH = Math.min(minH, Math.floor(block.startMin / 60))
        maxH = Math.max(maxH, Math.ceil(block.endMin / 60))
      }
    }
    return {
      timedByDay: timed,
      allDayByDay: allDay,
      startH: Math.max(0, Math.min(minH, 7)),
      endH: Math.min(24, Math.max(maxH, 23)),
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, weekKey])

  const bodyPx = (endH - startH) * HOUR_PX

  const snapMin = (col: HTMLElement, clientY: number): number => {
    const rect = col.getBoundingClientRect()
    const raw = startH * 60 + ((clientY - rect.top) / HOUR_PX) * 60
    return Math.min(Math.max(Math.round(raw / 30) * 30, startH * 60), endH * 60 - 60)
  }

  const nowMin = now.getHours() * 60 + now.getMinutes()

  return (
    <div className={styles.card}>
      <div className={styles.headRow}>
        <div className={styles.corner} />
        {weekDays.map((key) => (
          <div key={key} className={`${styles.headCell} ${key === today ? styles.headToday : ''}`}>
            <span className={styles.dayName}>{weekdayShortLabel(key)}</span>
            <span className={styles.dayNum}>{dateFromKey(key).getDate()}</span>
          </div>
        ))}
      </div>

      <div className={styles.allDayRow}>
        <div className={styles.allDayLabel}>全天</div>
        {weekDays.map((key) => (
          <div key={key} className={styles.allDayCell}>
            {(allDayByDay.get(key) ?? []).map((event) => (
              <button
                key={event.id}
                type="button"
                className={`${styles.allDayChip} ${event.goal_links.length > 0 ? styles.allDayGoal : ''}`}
                onClick={(clickEvent) => {
                  clickEvent.stopPropagation()
                  onEventClick(event, { x: clickEvent.clientX, y: clickEvent.clientY })
                }}
              >
                {event.subject || '(无标题)'}
              </button>
            ))}
          </div>
        ))}
      </div>

      <div className={styles.body}>
        <div className={styles.axisCol} style={{ height: bodyPx }}>
          {Array.from({ length: endH - startH + 1 }, (_, i) => (
            <span key={i} className={styles.axisLabel} style={{ top: i * HOUR_PX }}>
              {pad2(startH + i)}:00
            </span>
          ))}
        </div>

        {weekDays.map((key) => {
          const isToday = key === today
          return (
            <div
              key={key}
              className={`${styles.dayCol} ${isToday ? styles.dayColToday : ''}`}
              style={{ height: bodyPx }}
              onMouseMove={(moveEvent) => {
                if (moveEvent.target !== moveEvent.currentTarget) {
                  if (hover) setHover(null)
                  return
                }
                const min = snapMin(moveEvent.currentTarget, moveEvent.clientY)
                if (!hover || hover.date !== key || hover.min !== min) setHover({ date: key, min })
              }}
              onMouseLeave={() => setHover((current) => (current?.date === key ? null : current))}
              onClick={(clickEvent) => {
                if (clickEvent.target !== clickEvent.currentTarget) return
                const min = snapMin(clickEvent.currentTarget, clickEvent.clientY)
                onCreateSlot(key, minutesToTime(min), minutesToTime(min + 60))
              }}
            >
              {(timedByDay.get(key) ?? []).map((block) => {
                const bound = block.event.goal_links.length > 0
                const top = ((block.startMin - startH * 60) / 60) * HOUR_PX
                const height = Math.max(((block.endMin - block.startMin) / 60) * HOUR_PX - 2, 18)
                const compact = height < 36
                const goalTitle = block.event.goal_links[0]?.goal_title
                return (
                  <div
                    key={block.event.id}
                    className={`${styles.evt} ${bound ? styles.evtGoal : ''} ${compact ? styles.evtCompact : ''}`}
                    style={{
                      top,
                      height,
                      left: `calc(${(block.col / block.cols) * 100}% + 3px)`,
                      width: `calc(${100 / block.cols}% - 6px)`,
                    }}
                    onClick={(clickEvent) => {
                      clickEvent.stopPropagation()
                      onEventClick(block.event, { x: clickEvent.clientX, y: clickEvent.clientY })
                    }}
                  >
                    {compact ? (
                      <>
                        <span className={styles.etime}>{eventClock(block.event.start_local)}</span>
                        <span className={styles.etitle}>{block.event.subject || '(无标题)'}</span>
                      </>
                    ) : (
                      <>
                        <span className={styles.etitle}>{block.event.subject || '(无标题)'}</span>
                        <span className={styles.etime}>
                          {eventClock(block.event.start_local)} – {eventClock(block.event.end_local)}
                        </span>
                        {bound && goalTitle && height >= 56 && (
                          <span className={styles.egoal}>◆ {goalTitle}</span>
                        )}
                      </>
                    )}
                  </div>
                )
              })}

              {hover?.date === key && (
                <div
                  className={styles.ghostSlot}
                  style={{ top: ((hover.min - startH * 60) / 60) * HOUR_PX }}
                >
                  ＋ {minutesToTime(hover.min)} 新建
                </div>
              )}

              {isToday && nowMin >= startH * 60 && nowMin <= endH * 60 && (
                <div className={styles.nowLine} style={{ top: ((nowMin - startH * 60) / 60) * HOUR_PX }} />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
