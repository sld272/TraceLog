import { type Todo } from '@/api/client'

/** 判断待办是否已完成 */
export function isTodoDone(todo: Todo): boolean {
  return ['已完成', '完成', 'done', 'completed'].includes(todo.status)
}

/** 获取今天的日期键（格式：YYYY-MM-DD） */
export function getTodayKey(): string {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/** 清理可选字段的空字符串为 null */
export function cleanOptionalField(value: string): string | null {
  const text = value.trim()
  return text || null
}
