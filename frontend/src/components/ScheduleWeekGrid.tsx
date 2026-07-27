import { type CSSProperties, useEffect, useMemo, useState } from 'react'
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

const HOUR_PX = 56
/** 网格再空也保留这么多小时，太窄看不出一天的形状。 */
const MIN_VISIBLE_HOURS = 9

interface ScheduleWeekGridProps {
  weekDays: string[]
  today: string
  events: ScheduleEvent[]
  onEventClick: (event: ScheduleEvent, anchor: { x: number; y: number }) => void
  onCreateSlot: (date: string, startTime: string, endTime: string) => void
  /** 点全天格子新建一条全天事件。 */
  onCreateAllDay: (date: string) => void
  /** 后台提交在途的事件 id：降透明并禁点击。 */
  pendingIds?: Set<string>
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

export function ScheduleWeekGrid({ weekDays, today, events, onEventClick, onCreateSlot, onCreateAllDay, pendingIds }: ScheduleWeekGridProps) {
  const now = useNow()
  const [hover, setHover] = useState<{ date: string; min: number } | null>(null)

  const weekKey = weekDays.join(',')
  const nowHour = now.getHours()
  /* 网格只铺到有事情发生的那几个小时。原来固定从 07:00 铺到 23:00，
     一周只有两件事时，整页有八成是空格子。 */
  const { timedByDay, allDayByDay, startH, endH } = useMemo(() => {
    const timed = new Map<string, ScheduleBlock[]>()
    const allDay = new Map<string, ScheduleEvent[]>()
    let minH = 24
    let maxH = 0
    let hasTimed = false
    for (const key of weekDays) {
      const dayEvents = events.filter((event) => eventDateKey(event) === key)
      allDay.set(key, dayEvents.filter((event) => event.all_day))
      const blocks = layoutDayBlocks(dayEvents.filter((event) => !event.all_day))
      timed.set(key, blocks)
      for (const block of blocks) {
        hasTimed = true
        minH = Math.min(minH, Math.floor(block.startMin / 60))
        maxH = Math.max(maxH, Math.ceil(block.endMin / 60))
      }
    }
    if (!hasTimed) {
      minH = 8
      maxH = 20
    } else {
      minH = Math.max(0, minH - 1)
      maxH = Math.min(24, maxH + 1)
    }
    /* "现在"只在它本来就挨着这批事件时才把范围撑开一点。凌晨打开页面时，
       无条件包含当前时刻会把网格从 00:00 一路铺下来，比固定 07:00 还糟。 */
    if (weekDays.includes(today) && nowHour >= minH - 2 && nowHour <= maxH + 2) {
      minH = Math.min(minH, nowHour)
      maxH = Math.max(maxH, Math.min(24, nowHour + 1))
    }
    /* 太窄的网格看不出一天的形状，兜一个最小跨度 */
    while (maxH - minH < MIN_VISIBLE_HOURS) {
      if (maxH < 24) maxH += 1
      else if (minH > 0) minH -= 1
      else break
    }
    return { timedByDay: timed, allDayByDay: allDay, startH: minH, endH: maxH }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, weekKey, today, nowHour])

  const bodyPx = (endH - startH) * HOUR_PX

  /* 吸附到半小时。下界留半小时而不是一小时——留一小时的话，网格最后那条
     半小时带永远吸不上去（空周网格铺到 20:00 时，19:30 会被拽回 19:00），
     等于有一格是看得见点不着的。事件跨出可见范围没关系，下次渲染网格会自己长出来。 */
  const snapMin = (col: HTMLElement, clientY: number): number => {
    const rect = col.getBoundingClientRect()
    const raw = startH * 60 + ((clientY - rect.top) / HOUR_PX) * 60
    return Math.min(Math.max(Math.round(raw / 30) * 30, startH * 60), endH * 60 - 30)
  }

  const nowMin = now.getHours() * 60 + now.getMinutes()

  return (
    <div className={styles.card} style={{ '--hour-px': `${HOUR_PX}px` } as CSSProperties}>
      <div className={styles.headRow}>
        <div className={styles.corner} />
        {weekDays.map((key) => (
          <div key={key} className={`${styles.headCell} ${key === today ? styles.headToday : ''}`}>
            <span className={styles.dayName}>{weekdayShortLabel(key)}</span>
            <span className={styles.dayNum}>{dateFromKey(key).getDate()}</span>
          </div>
        ))}
      </div>

      {/* 全天行常驻：它同时是"这天有没有全天安排"的答案和新建入口，
          空着的格子点一下就是新建一条全天事件。 */}
      <div className={styles.allDayRow}>
        <div className={styles.allDayLabel}>全天</div>
        {weekDays.map((key) => (
          <div
            key={key}
            className={`${styles.allDayCell} ${key === today ? styles.allDayCellToday : ''}`}
            role="button"
            tabIndex={0}
            aria-label={`在 ${key} 新建全天日程`}
            onClick={(clickEvent) => {
              if (clickEvent.target !== clickEvent.currentTarget) return
              onCreateAllDay(key)
            }}
            onKeyDown={(keyEvent) => {
              if (keyEvent.target !== keyEvent.currentTarget) return
              if (keyEvent.key === 'Enter' || keyEvent.key === ' ') {
                keyEvent.preventDefault()
                onCreateAllDay(key)
              }
            }}
          >
            {(allDayByDay.get(key) ?? []).map((event) => {
              const pending = pendingIds?.has(event.id) ?? false
              return (
                <button
                  key={event.id}
                  type="button"
                  className={`${styles.allDayChip} ${event.goal_links.length > 0 ? styles.allDayGoal : ''} ${pending ? styles.allDayPending : ''}`}
                  onClick={(clickEvent) => {
                    clickEvent.stopPropagation()
                    if (pending) return
                    onEventClick(event, { x: clickEvent.clientX, y: clickEvent.clientY })
                  }}
                >
                  {event.subject || '(无标题)'}
                </button>
              )
            })}
            <span className={styles.allDayGhost} aria-hidden="true">＋</span>
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
                const pending = pendingIds?.has(block.event.id) ?? false
                const top = ((block.startMin - startH * 60) / 60) * HOUR_PX
                const height = Math.max(((block.endMin - block.startMin) / 60) * HOUR_PX - 2, 18)
                const compact = height < 36
                const goalTitle = block.event.goal_links[0]?.goal_title
                return (
                  <div
                    key={block.event.id}
                    className={`${styles.evt} ${bound ? styles.evtGoal : ''} ${compact ? styles.evtCompact : ''} ${pending ? styles.evtPending : ''}`}
                    style={{
                      top,
                      height,
                      left: `calc(${(block.col / block.cols) * 100}% + 3px)`,
                      width: `calc(${100 / block.cols}% - 6px)`,
                    }}
                    onClick={(clickEvent) => {
                      clickEvent.stopPropagation()
                      if (pending) return
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
                  aria-hidden="true"
                >
                  ＋
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
