import { type CSSProperties, useEffect, useMemo, useRef, useState } from 'react'
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
/** 一天的小时数：网格恒为 00:00–24:00，靠滚动而不是裁剪来避开空白。 */
const DAY_HOURS = 24
/** 一周没有定时事件时，视野落在哪个钟点。 */
const EMPTY_WEEK_FOCUS_HOUR = 8
/** 焦点上方留出的上文，让人看得见"这之前是空的"。 */
const FOCUS_LEAD_HOURS = 1

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
  const headRowRef = useRef<HTMLDivElement>(null)
  const allDayRowRef = useRef<HTMLDivElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)

  const weekKey = weekDays.join(',')
  /* 网格铺满一整天。曾经只铺到"有事情发生的那几个小时"以躲开空格子，代价是
     当周最早的事在十点以后时，早晨整段既看不见也点不到——而新建日程正是点空
     格子。改成恒定 00:00–24:00 + 卡片内滚：空白留在视野之外，而不是不存在。 */
  const { timedByDay, allDayByDay } = useMemo(() => {
    const timed = new Map<string, ScheduleBlock[]>()
    const allDay = new Map<string, ScheduleEvent[]>()
    for (const key of weekDays) {
      const dayEvents = events.filter((event) => eventDateKey(event) === key)
      allDay.set(key, dayEvents.filter((event) => event.all_day))
      timed.set(key, layoutDayBlocks(dayEvents.filter((event) => !event.all_day)))
    }
    return { timedByDay: timed, allDayByDay: allDay }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, weekKey])

  const bodyPx = DAY_HOURS * HOUR_PX

  /* 吸附到半小时。下界留半小时而不是一小时——留一小时的话，一天最后那条
     半小时带永远吸不上去（23:30 会被拽回 23:00），等于有一格是看得见点不着的。 */
  const snapMin = (col: HTMLElement, clientY: number): number => {
    const rect = col.getBoundingClientRect()
    const raw = ((clientY - rect.top) / HOUR_PX) * 60
    return Math.min(Math.max(Math.round(raw / 30) * 30, 0), DAY_HOURS * 60 - 30)
  }

  const nowMin = now.getHours() * 60 + now.getMinutes()

  /* 打开时把视野放在有内容的地方：今天这周对准此刻，别的周对准当周最早的一件事。
     一旦你自己滚动过，这一周就不再自动归位——归位是开场白，不是纠正。 */
  const userMovedRef = useRef(false)
  const autoScrollRef = useRef(false)
  const lastTopRef = useRef(0)
  useEffect(() => {
    userMovedRef.current = false
  }, [weekKey])
  useEffect(() => {
    const el = bodyRef.current
    if (!el || userMovedRef.current) return
    let earliest: number | null = null
    for (const blocks of timedByDay.values()) {
      for (const block of blocks) {
        const hour = Math.floor(block.startMin / 60)
        if (earliest === null || hour < earliest) earliest = hour
      }
    }
    const nowHour = new Date().getHours()
    const anchor = weekDays.includes(today)
      ? Math.min(nowHour, earliest ?? nowHour)
      : earliest ?? EMPTY_WEEK_FOCUS_HOUR
    const top = Math.max(0, (anchor - FOCUS_LEAD_HOURS) * HOUR_PX)
    autoScrollRef.current = true
    lastTopRef.current = top
    el.scrollTop = top
    // 切周的那一瞬 events 还是上一周的，本周的事件晚一步才到——所以数据每变一次
    // 都要重新对准，不能认第一次。判"数据到齐了没有"是认不出来的：这里看到的
    // events 是整段区间的，条数不为零并不代表这一周的已经来了。
    // nowHour 故意不进依赖：跨整点重新归位会在你眼皮底下把网格拽走
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weekKey, timedByDay])

  /* 纵向滚动条会把 body 的 7 列压窄，列头和全天行没有滚动条就会错开一格宽度
     （macOS 的浮层滚动条量出来是 0，Windows 上是十几像素）。量出来补给它们。 */
  const [scrollbarPx, setScrollbarPx] = useState(0)
  useEffect(() => {
    const el = bodyRef.current
    if (!el) return
    const measure = () => setScrollbarPx(el.offsetWidth - el.clientWidth)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const syncHorizontalScroll = (source: HTMLDivElement) => {
    if (source === bodyRef.current) {
      // 程序归位触发的那次 scroll 不算你在看别处
      if (source.scrollTop !== lastTopRef.current) {
        if (autoScrollRef.current) autoScrollRef.current = false
        else userMovedRef.current = true
        lastTopRef.current = source.scrollTop
      }
    }
    const scrollLeft = source.scrollLeft
    for (const row of [headRowRef.current, allDayRowRef.current, bodyRef.current]) {
      if (row && row !== source) row.scrollLeft = scrollLeft
    }
  }

  return (
    <div
      className={styles.card}
      style={{ '--hour-px': `${HOUR_PX}px`, '--scrollbar-px': `${scrollbarPx}px` } as CSSProperties}
    >
      <div ref={headRowRef} className={styles.headRow} onScroll={(event) => syncHorizontalScroll(event.currentTarget)}>
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
      <div ref={allDayRowRef} className={styles.allDayRow} onScroll={(event) => syncHorizontalScroll(event.currentTarget)}>
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

      <div ref={bodyRef} className={styles.body} onScroll={(event) => syncHorizontalScroll(event.currentTarget)}>
        <div className={styles.axisCol} style={{ height: bodyPx }}>
          {/* 只标到 23:00：24:00 那条线就是一天的下边界，标出来反而像多了一小时 */}
          {Array.from({ length: DAY_HOURS }, (_, i) => (
            <span key={i} className={styles.axisLabel} style={{ top: i * HOUR_PX }}>
              {pad2(i)}:00
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
                const top = (block.startMin / 60) * HOUR_PX
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
                  style={{ top: (hover.min / 60) * HOUR_PX }}
                  aria-hidden="true"
                >
                  ＋
                </div>
              )}

              {isToday && (
                <div className={styles.nowLine} style={{ top: (nowMin / 60) * HOUR_PX }} />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
