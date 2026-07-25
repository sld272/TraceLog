import type { GoalActivityKind } from '@/api/client'

export const ACTIVITY_KIND_LABELS: Record<GoalActivityKind, string> = {
  commitment: '承诺',
  progress: '进展',
  blocked: '卡住',
  milestone: '里程碑',
  scheduled: '已排期',
}

/** 全部五个 kind，按台账展示顺序。 */
export const ACTIVITY_KINDS: GoalActivityKind[] = [
  'commitment',
  'progress',
  'blocked',
  'milestone',
  'scheduled',
]

/**
 * 手动补标可选的 kind。
 *
 * 不含 `scheduled`：它只由「已排期日程到期」这一确定性事实产生，手选会写出
 * 语义错误的行（这条帖子并不是一场到期的日程）。
 */
export const MANUAL_ACTIVITY_KINDS: GoalActivityKind[] = [
  'commitment',
  'progress',
  'blocked',
  'milestone',
]
