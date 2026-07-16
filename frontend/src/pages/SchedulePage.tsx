import { useEffect, useMemo, useState } from 'react'
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
import { getCachedScheduleStatus, setCachedScheduleStatus } from '@/utils/scheduleStatusCache'
import { Notice } from '@/components/Notice'
import { ScheduleEventDrawer } from '@/components/ScheduleEventDrawer'
import { ScheduleEventPopover } from '@/components/ScheduleEventPopover'
import { ScheduleWeekGrid } from '@/components/ScheduleWeekGrid'
import { ScheduleMonthGrid } from '@/components/ScheduleMonthGrid'
import { PlusIcon, RefreshCwIcon } from '@/components/icons'
import { formatSmartTime } from '@/utils/date'
import {
  dateFromKey,
  fetchGoalProgress,
  monthCalendarCells,
  monthTitleLabel,
  shiftDateKey,
  shiftMonthKey,
  todayKey,
  weekKeysFor,
  weekRangeLabel,
} from '@/utils/schedule'
import workspaceStyles from './WorkspacePages.module.css'
import styles from './SchedulePage.module.css'

type CalendarView = 'week' | 'month'
type PopoverState = { event: ScheduleEvent; anchor: { x: number; y: number } }
type DrawerState =
  | { mode: 'create'; prefill?: { date: string; start_time?: string; end_time?: string } }
  | { mode: 'edit'; event: ScheduleEvent }

interface SchedulePageProps {
  onOpenSettings: () => void
}

export function SchedulePage({ onOpenSettings }: SchedulePageProps) {
  /* 先用上次已知状态渲染，避免切页时把"加载中"误显示为未连接引导（P9）。 */
  const [status, setStatus] = useState<ScheduleStatus | null>(() => getCachedScheduleStatus())
  const [view, setView] = useState<CalendarView>('week')
  const [anchor, setAnchor] = useState<string>(() => todayKey())
  const [events, setEvents] = useState<ScheduleEvent[]>([])
  const [goals, setGoals] = useState<Goal[]>([])
  const [progressByGoal, setProgressByGoal] = useState<Record<string, ScheduleProgress>>({})
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [popover, setPopover] = useState<PopoverState | null>(null)
  const [drawer, setDrawer] = useState<DrawerState | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  const connected = status?.connected ?? false
  const today = todayKey()

  const weekDays = useMemo(() => weekKeysFor(anchor), [anchor])
  const monthCells = useMemo(() => {
    const date = dateFromKey(anchor)
    return monthCalendarCells(date.getFullYear(), date.getMonth() + 1)
  }, [anchor])
  const range = useMemo(() => {
    if (view === 'week') return { start: weekDays[0]!, end: weekDays[6]! }
    return { start: monthCells[0]!.key, end: monthCells[monthCells.length - 1]!.key }
  }, [view, weekDays, monthCells])

  /* 状态 + 目标：进页拉一次。 */
  useEffect(() => {
    let cancelled = false
    void (async () => {
      setLoading(true)
      try {
        const [statusData, goalData] = await Promise.all([
          getScheduleStatus(),
          listGoals({ status: 'active' }),
        ])
        if (cancelled) return
        setStatus(statusData)
        setCachedScheduleStatus(statusData)
        setGoals(goalData)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : '日程加载失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  /* 事件 + 目标进度：随视图/锚点/目标/刷新信号变化重拉当前区间。 */
  useEffect(() => {
    if (!connected) {
      setEvents([])
      setProgressByGoal({})
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const result = await listScheduleEvents(range.start, range.end)
        if (cancelled) return
        setEvents(result.events)
        const ids = new Set<string>()
        result.events.forEach((event) => event.goal_links.forEach((link) => ids.add(link.goal_id)))
        goals.forEach((goal) => ids.add(goal.id))
        const progress = await fetchGoalProgress([...ids])
        if (cancelled) return
        setProgressByGoal(progress)
        setError(null)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : '日程加载失败')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [connected, range.start, range.end, goals, reloadKey])

  const goPrev = () => setAnchor((a) => (view === 'week' ? shiftDateKey(a, -7) : shiftMonthKey(a, -1)))
  const goNext = () => setAnchor((a) => (view === 'week' ? shiftDateKey(a, 7) : shiftMonthKey(a, 1)))
  const goToday = () => setAnchor(todayKey())

  /* 键盘：←/→ 导航、T 回今天、Esc 关浮层；输入框内或抽屉打开时不劫持导航。 */
  useEffect(() => {
    const onKey = (keyEvent: KeyboardEvent) => {
      const tag = (keyEvent.target as HTMLElement | null)?.tagName
      const typing = tag === 'INPUT' || tag === 'TEXTAREA'
      if (keyEvent.key === 'Escape') {
        setPopover(null)
        return
      }
      if (typing || drawer) return
      if (keyEvent.key === 'ArrowLeft') {
        keyEvent.preventDefault()
        goPrev()
      } else if (keyEvent.key === 'ArrowRight') {
        keyEvent.preventDefault()
        goNext()
      } else if (keyEvent.key === 't' || keyEvent.key === 'T') {
        goToday()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, drawer])

  const handleSync = async () => {
    setSyncing(true)
    setNotice(null)
    setError(null)
    try {
      const result = await syncSchedule()
      if (result.ok) {
        setNotice(`同步完成：更新 ${result.upserted} 条，移除 ${result.deleted} 条。`)
        const statusData = await getScheduleStatus()
        setStatus(statusData)
        setCachedScheduleStatus(statusData)
        setReloadKey((key) => key + 1)
      } else {
        setError('Microsoft 日历尚未连接，请先在设置中登录。')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '同步失败')
    } finally {
      setSyncing(false)
    }
  }

  const handleSaved = () => {
    const editing = drawer?.mode === 'edit'
    setDrawer(null)
    setNotice(editing ? '日程已更新。' : '日程已保存。')
    setReloadKey((key) => key + 1)
  }

  const handleDeleted = () => {
    setPopover(null)
    setNotice('日程已删除。')
    setReloadKey((key) => key + 1)
  }

  const openEvent = (event: ScheduleEvent, at: { x: number; y: number }) =>
    setPopover({ event, anchor: at })
  const openCreateSlot = (date: string, startTime: string, endTime: string) =>
    setDrawer({ mode: 'create', prefill: { date, start_time: startTime, end_time: endTime } })
  const openDayWeek = (dateKey: string) => {
    setAnchor(dateKey)
    setView('week')
  }
  const editFromPopover = (event: ScheduleEvent) => {
    setPopover(null)
    setDrawer({ mode: 'edit', event })
  }

  const goalStripItems = useMemo(
    () =>
      goals
        .map((goal) => ({ goal, progress: progressByGoal[goal.id] }))
        .filter(
          (item): item is { goal: Goal; progress: ScheduleProgress } =>
            item.progress?.expectation != null,
        ),
    [goals, progressByGoal],
  )

  const subtitle = view === 'week' ? weekRangeLabel(weekDays) : monthTitleLabel(anchor)

  return (
    <div className={workspaceStyles.page}>
      <header className={workspaceStyles.header}>
        <div className={workspaceStyles.titleGroup}>
          <h1 className={workspaceStyles.title}>日程</h1>
          <p className={workspaceStyles.subtitle}>{subtitle}</p>
        </div>
        {connected && (
          <div className={styles.controls}>
            <button className={styles.ghostBtn} type="button" onClick={goToday}>今天</button>
            <span className={styles.arrowGroup}>
              <button className={styles.arrowBtn} type="button" onClick={goPrev} aria-label="上一页">‹</button>
              <button className={styles.arrowBtn} type="button" onClick={goNext} aria-label="下一页">›</button>
            </span>
            <span className={styles.segmented}>
              <button
                className={`${styles.segBtn} ${view === 'week' ? styles.segActive : ''}`}
                type="button"
                onClick={() => setView('week')}
              >
                周
              </button>
              <button
                className={`${styles.segBtn} ${view === 'month' ? styles.segActive : ''}`}
                type="button"
                onClick={() => setView('month')}
              >
                月
              </button>
            </span>
            <button className={styles.ghostBtn} type="button" onClick={() => void handleSync()} disabled={syncing}>
              <RefreshCwIcon />
              {syncing ? '同步中...' : '立即同步'}
            </button>
            <button className={styles.primaryBtn} type="button" onClick={() => setDrawer({ mode: 'create' })}>
              <PlusIcon />
              新建日程
            </button>
          </div>
        )}
      </header>

      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}
      {notice && <Notice kind="success" onClose={() => setNotice(null)}>{notice}</Notice>}

      {loading && status === null ? (
        <div className={workspaceStyles.empty}>加载中...</div>
      ) : status === null ? (
        <div className={workspaceStyles.empty}>连接状态获取失败，稍后再试。</div>
      ) : !connected ? (
        <div className={styles.guide}>
          <span className={styles.guideIcon}><CalendarBigIcon /></span>
          <p className={styles.guideTitle}>还没有连接日历</p>
          <p className={styles.guideHint}>
            拾迹通过你的 Microsoft 账户读写 Outlook 日历。连接后，这里会以日历视图展示你的日程，也能把日程绑定到目标。
          </p>
          <button className={workspaceStyles.button} type="button" onClick={onOpenSettings}>
            去设置连接
          </button>
        </div>
      ) : (
        <>
          {view === 'week' && goalStripItems.length > 0 && (
            <div className={styles.goalStrip}>
              <span className={styles.goalStripLabel}>本周目标</span>
              {goalStripItems.map(({ goal, progress }) => {
                const target = progress.expectation!.target
                const current = progress.current
                return (
                  <span key={goal.id} className={styles.goalChip}>
                    <span className={styles.gname}>{goal.title}</span>
                    {target <= 7 && (
                      <span className={styles.goalDots}>
                        {Array.from({ length: target }, (_, i) => (
                          <i key={i} className={`${styles.goalDot} ${i < current ? styles.goalDotDone : ''}`} />
                        ))}
                      </span>
                    )}
                    <span className={styles.goalNum}>{current}/{target} · {progress.expectation!.label}</span>
                  </span>
                )
              })}
            </div>
          )}

          {view === 'week' ? (
            <ScheduleWeekGrid
              weekDays={weekDays}
              today={today}
              events={events}
              onEventClick={openEvent}
              onCreateSlot={openCreateSlot}
            />
          ) : (
            <ScheduleMonthGrid
              cells={monthCells}
              today={today}
              events={events}
              onEventClick={openEvent}
              onDayClick={openDayWeek}
            />
          )}

          <p className={styles.syncMeta}>
            {status?.last_sync_at ? `上次同步 ${formatSmartTime(status.last_sync_at)}` : '尚未同步'} · 改动实时写回 Outlook
          </p>
          <p className={styles.kbdHint}>
            <kbd className={styles.kbd}>←</kbd><kbd className={styles.kbd}>→</kbd> 切换 ·{' '}
            <kbd className={styles.kbd}>T</kbd> 回到今天 · 点击空白时段新建 ·{' '}
            <span className={styles.warmSwatch} /> 琥珀色 = 已绑定目标的日程
          </p>
        </>
      )}

      {popover && (
        <ScheduleEventPopover
          event={popover.event}
          anchor={popover.anchor}
          progressByGoal={progressByGoal}
          onClose={() => setPopover(null)}
          onEdit={editFromPopover}
          onDeleted={handleDeleted}
        />
      )}

      {drawer && (
        <ScheduleEventDrawer
          goals={goals}
          prefill={drawer.mode === 'create' ? drawer.prefill : undefined}
          event={drawer.mode === 'edit' ? drawer.event : undefined}
          onClose={() => setDrawer(null)}
          onSaved={handleSaved}
        />
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
