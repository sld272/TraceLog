import { useEffect, useState } from 'react'
import {
  type MemoryOperation,
  type ScheduleEvent,
  type ScheduleProgress,
  type ScheduleStatus,
  getScheduleStatus,
  listMemoryOperations,
  listScheduleEvents,
} from '@/api/client'
import { formatSmartTime } from '@/utils/date'
import { fetchGoalProgress, monthDayLabel, todayKey } from '@/utils/schedule'
import { ChevronRightIcon } from '@/components/icons'
import { MiniCalendar } from '@/components/MiniCalendar'
import { ScheduleList } from '@/components/ScheduleList'
import styles from './RightPanel.module.css'

interface RightPanelProps {
  searchQuery: string
  onSearchQueryChange: (value: string) => void
  onOpenMemory: () => void
  /** 日期透镜当前选中的日期（null = 最新流）。 */
  selectedDate: string | null
  onSelectDate: (date: string) => void
  onOpenSchedule: () => void
  onOpenSettings: () => void
}

export function RightPanel({
  searchQuery,
  onSearchQueryChange,
  onOpenMemory,
  selectedDate,
  onSelectDate,
  onOpenSchedule,
  onOpenSettings,
}: RightPanelProps) {
  const [status, setStatus] = useState<ScheduleStatus | null>(null)

  useEffect(() => {
    let cancelled = false
    void getScheduleStatus()
      .then((data) => {
        if (!cancelled) setStatus(data)
      })
      .catch(() => {
        /* 右栏保持安静：拿不到日程状态时按未连接降级 */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const connected = status?.connected ?? false

  return (
    <div className={styles.panel}>
      <div className={styles.panelSearch}>
        <SearchIcon />
        <input
          value={searchQuery}
          onChange={(event) => onSearchQueryChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') onSearchQueryChange('')
          }}
          placeholder="搜索动态…"
          aria-label="搜索动态"
        />
        {searchQuery && (
          <button className={styles.panelSearchClear} onClick={() => onSearchQueryChange('')} aria-label="清空搜索" title="清空搜索">
            ×
          </button>
        )}
      </div>
      <MiniCalendar selectedDate={selectedDate} connected={connected} onSelectDate={onSelectDate} />
      <ScheduleDayCard
        targetDate={selectedDate ?? todayKey()}
        isToday={selectedDate === null}
        connected={connected}
        onOpenSchedule={onOpenSchedule}
        onOpenSettings={onOpenSettings}
      />
      <MemoryPulseCard onOpenMemory={onOpenMemory} />
    </div>
  )
}

function ScheduleDayCard({
  targetDate,
  isToday,
  connected,
  onOpenSchedule,
  onOpenSettings,
}: {
  targetDate: string
  isToday: boolean
  connected: boolean
  onOpenSchedule: () => void
  onOpenSettings: () => void
}) {
  const [events, setEvents] = useState<ScheduleEvent[]>([])
  const [progressByGoal, setProgressByGoal] = useState<Record<string, ScheduleProgress>>({})

  useEffect(() => {
    if (!connected) {
      setEvents([])
      setProgressByGoal({})
      return
    }
    let cancelled = false
    void listScheduleEvents(targetDate, targetDate)
      .then(async (result) => {
        if (cancelled) return
        setEvents(result.events)
        const goalIds = result.events.flatMap((event) => event.goal_links.map((link) => link.goal_id))
        const progress = await fetchGoalProgress(goalIds)
        if (!cancelled) setProgressByGoal(progress)
      })
      .catch(() => {
        if (!cancelled) {
          setEvents([])
          setProgressByGoal({})
        }
      })
    return () => {
      cancelled = true
    }
  }, [targetDate, connected])

  const title = isToday ? '今日日程' : `${monthDayLabel(targetDate)}日程`
  const emptyText = isToday ? '今天没有日程，安心记录就好。' : '这天没有日程。'

  return (
    <section className={styles.card}>
      <PanelHeader title={title} onMore={onOpenSchedule} />
      {!connected ? (
        <p className={styles.schedGuide}>
          连接 Outlook 日历后，这里会显示你的日程。
          <button type="button" className={styles.schedGuideLink} onClick={onOpenSettings}>去设置连接</button>
        </p>
      ) : (
        <>
          <ScheduleList events={events.slice(0, 4)} progressByGoal={progressByGoal} emptyText={emptyText} />
          {events.length > 0 && (
            <div className={styles.schedSource}>
              <SyncIcon />
              同步自 Outlook 日历
            </div>
          )}
        </>
      )}
    </section>
  )
}

function SyncIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21.5 12a9.5 9.5 0 1 1-9.5-9.5" />
      <path d="M21.5 2.5 12 12" />
    </svg>
  )
}

function MemoryPulseCard({ onOpenMemory }: { onOpenMemory: () => void }) {
  const [entries, setEntries] = useState<PulseEntry[]>([])

  useEffect(() => {
    let cancelled = false
    void listMemoryOperations(30)
      .then((data) => {
        if (!cancelled) setEntries(pulseEntries(data))
      })
      .catch(() => {
        /* 右栏保持安静：拿不到记忆变化时不打扰用户 */
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <section className={styles.card}>
      <PanelHeader title="最近记忆变化" onMore={onOpenMemory} />
      <div className={styles.itemList}>
        {entries.length > 0 ? (
          <div className={styles.pulseList}>
            {entries.map((entry) => (
              <button key={entry.key} type="button" className={styles.pulse} onClick={onOpenMemory}>
                <span className={styles.pulseTitle}>{entry.title}</span>
                <span className={styles.pulseMeta}>{entry.meta}</span>
              </button>
            ))}
          </div>
        ) : (
          <p className={styles.empty}>还没有记忆变化。和 AI 好友多聊聊，TA 会慢慢记住你。</p>
        )}
      </div>
    </section>
  )
}

interface PulseEntry {
  key: string
  title: string
  meta: string
}

/* 通知纪律（P5）：合并/重连/模型内部撤回是机制细节，不进用户视野；同一轮整理
 * 改动多时聚合成一条摘要，不逐条刷屏。文案说人话，不出现任何内部术语。 */
const HIDDEN_OPS = new Set(['supersede', 'relink'])

function isUserVisible(operation: MemoryOperation): boolean {
  if (HIDDEN_OPS.has(operation.op)) return false
  if (operation.op === 'retract' && operation.actor !== 'user') return false
  return true
}

function pulseEntries(operations: MemoryOperation[], limit = 6): PulseEntry[] {
  const visible = operations.filter(isUserVisible)
  // newest first from the API; group runs with many changes into one summary
  const byRun = new Map<number, MemoryOperation[]>()
  for (const op of visible) {
    if (op.reconcile_run_id !== null) {
      const group = byRun.get(op.reconcile_run_id) ?? []
      group.push(op)
      byRun.set(op.reconcile_run_id, group)
    }
  }
  const summarizedRuns = new Set<number>()
  const entries: PulseEntry[] = []
  for (const op of visible) {
    if (entries.length >= limit) break
    const runId = op.reconcile_run_id
    if (runId !== null && (byRun.get(runId)?.length ?? 0) >= 3) {
      if (summarizedRuns.has(runId)) continue
      summarizedRuns.add(runId)
      const group = byRun.get(runId) ?? []
      entries.push({
        key: `run-${runId}`,
        title: runSummaryTitle(group),
        meta: `一次整理 · ${formatSmartTime(group[0]?.created_at ?? op.created_at)}`,
      })
      continue
    }
    entries.push({
      key: `op-${op.id}`,
      title: operationTitle(op),
      meta: `${operationLabel(op.op, op.actor)} · ${formatSmartTime(op.created_at)}`,
    })
  }
  return entries
}

function runSummaryTitle(group: MemoryOperation[]): string {
  const counts: Record<string, number> = {}
  for (const op of group) counts[op.op] = (counts[op.op] ?? 0) + 1
  const parts: string[] = []
  if (counts.add) parts.push(`新记住 ${counts.add} 件事`)
  if (counts.confirm) parts.push(`确认了 ${counts.confirm} 条`)
  if (counts.revise) parts.push(`更新了 ${counts.revise} 条`)
  if (counts.retain) parts.push(`核对保留 ${counts.retain} 条`)
  const rest = group.length - (counts.add ?? 0) - (counts.confirm ?? 0) - (counts.revise ?? 0) - (counts.retain ?? 0)
  if (rest > 0) parts.push(`其他调整 ${rest} 条`)
  return parts.join('，') || `整理了 ${group.length} 条记忆`
}

function operationTitle(operation: MemoryOperation): string {
  const content = operation.after?.content ?? operation.before?.content
  if (typeof content === 'string' && content.trim()) return content
  return '一条记忆'
}

function operationLabel(op: string, actor: string): string {
  const labels: Record<string, string> = {
    add: 'TA 记住了',
    confirm: '又确认了一次',
    revise: '更新了',
    retract: '应你的要求忘记了',
    retain: '核对后保留',
    challenge: '正在重新核对',
    decay: '很久没提，慢慢淡忘了',
    promote: '记得更牢了',
    restore: '找回了',
    user_create: '你添加的',
    user_edit: '你修改了',
    user_delete: '你删除了',
  }
  return labels[op] ?? (actor === 'user' ? '你调整了' : 'TA 整理了')
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.35-4.35" />
    </svg>
  )
}

function PanelHeader({ title, onMore }: { title: string; onMore?: () => void }) {
  return (
    <div className={styles.cardHeader}>
      <h3 className={styles.cardTitle}>{title}</h3>
      {onMore && (
        <button className={styles.cardMore} type="button" onClick={onMore}>
          查看更多
          <ChevronRightIcon width={13} height={13} />
        </button>
      )}
    </div>
  )
}

