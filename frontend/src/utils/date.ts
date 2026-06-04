/** 格式化相对时间（刚刚、N分钟前、N小时前等） */
export function formatRelativeTime(ts: string): string {
  const date = new Date(ts)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffMin = Math.floor(diffMs / 60000)
  const diffHour = Math.floor(diffMs / 3600000)
  const diffDay = Math.floor(diffMs / 86400000)

  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin} 分钟前`
  if (diffHour < 24) return `${diffHour} 小时前`
  if (diffDay < 7) return `${diffDay} 天前`

  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
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
