import { useEffect, useMemo, useState } from 'react'
import { listPostActivity, listScheduleEvents } from '@/api/client'
import {
  WEEKDAY_HEADERS,
  eventDateKey,
  localDateKey,
  monthGrid,
  todayKey,
} from '@/utils/schedule'
import styles from './MiniCalendar.module.css'

interface MiniCalendarProps {
  selectedDate: string | null
  connected: boolean
  onSelectDate: (date: string) => void
}

function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

export function MiniCalendar({ selectedDate, connected, onSelectDate }: MiniCalendarProps) {
  const today = todayKey()
  const [now] = useState(() => new Date())
  const [viewYear, setViewYear] = useState(now.getFullYear())
  const [viewMonth, setViewMonth] = useState(now.getMonth() + 1)
  const [postCounts, setPostCounts] = useState<Record<string, number>>({})
  const [scheduledDays, setScheduledDays] = useState<Set<string>>(new Set())

  const cells = useMemo(() => monthGrid(viewYear, viewMonth), [viewYear, viewMonth])

  useEffect(() => {
    let cancelled = false
    const monthStart = `${viewYear}-${pad2(viewMonth)}-01`
    const lastDay = new Date(viewYear, viewMonth, 0).getDate()
    const monthEnd = `${viewYear}-${pad2(viewMonth)}-${pad2(lastDay)}`

    void listPostActivity(monthStart, monthEnd)
      .then((activity) => {
        if (cancelled) return
        const counts: Record<string, number> = {}
        for (const item of activity) {
          const key = localDateKey(item.ts)
          if (key) counts[key] = (counts[key] ?? 0) + 1
        }
        setPostCounts(counts)
      })
      .catch(() => {
        /* 右栏保持安静：拿不到活跃度时不打扰用户 */
      })

    if (!connected) {
      setScheduledDays(new Set())
      return () => {
        cancelled = true
      }
    }
    void listScheduleEvents(monthStart, monthEnd)
      .then((result) => {
        if (cancelled) return
        const days = new Set<string>()
        for (const event of result.events) days.add(eventDateKey(event))
        setScheduledDays(days)
      })
      .catch(() => {
        if (!cancelled) setScheduledDays(new Set())
      })

    return () => {
      cancelled = true
    }
  }, [viewYear, viewMonth, connected])

  const goToPrevMonth = () => {
    setViewMonth((prev) => {
      if (prev === 1) {
        setViewYear((year) => year - 1)
        return 12
      }
      return prev - 1
    })
  }

  const goToNextMonth = () => {
    setViewMonth((prev) => {
      if (prev === 12) {
        setViewYear((year) => year + 1)
        return 1
      }
      return prev + 1
    })
  }

  return (
    <div className={styles.calCard}>
      <div className={styles.calHead}>
        <span className={styles.calMonth}>{viewYear}年{viewMonth}月</span>
        <span className={styles.calNav}>
          <button className={styles.calNavBtn} type="button" onClick={goToPrevMonth} title="上个月" aria-label="上个月">
            <ChevronIcon direction="left" />
          </button>
          <button className={styles.calNavBtn} type="button" onClick={goToNextMonth} title="下个月" aria-label="下个月">
            <ChevronIcon direction="right" />
          </button>
        </span>
      </div>
      <div className={styles.calGrid}>
        {WEEKDAY_HEADERS.map((label) => (
          <div key={label} className={styles.calDow}>{label}</div>
        ))}
        {cells.map((cell, index) => {
          if (cell.outside) {
            return (
              <div key={`out-${index}`} className={`${styles.calCell} ${styles.outside}`}>
                {cell.day}
              </div>
            )
          }
          const heat = Math.min(postCounts[cell.key] ?? 0, 3)
          const className = [
            styles.calCell,
            cell.key === today ? styles.today : '',
            cell.key === selectedDate ? styles.selected : '',
          ]
            .filter(Boolean)
            .join(' ')
          return (
            <button
              key={cell.key}
              type="button"
              className={className}
              data-heat={heat}
              title={`${viewMonth}月${cell.day}日`}
              aria-pressed={cell.key === selectedDate}
              onClick={() => onSelectDate(cell.key)}
            >
              {cell.day}
              {scheduledDays.has(cell.key) && <span className={styles.schedDot} aria-hidden="true" />}
            </button>
          )
        })}
      </div>
      <div className={styles.calFoot}>
        <span className={styles.k}><span className={styles.kSwatch} />帖子密度</span>
        <span className={styles.k}><span className={styles.kDot} />有日程</span>
      </div>
    </div>
  )
}

function ChevronIcon({ direction }: { direction: 'left' | 'right' }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {direction === 'left' ? <polyline points="15 18 9 12 15 6" /> : <polyline points="9 18 15 12 9 6" />}
    </svg>
  )
}
