import { useCallback, useEffect, useState } from 'react'
import {
  type Goal,
  type ScheduleEvent,
  type ScheduleProgress,
  type ScheduleStatus,
  getScheduleStatus,
  listGoals,
  listScheduleEvents,
  syncSchedule,
} from '@/api/client'
import { Notice } from '@/components/Notice'
import { ScheduleEventDrawer } from '@/components/ScheduleEventDrawer'
import { ScheduleList } from '@/components/ScheduleList'
import { PlusIcon, RefreshCwIcon } from '@/components/icons'
import { formatSmartTime } from '@/utils/date'
import {
  eventDateKey,
  fetchGoalProgress,
  monthDayLabel,
  relativeDayLabel,
  todayKey,
  weekKeys,
  weekdayLabel,
} from '@/utils/schedule'
import workspaceStyles from './WorkspacePages.module.css'
import styles from './SchedulePage.module.css'

interface SchedulePageProps {
  onOpenSettings: () => void
}

export function SchedulePage({ onOpenSettings }: SchedulePageProps) {
  const [status, setStatus] = useState<ScheduleStatus | null>(null)
  const [events, setEvents] = useState<ScheduleEvent[]>([])
  const [goals, setGoals] = useState<Goal[]>([])
  const [progressByGoal, setProgressByGoal] = useState<Record<string, ScheduleProgress>>({})
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const week = weekKeys()
  const today = todayKey()
  const connected = status?.connected ?? false

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [statusData, goalData] = await Promise.all([
        getScheduleStatus(),
        listGoals({ status: 'active' }),
      ])
      setStatus(statusData)
      setGoals(goalData)
      if (statusData.connected) {
        const keys = weekKeys()
        const result = await listScheduleEvents(keys[0]!, keys[6]!)
        setEvents(result.events)
        const goalIds = result.events.flatMap((event) => event.goal_links.map((link) => link.goal_id))
        setProgressByGoal(await fetchGoalProgress(goalIds))
      } else {
        setEvents([])
        setProgressByGoal({})
      }
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '日程加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const handleSync = async () => {
    setSyncing(true)
    setNotice(null)
    setError(null)
    try {
      const result = await syncSchedule()
      if (result.ok) {
        setNotice(`同步完成：更新 ${result.upserted} 条，移除 ${result.deleted} 条。`)
        await refresh()
      } else {
        setError('Microsoft 日历尚未连接，请先在设置中登录。')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '同步失败')
    } finally {
      setSyncing(false)
    }
  }

  const handleCreated = () => {
    setDrawerOpen(false)
    setNotice('日程已创建。')
    void refresh()
  }

  const eventsByDay = new Map<string, ScheduleEvent[]>()
  for (const event of events) {
    const key = eventDateKey(event)
    const list = eventsByDay.get(key) ?? []
    list.push(event)
    eventsByDay.set(key, list)
  }

  return (
    <div className={workspaceStyles.page}>
      <header className={workspaceStyles.header}>
        <div className={workspaceStyles.titleGroup}>
          <h1 className={workspaceStyles.title}>日程</h1>
          <p className={workspaceStyles.subtitle}>
            {monthDayLabel(week[0]!)} - {monthDayLabel(week[6]!)} · 本周
          </p>
        </div>
        {connected && (
          <div className={styles.actions}>
            <button className={workspaceStyles.ghostButton} type="button" onClick={() => void handleSync()} disabled={syncing}>
              <RefreshCwIcon />
              {syncing ? '同步中...' : '立即同步'}
            </button>
            <button className={workspaceStyles.button} type="button" onClick={() => setDrawerOpen(true)}>
              <PlusIcon />
              新建日程
            </button>
          </div>
        )}
      </header>

      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}
      {notice && <Notice kind="success" onClose={() => setNotice(null)}>{notice}</Notice>}

      {loading ? (
        <div className={workspaceStyles.empty}>加载中...</div>
      ) : !connected ? (
        <div className={styles.guide}>
          <span className={styles.guideIcon}><CalendarBigIcon /></span>
          <p className={styles.guideTitle}>还没有连接日历</p>
          <p className={styles.guideHint}>
            拾迹通过你的 Microsoft 账户读写 Outlook 日历。连接后，这里会按周展示你的日程，也能把日程绑定到目标。
          </p>
          <button className={workspaceStyles.button} type="button" onClick={onOpenSettings}>
            去设置连接
          </button>
        </div>
      ) : (
        <>
          <div className={styles.weekList}>
            {week.map((key) => {
              const dayEvents = eventsByDay.get(key) ?? []
              const isToday = key === today
              return (
                <section key={key} className={`${styles.dayGroup} ${isToday ? styles.today : ''}`}>
                  <div className={styles.dayHead}>
                    <span className={styles.dayLabel}>{relativeDayLabel(key, today)}</span>
                    <span className={styles.dayMeta}>{monthDayLabel(key)} {weekdayLabel(key)}</span>
                    {dayEvents.length > 0 && <span className={styles.dayCount}>{dayEvents.length} 项</span>}
                  </div>
                  <ScheduleList
                    events={dayEvents}
                    progressByGoal={progressByGoal}
                    emptyText="这天没有日程。"
                    showLocation
                  />
                </section>
              )
            })}
          </div>
          {status?.last_sync_at && (
            <p className={styles.syncMeta}>上次同步 {formatSmartTime(status.last_sync_at)} · 同步自 Outlook 日历</p>
          )}
        </>
      )}

      {drawerOpen && (
        <ScheduleEventDrawer goals={goals} onClose={() => setDrawerOpen(false)} onCreated={handleCreated} />
      )}
    </div>
  )
}

function CalendarBigIcon() {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="18" rx="2" />
      <line x1="16" y1="2" x2="16" y2="6" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
    </svg>
  )
}
