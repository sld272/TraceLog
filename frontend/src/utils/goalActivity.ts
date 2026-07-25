import type { GoalActivityKind } from '@/api/client'

export const ACTIVITY_KIND_LABELS: Record<GoalActivityKind, string> = {
  commitment: '承诺',
  progress: '进展',
  blocked: '卡住',
  milestone: '里程碑',
  scheduled: '已排期',
}
