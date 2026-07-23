import { type ScheduleEvent, type ScheduleProgress, getGoalSchedule } from '@/api/client'

/** 周一起始的星期表头。 */
export const WEEKDAY_HEADERS = ['一', '二', '三', '四', '五', '六', '日'] as const

/** getDay() 索引（0=周日）对应的中文单字。 */
const WEEKDAY_CN = ['日', '一', '二', '三', '四', '五', '六'] as const

/** 浏览器当前跟随的系统时区。 */
export const SYSTEM_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone

function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

/** 把帖子时间戳（ISO 字符串或秒级数字）分桶为本地时区的 'YYYY-MM-DD'。 */
export function localDateKey(value: string | number | Date): string {
  const date =
    value instanceof Date
      ? value
      : typeof value === 'number'
        ? new Date(value * 1000)
        : new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

/** 事件按当前系统时区的墙钟日期分桶（start_local 已是本地时间字符串）。 */
export function eventDateKey(event: Pick<ScheduleEvent, 'start_local'>): string {
  return String(event.start_local).slice(0, 10)
}

/** 今天的 'YYYY-MM-DD'（本地时区）。 */
export function todayKey(now: Date = new Date()): string {
  return localDateKey(now)
}

/** 从 'YYYY-MM-DD' 构造本地 Date（零点）。 */
export function dateFromKey(key: string): Date {
  return new Date(`${key}T00:00:00`)
}

/** 事件展示时间：全天 → '全天'，否则 start_local 的 'HH:MM'。 */
export function formatEventTime(event: Pick<ScheduleEvent, 'all_day' | 'start_local'>): string {
  if (event.all_day) return '全天'
  return String(event.start_local).slice(11, 16)
}

/** 'YYYY-MM-DD' → 'M月D日'。 */
export function monthDayLabel(key: string): string {
  const date = dateFromKey(key)
  if (Number.isNaN(date.getTime())) return key
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

/** 'YYYY-MM-DD' → '星期X'。 */
export function weekdayLabel(key: string): string {
  const date = dateFromKey(key)
  if (Number.isNaN(date.getTime())) return ''
  return `星期${WEEKDAY_CN[date.getDay()]}`
}

/** 相对今天的日期标签：今天 / 明天 / 昨天 / 周X（本周内）/ M月D日。 */
export function relativeDayLabel(key: string, today: string = todayKey()): string {
  if (key === today) return '今天'
  const target = dateFromKey(key)
  const base = dateFromKey(today)
  if (Number.isNaN(target.getTime()) || Number.isNaN(base.getTime())) return monthDayLabel(key)
  const diff = Math.round((target.getTime() - base.getTime()) / 86400000)
  if (diff === 1) return '明天'
  if (diff === -1) return '昨天'
  if (diff > 1 && diff < 7) return `周${WEEKDAY_CN[target.getDay()]}`
  return monthDayLabel(key)
}

export interface CalendarCell {
  key: string
  day: number
  outside: boolean
}

/** 生成某年某月的月历方格（周一起始，含相邻月补格），始终是 7 的整数倍。 */
export function monthGrid(year: number, month: number): CalendarCell[] {
  const cells: CalendarCell[] = []
  const first = new Date(year, month - 1, 1)
  const leading = (first.getDay() + 6) % 7 // 周一起始的前导空格数
  const daysInMonth = new Date(year, month, 0).getDate()

  const prevMonthDays = new Date(year, month - 1, 0).getDate()
  for (let i = leading; i > 0; i -= 1) {
    const day = prevMonthDays - i + 1
    cells.push({ key: '', day, outside: true })
  }
  for (let day = 1; day <= daysInMonth; day += 1) {
    cells.push({ key: `${year}-${pad2(month)}-${pad2(day)}`, day, outside: false })
  }
  let trailingDay = 1
  while (cells.length % 7 !== 0) {
    cells.push({ key: '', day: trailingDay, outside: true })
    trailingDay += 1
  }
  return cells
}

/** 当前系统时区里，本周（周一至周日）的 7 个日期 key。 */
export function weekKeys(now: Date = new Date()): string[] {
  const monday = new Date(now)
  const offset = (now.getDay() + 6) % 7
  monday.setDate(now.getDate() - offset)
  monday.setHours(0, 0, 0, 0)
  return Array.from({ length: 7 }, (_, i) => {
    const day = new Date(monday)
    day.setDate(monday.getDate() + i)
    return localDateKey(day)
  })
}

/** 目标绑定进度的展示文案，例如「每周健身 3 次 · 本周 2/3」。 */
export function goalChipText(goalTitle: string, progressText: string | null): string {
  return progressText ? `${goalTitle} · 本周 ${progressText}` : goalTitle
}

/** goal_id → 本周进度。用于把 goalChip 补上「本周 N/M」。拉取失败的目标略过。 */
export async function fetchGoalProgress(goalIds: string[]): Promise<Record<string, ScheduleProgress>> {
  const distinct = [...new Set(goalIds.filter(Boolean))]
  if (distinct.length === 0) return {}
  const entries = await Promise.all(
    distinct.map(async (id): Promise<readonly [string, ScheduleProgress | null]> => {
      try {
        return [id, (await getGoalSchedule(id)).progress] as const
      } catch {
        return [id, null] as const
      }
    }),
  )
  const map: Record<string, ScheduleProgress> = {}
  for (const [id, progress] of entries) {
    if (progress) map[id] = progress
  }
  return map
}

/** 单条事件 goalChip 的完整文案：期望 label（或目标标题）+ 「· 本周 N/M」。 */
export function goalChipLabel(
  goalTitle: string,
  progress: ScheduleProgress | undefined,
): string {
  const label = progress?.expectation?.label ?? goalTitle
  return progress?.text ? `${label} · 本周 ${progress.text}` : label
}

/* ===== 日历视图（P10）辅助 ===== */

/** 含 anchorKey 的那一周（周一至周日）的 7 个日期 key。 */
export function weekKeysFor(anchorKey: string): string[] {
  const base = dateFromKey(anchorKey)
  if (Number.isNaN(base.getTime())) return weekKeys()
  const monday = new Date(base)
  const offset = (base.getDay() + 6) % 7
  monday.setDate(base.getDate() - offset)
  monday.setHours(0, 0, 0, 0)
  return Array.from({ length: 7 }, (_, i) => {
    const day = new Date(monday)
    day.setDate(monday.getDate() + i)
    return localDateKey(day)
  })
}

/** 把 'YYYY-MM-DD' 平移 days 天（负数向前）。 */
export function shiftDateKey(key: string, days: number): string {
  const date = dateFromKey(key)
  if (Number.isNaN(date.getTime())) return key
  date.setDate(date.getDate() + days)
  return localDateKey(date)
}

/** 把 anchor 平移 months 个月，落到目标月的第一天。 */
export function shiftMonthKey(key: string, months: number): string {
  const date = dateFromKey(key)
  if (Number.isNaN(date.getTime())) return key
  return localDateKey(new Date(date.getFullYear(), date.getMonth() + months, 1))
}

/**
 * 月历方格（周一起始，补格带真实 key + outside 标记）。
 * 与 monthGrid 的区别：补格也给出真实日期 key，方便渲染事件与点击跳转。
 */
export function monthCalendarCells(year: number, month: number): CalendarCell[] {
  const first = new Date(year, month - 1, 1)
  const leading = (first.getDay() + 6) % 7
  const daysInMonth = new Date(year, month, 0).getDate()
  const total = Math.ceil((leading + daysInMonth) / 7) * 7
  const start = new Date(year, month - 1, 1 - leading)
  const cells: CalendarCell[] = []
  for (let i = 0; i < total; i += 1) {
    const day = new Date(start)
    day.setDate(start.getDate() + i)
    cells.push({ key: localDateKey(day), day: day.getDate(), outside: day.getMonth() !== month - 1 })
  }
  return cells
}

/** 'YYYY-MM-DDTHH:MM:SS' → 当天分钟数（HH*60+MM）。 */
export function localMinutes(local: string): number {
  const hours = Number(String(local).slice(11, 13))
  const minutes = Number(String(local).slice(14, 16))
  if (Number.isNaN(hours) || Number.isNaN(minutes)) return 0
  return hours * 60 + minutes
}

/** 分钟数 → 'HH:MM'。 */
export function minutesToTime(min: number): string {
  const clamped = Math.max(0, Math.min(min, 24 * 60))
  return `${pad2(Math.floor(clamped / 60) % 24)}:${pad2(clamped % 60)}`
}

/** 事件时长（分钟），基于 start_ts/end_ts。 */
export function eventDurationMinutes(event: Pick<ScheduleEvent, 'start_ts' | 'end_ts'>): number {
  return Math.max(0, Math.round((event.end_ts - event.start_ts) / 60))
}

/** 'HH:MM'（本地墙钟）取自 start_local / end_local。 */
export function eventClock(local: string): string {
  return String(local).slice(11, 16)
}

/** 周视图副标题：`2026年7月13日 – 19日`（跨月带月份）。 */
export function weekRangeLabel(days: string[]): string {
  const first = dateFromKey(days[0] ?? '')
  const last = dateFromKey(days[6] ?? '')
  if (Number.isNaN(first.getTime()) || Number.isNaN(last.getTime())) return ''
  const head = `${first.getFullYear()}年${first.getMonth() + 1}月${first.getDate()}日`
  if (first.getMonth() === last.getMonth()) return `${head} – ${last.getDate()}日`
  return `${head} – ${last.getMonth() + 1}月${last.getDate()}日`
}

/** 月视图副标题：`2026年7月`。 */
export function monthTitleLabel(key: string): string {
  const date = dateFromKey(key)
  if (Number.isNaN(date.getTime())) return ''
  return `${date.getFullYear()}年${date.getMonth() + 1}月`
}

/** 短星期标签：`周X`（用于事件 popover）。 */
export function weekdayShortLabel(key: string): string {
  const date = dateFromKey(key)
  if (Number.isNaN(date.getTime())) return ''
  return `周${WEEKDAY_CN[date.getDay()]}`
}

/** 一个绝对定位的事件块：时间坐标 + 重叠分列位置。 */
export interface ScheduleBlock {
  event: ScheduleEvent
  /** 当天起始分钟。 */
  startMin: number
  /** 当天结束分钟（startMin + 时长，跨日截断到 1440）。 */
  endMin: number
  /** 所在列索引（0 起）。 */
  col: number
  /** 所在聚簇的总列数。 */
  cols: number
}

/**
 * 一天内定时事件的重叠布局：按区间聚簇，簇内贪心分列
 * （排序后放入首个不冲突列），每列等分宽度。all_day 事件应先剔除。
 */
export function layoutDayBlocks(events: ScheduleEvent[]): ScheduleBlock[] {
  const blocks: ScheduleBlock[] = events.map((event) => {
    const startMin = localMinutes(event.start_local)
    const endMin = Math.min(startMin + eventDurationMinutes(event), 24 * 60)
    return { event, startMin, endMin: Math.max(endMin, startMin + 1), col: 0, cols: 1 }
  })
  blocks.sort((a, b) => a.startMin - b.startMin || a.endMin - b.endMin)

  let i = 0
  while (i < blocks.length) {
    let clusterEnd = blocks[i]!.endMin
    let j = i + 1
    while (j < blocks.length && blocks[j]!.startMin < clusterEnd) {
      clusterEnd = Math.max(clusterEnd, blocks[j]!.endMin)
      j += 1
    }
    const cluster = blocks.slice(i, j)
    const colEnds: number[] = []
    for (const block of cluster) {
      let placed = false
      for (let c = 0; c < colEnds.length; c += 1) {
        if (colEnds[c]! <= block.startMin) {
          block.col = c
          colEnds[c] = block.endMin
          placed = true
          break
        }
      }
      if (!placed) {
        block.col = colEnds.length
        colEnds.push(block.endMin)
      }
    }
    for (const block of cluster) block.cols = colEnds.length
    i = j
  }
  return blocks
}
