import { useEffect, useMemo, useRef, useState } from 'react'
import {
  type CreateScheduleEventInput,
  type Goal,
  type ScheduleEvent,
  type ScheduleProgress,
  type ScheduleStatus,
  ApiError,
  createLocalCalendarAccount,
  createScheduleEvent,
  deleteScheduleEvent,
  getScheduleStatus,
  linkGoalSchedule,
  listGoals,
  listScheduleEvents,
  syncSchedule,
  unlinkGoalSchedule,
  updateScheduleEvent,
} from '@/api/client'
import {
  getCachedScheduleStatus,
  hasLocalCalendarAccount,
  invalidateScheduleStatusCache,
  setCachedScheduleStatus,
} from '@/utils/scheduleStatusCache'
import { Notice } from '@/components/Notice'
import { ScheduleEventDrawer } from '@/components/ScheduleEventDrawer'
import { ScheduleMigrationDialog } from '@/components/ScheduleMigrationDialog'
import { ScheduleEventPopover } from '@/components/ScheduleEventPopover'
import { ScheduleWeekGrid } from '@/components/ScheduleWeekGrid'
import { ScheduleMonthGrid } from '@/components/ScheduleMonthGrid'
import { PlusIcon, RefreshCwIcon } from '@/components/icons'
import { formatSmartTime } from '@/utils/date'
import {
  dateFromKey,
  eventClock,
  eventDateKey,
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

/** 一笔后台在途的写操作及其乐观覆盖项。 */
type PendingOp = {
  /** 递增唯一，定位 op。 */
  key: number
  type: 'create' | 'update' | 'delete'
  /** create 初始为 `pending_${key}`，成功后换成服务器真实 id。 */
  eventId: string
  /** create/update 的乐观事件；delete 为 null。 */
  optimistic: ScheduleEvent | null
  /** null=在飞；成功落定后 = ++epoch（用于对账清除）。 */
  settledEpoch: number | null
}

/** 一条失败提示（失败后 op 已从覆盖层移除，仅剩这条横幅）。 */
type FailedOp = { key: number; message: string; retry?: () => void }

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
  const [creatingLocal, setCreatingLocal] = useState(false)
  const [migration, setMigration] = useState<{ source: 'prompt' | 'settings' } | null>(null)
  /** 本次挂载是否已自动弹过迁移邀请（防 StrictMode / 重复触发）。 */
  const migrationPromptShown = useRef(false)

  /* 后台静默同步：乐观覆盖层 + 失败横幅 + epoch 对账。 */
  const [pendingOps, setPendingOps] = useState<PendingOp[]>([])
  const [failedOps, setFailedOps] = useState<FailedOp[]>([])
  /** 单调递增的落定序号；对账凭它判断某次拉取是否已包含该改动。 */
  const epochRef = useRef(0)
  /** op / 失败横幅的唯一 key 源。 */
  const opKeyRef = useRef(0)

  const connected = status?.connected ?? false
  const hasLocal = hasLocalCalendarAccount(status)
  const localEventCount =
    status?.accounts?.find((account) => account.provider === 'local')?.event_count ?? 0
  /** 有任一可写账号（Outlook 已连或本地日历已创建）即可使用日历。 */
  const usable = connected || hasLocal
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
        /* 已连接 且 有本地日程 且未 dismiss → 自动弹一次迁移邀请。 */
        if (statusData.migration_prompt_pending && !migrationPromptShown.current) {
          migrationPromptShown.current = true
          setMigration({ source: 'prompt' })
        }
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
    if (!usable) {
      setEvents([])
      setProgressByGoal({})
      return
    }
    /* 对账基线：只有「本次拉取开始时已 settle」的 op 才被这次响应证明落地。 */
    const fetchEpoch = epochRef.current
    let cancelled = false
    void (async () => {
      try {
        const result = await listScheduleEvents(range.start, range.end)
        if (cancelled) return
        setEvents(result.events)
        /* 摘掉覆盖层：仅清除落定序号 ≤ 本次拉取起点的 op（服务器基线已含该改动）。
           在飞（settledEpoch===null）或「拉取起点之后才落定」的 op 一律保留，
           避免 pending create 闪没 / pending delete 闪回。与 setEvents 同批渲染。 */
        setPendingOps((ops) =>
          ops.filter((op) => !(op.settledEpoch !== null && op.settledEpoch <= fetchEpoch)),
        )
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
  }, [usable, range.start, range.end, goals, reloadKey])

  /* 服务器基线 + 乐观覆盖层的合并结果，喂给网格。 */
  const displayEvents = useMemo(() => {
    let merged = events
    for (const op of pendingOps) {
      if (op.type === 'delete') merged = merged.filter((e) => e.id !== op.eventId)
      else if (op.type === 'update') merged = merged.map((e) => (e.id === op.eventId ? op.optimistic! : e))
      /* create：先按 id 去重再追加（settle 后真实 id 可能已被并发 sync 拉进基线）。 */
      else merged = [...merged.filter((e) => e.id !== op.eventId), op.optimistic!]
    }
    return merged
  }, [events, pendingOps])

  /* 仍在飞的事件 id：降透明 + 禁点击；settle 后立即恢复可交互（此时已是真实 id）。 */
  const pendingIds = useMemo(
    () => new Set(pendingOps.filter((op) => op.settledEpoch === null).map((op) => op.eventId)),
    [pendingOps],
  )

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

  /* 「先用本地日历」：创建本地账号后直接进入日历。 */
  const handleUseLocal = async () => {
    setCreatingLocal(true)
    setError(null)
    try {
      await createLocalCalendarAccount()
      const statusData = await getScheduleStatus()
      setStatus(statusData)
      setCachedScheduleStatus(statusData)
      setNotice('已创建本地日历。日程仅保存在这台设备，连接云端账号可多端同步。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建本地日历失败')
    } finally {
      setCreatingLocal(false)
    }
  }

  /** 迁移完成 / 终态退出：失效缓存并重拉状态 + 事件（本地账号可能已移除）。 */
  const handleMigrationFinished = async () => {
    setMigration(null)
    invalidateScheduleStatusCache()
    try {
      const statusData = await getScheduleStatus()
      setStatus(statusData)
      setCachedScheduleStatus(statusData)
    } catch {
      /* 状态刷新失败忽略：下次进页会重拉 */
    }
    setReloadKey((key) => key + 1)
  }

  const openEvent = (event: ScheduleEvent, at: { x: number; y: number }) => {
    /* 双保险：在飞事件不开 Popover（网格已禁点击，此处再拦一道）。 */
    if (pendingIds.has(event.id)) return
    setPopover({ event, anchor: at })
  }
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

  /** 可写账号列表（顺序即 Drawer 默认值优先级：Outlook 优先）。 */
  const writableAccounts = useMemo(() => {
    const list: { id: string; label: string }[] = []
    if (connected) list.push({ id: 'outlook', label: 'Outlook' })
    if (hasLocal) list.push({ id: 'local', label: '本地日历' })
    return list
  }, [connected, hasLocal])

  /* ===== 后台静默提交：乐观构造 + 失败横幅 + 三个提交函数 ===== */

  const pushFailed = (message: string, retry?: () => void) => {
    const key = ++opKeyRef.current
    setFailedOps((list) => [...list, { key, message, retry }])
  }
  const dismissFailed = (key: number) => {
    setFailedOps((list) => list.filter((item) => item.key !== key))
  }

  /** 由 goalId 查 goal_title 拼出单条绑定（或空）；对照 ScheduleGoalLink 形状。 */
  const goalLinksFor = (goalId: string | null): ScheduleEvent['goal_links'] =>
    goalId ? [{ goal_id: goalId, goal_title: goals.find((g) => g.id === goalId)?.title ?? '' }] : []

  /** create 的乐观事件；字段名对齐 ScheduleEvent，时间字符串对齐后端墙钟。 */
  const buildOptimisticCreate = (input: CreateScheduleEventInput, key: number): ScheduleEvent => {
    const allDay = input.all_day ?? false
    const startLocal = allDay ? `${input.date}T00:00:00` : `${input.date}T${input.start_time ?? '00:00'}:00`
    const endLocal = allDay
      ? `${shiftDateKey(input.date, 1)}T00:00:00`
      : `${input.date}T${input.end_time ?? '00:00'}:00`
    const accountId = input.account_id ?? writableAccounts[0]?.id ?? 'outlook'
    return {
      id: `pending_${key}`,
      subject: input.subject,
      body_preview: null,
      start_ts: Math.floor(new Date(startLocal).getTime() / 1000),
      end_ts: Math.floor(new Date(endLocal).getTime() / 1000),
      start_local: startLocal,
      end_local: endLocal,
      all_day: allDay,
      location: null,
      web_link: null,
      series_master_id: null,
      is_cancelled: false,
      change_key: null,
      synced_at: Math.floor(Date.now() / 1000),
      account_id: accountId,
      provider: accountId === 'local' ? 'local' : 'outlook',
      goal_link: null,
      goal_links: goalLinksFor(input.goal_id ?? null),
    }
  }

  /** update 的乐观事件：原事件覆盖 subject/all_day 与重算的四个时间字段，goal_links 按 diff 重写。 */
  const buildOptimisticUpdate = (
    event: ScheduleEvent,
    fields: Partial<CreateScheduleEventInput>,
    goalTo: string | null,
  ): ScheduleEvent => {
    const allDay = fields.all_day ?? event.all_day
    const date = fields.date ?? eventDateKey(event)
    const startLocal = allDay ? `${date}T00:00:00` : `${date}T${fields.start_time ?? eventClock(event.start_local)}:00`
    const endLocal = allDay
      ? `${shiftDateKey(date, 1)}T00:00:00`
      : `${date}T${fields.end_time ?? eventClock(event.end_local)}:00`
    return {
      ...event,
      subject: fields.subject ?? event.subject,
      all_day: allDay,
      start_local: startLocal,
      end_local: endLocal,
      start_ts: Math.floor(new Date(startLocal).getTime() / 1000),
      end_ts: Math.floor(new Date(endLocal).getTime() / 1000),
      goal_links: goalLinksFor(goalTo),
    }
  }

  const submitCreate = (input: CreateScheduleEventInput) => {
    const key = ++opKeyRef.current
    const optimistic = buildOptimisticCreate(input, key)
    setPendingOps((ops) => [...ops, { key, type: 'create', eventId: optimistic.id, optimistic, settledEpoch: null }])
    void (async () => {
      try {
        const created = await createScheduleEvent(input)
        epochRef.current += 1
        const settledEpoch = epochRef.current
        setPendingOps((ops) =>
          ops.map((op) => (op.key === key ? { ...op, eventId: created.id, optimistic: created, settledEpoch } : op)),
        )
        setReloadKey((k) => k + 1)
      } catch (err) {
        setPendingOps((ops) => ops.filter((op) => op.key !== key))
        if (err instanceof ApiError && err.status === 409) {
          /* 无可用账号：给出去处指引，且不给 retry（重试仍会 409）。 */
          pushFailed('没有可用的日历账号：请先在设置中登录 Microsoft，或创建本地日历。')
        } else {
          pushFailed(`「${input.subject}」创建失败：${errMessage(err)}`, () => submitCreate(input))
        }
      }
    })()
  }

  const submitUpdate = (
    event: ScheduleEvent,
    fields: Partial<CreateScheduleEventInput>,
    goalDiff: { from: string | null; to: string | null },
  ) => {
    const key = ++opKeyRef.current
    const optimistic = buildOptimisticUpdate(event, fields, goalDiff.to)
    setPendingOps((ops) => [...ops, { key, type: 'update', eventId: event.id, optimistic, settledEpoch: null }])
    void (async () => {
      let updated: ScheduleEvent
      try {
        updated = await updateScheduleEvent(event.id, fields)
      } catch (err) {
        /* PATCH 本身失败：回滚显示原事件，整体重跑可 retry。 */
        setPendingOps((ops) => ops.filter((op) => op.key !== key))
        pushFailed(`「${fields.subject ?? event.subject}」保存失败：${errMessage(err)}`, () =>
          submitUpdate(event, fields, goalDiff),
        )
        return
      }
      /* PATCH 成功后顺序处理绑定；appliedGoalId 记录服务器实际生效到哪一步。 */
      let linkFailed = false
      let appliedGoalId = goalDiff.from
      if (goalDiff.from !== goalDiff.to) {
        try {
          if (goalDiff.from) await unlinkGoalSchedule(goalDiff.from, event.id)
          appliedGoalId = null
          if (goalDiff.to) {
            await linkGoalSchedule(goalDiff.to, event.id)
            appliedGoalId = goalDiff.to
          }
        } catch {
          linkFailed = true
        }
      }
      const settledEvent: ScheduleEvent = { ...updated, goal_links: goalLinksFor(appliedGoalId) }
      epochRef.current += 1
      const settledEpoch = epochRef.current
      setPendingOps((ops) => ops.map((op) => (op.key === key ? { ...op, optimistic: settledEvent, settledEpoch } : op)))
      if (linkFailed) {
        /* 部分成功：无 retry（盲目重跑 unlink 会对不存在的链接报错），对账拉取会带权威值。 */
        pushFailed(`「${settledEvent.subject}」已保存，但目标绑定更新失败，请重新编辑`)
      }
      setReloadKey((k) => k + 1)
    })()
  }

  const submitDelete = (event: ScheduleEvent) => {
    const key = ++opKeyRef.current
    setPendingOps((ops) => [...ops, { key, type: 'delete', eventId: event.id, optimistic: null, settledEpoch: null }])
    void (async () => {
      try {
        await deleteScheduleEvent(event.id)
        epochRef.current += 1
        const settledEpoch = epochRef.current
        setPendingOps((ops) => ops.map((op) => (op.key === key ? { ...op, settledEpoch } : op)))
        setReloadKey((k) => k + 1)
      } catch (err) {
        setPendingOps((ops) => ops.filter((op) => op.key !== key))
        pushFailed(`「${event.subject}」删除失败：${errMessage(err)}`, () => submitDelete(event))
      }
    })()
  }

  return (
    <div className={workspaceStyles.page}>
      <header className={workspaceStyles.header}>
        <div className={workspaceStyles.titleGroup}>
          <h1 className={workspaceStyles.title}>日程</h1>
          <p className={workspaceStyles.subtitle}>{subtitle}</p>
        </div>
        {usable && (
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
            {connected && (
              <button className={styles.ghostBtn} type="button" onClick={() => void handleSync()} disabled={syncing}>
                <RefreshCwIcon />
                {syncing ? '同步中...' : '立即同步'}
              </button>
            )}
            <button className={styles.primaryBtn} type="button" onClick={() => setDrawer({ mode: 'create' })}>
              <PlusIcon />
              新建日程
            </button>
          </div>
        )}
      </header>

      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}
      {notice && <Notice kind="success" onClose={() => setNotice(null)}>{notice}</Notice>}
      {failedOps.map((failed) => (
        <Notice
          key={failed.key}
          kind="error"
          actions={
            failed.retry && (
              <button
                className={workspaceStyles.ghostButton}
                type="button"
                onClick={() => {
                  /* 先撤掉本条，再重新提交，避免旧横幅残留。 */
                  dismissFailed(failed.key)
                  failed.retry?.()
                }}
              >
                重试
              </button>
            )
          }
          onClose={() => dismissFailed(failed.key)}
        >
          {failed.message}
        </Notice>
      ))}

      {loading && status === null ? (
        <div className={workspaceStyles.empty}>加载中...</div>
      ) : status === null ? (
        <div className={workspaceStyles.empty}>连接状态获取失败，稍后再试。</div>
      ) : !usable ? (
        <div className={styles.guide}>
          <span className={styles.guideIcon}><CalendarBigIcon /></span>
          <p className={styles.guideTitle}>还没有日历账号</p>
          <p className={styles.guideHint}>
            连接 Microsoft 账户后，日程与 Outlook 实时同步、多端可见；也可以先用只保存在这台设备上的本地日历。
          </p>
          <button className={workspaceStyles.button} type="button" onClick={onOpenSettings}>
            连接 Microsoft 账户（推荐）
          </button>
          <button
            className={styles.guideSecondary}
            type="button"
            onClick={() => void handleUseLocal()}
            disabled={creatingLocal}
          >
            {creatingLocal ? '创建中...' : '先用本地日历'}
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
              events={displayEvents}
              onEventClick={openEvent}
              onCreateSlot={openCreateSlot}
              pendingIds={pendingIds}
            />
          ) : (
            <ScheduleMonthGrid
              cells={monthCells}
              today={today}
              events={displayEvents}
              onEventClick={openEvent}
              onDayClick={openDayWeek}
              pendingIds={pendingIds}
            />
          )}

          <p className={styles.syncMeta}>
            {connected
              ? `${status?.last_sync_at ? `上次同步 ${formatSmartTime(status.last_sync_at)}` : '尚未同步'} · 改动实时写回 Outlook${hasLocal ? ' · 本地日程仅保存在本机' : ''}`
              : '本地日程仅保存在这台设备，连接云端账号可多端同步'}
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
          onDelete={(deleted) => {
            setPopover(null)
            submitDelete(deleted)
          }}
        />
      )}

      {drawer && (
        <ScheduleEventDrawer
          goals={goals}
          accounts={writableAccounts}
          prefill={drawer.mode === 'create' ? drawer.prefill : undefined}
          event={drawer.mode === 'edit' ? drawer.event : undefined}
          onClose={() => setDrawer(null)}
          onSubmit={(submission) => {
            setDrawer(null)
            if (submission.kind === 'create') submitCreate(submission.input)
            else submitUpdate(submission.event, submission.fields, submission.goalDiff)
          }}
        />
      )}

      {migration && (
        <ScheduleMigrationDialog
          source={migration.source}
          localEventCount={localEventCount}
          onClose={() => setMigration(null)}
          onFinished={() => void handleMigrationFinished()}
        />
      )}
    </div>
  )
}

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : '未知错误'
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
