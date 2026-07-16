import { type ScheduleEvent, type ScheduleProgress, getGoalSchedule } from '@/api/client'

/** 周一起始的星期表头。 */
export const WEEKDAY_HEADERS = ['一', '二', '三', '四', '五', '六', '日'] as const

/** getDay() 索引（0=周日）对应的中文单字。 */
const WEEKDAY_CN = ['日', '一', '二', '三', '四', '五', '六'] as const

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

/** 事件按其 Asia/Shanghai 墙钟日期分桶（start_local 已是本地时间字符串）。 */
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

/** 本周（周一至周日）的 7 个日期 key，含 Asia/Shanghai 语义（用本地时区近似）。 */
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
