const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

/** 格式化动态时间（刚刚、今天 HH:mm、M月D日 HH:mm 等） */
export function formatSmartTime(value: string | number, now = new Date()): string {
  const date = parseDateValue(value)
  if (!date) return '-'

  const diffMs = now.getTime() - date.getTime()
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin} 分钟前`

  const dayDiff = getCalendarDayDiff(date, now)
  if (dayDiff === 0) return `今天 ${formatClock(date)}`
  if (dayDiff === 1) return `昨天 ${formatClock(date)}`
  if (dayDiff > 1 && dayDiff < 7) return `${WEEKDAYS[date.getDay()]} ${formatClock(date)}`

  if (date.getFullYear() === now.getFullYear()) {
    return `${date.getMonth() + 1}月${date.getDate()}日 ${formatClock(date)}`
  }

  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
}

/** 格式化绝对时间，用于悬停提示 */
export function formatAbsoluteTime(value: string | number): string {
  const date = parseDateValue(value)
  if (!date) return '-'
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${formatClock(date)}:${pad2(date.getSeconds())}`
}

/** 格式化 time.dateTime 属性 */
export function formatDateTimeAttribute(value: string | number): string {
  const date = parseDateValue(value)
  return date?.toISOString() ?? ''
}

function parseDateValue(value: string | number): Date | null {
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function formatClock(date: Date): string {
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
}

function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

function getCalendarDayDiff(date: Date, now: Date): number {
  const dateUtc = Date.UTC(date.getFullYear(), date.getMonth(), date.getDate())
  const nowUtc = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())
  return Math.floor((nowUtc - dateUtc) / 86400000)
}

/** 格式化日期（月日格式） */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '-'
  return formatDateFromMs(date.getTime())
}

/** 从毫秒时间戳格式化日期 */
export function formatDateFromMs(value: number): string {
  return new Date(value).toLocaleDateString('zh-CN', {
    month: 'short',
    day: 'numeric',
  })
}

/** 格式化日期范围 */
export function formatDateScope(start: string | null | undefined, end: string | null | undefined): string {
  const startText = formatDate(start)
  const endText = formatDate(end)
  if (startText === '-' && endText === '-') return '等待整理'
  return `${startText} - ${endText}`
}

/** 格式化 Unix 时间戳范围 */
export function formatUnixScope(start: number, end: number): string {
  return `${formatDateFromMs(start * 1000)} - ${formatDateFromMs(end * 1000)}`
}

/** 格式化日期标签（今天 / 月日） */
export function formatDateLabel(date: string, todayKey: string): string {
  if (date === todayKey) return '今天'
  const parsed = new Date(`${date}T00:00:00`)
  if (Number.isNaN(parsed.getTime())) return date
  return parsed.toLocaleDateString('zh-CN', {
    month: 'short',
    day: 'numeric',
  })
}
