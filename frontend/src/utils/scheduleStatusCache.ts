import { type ScheduleStatus } from '@/api/client'

/**
 * 右栏与日程页共享的「日历连接状态」缓存 + 三态派生。
 *
 * 背景：/schedule/status 首次通常要 3-5 秒。请求在途（尚无任何已知状态）时
 * 不能当成「未连接」渲染，否则登录成功后切页/刷新会闪现「去设置连接」引导。
 * 这里把上次已知的 status 缓存在模块级变量（并落到 sessionStorage 以扛住整页
 * 刷新），组件挂载时先用缓存值渲染，后台刷新再校正。退出登录 / 恢复默认
 * client_id 等会清空后端登录态的动作，必须主动失效这份缓存，避免残留一个过期
 * 的「已连接」快照。
 */

/**
 * UI 面向的连接状态：
 * - 'loading'      请求在途且没有任何已知状态 → 显示中性占位，不显示引导
 * - 'connected'    有任一可用日历账号（Outlook 已连接或本地日历已创建）→ 显示日程
 * - 'disconnected' 明确既未连接云端也没有本地账号 → 显示引导
 * - 'error'        请求失败（网络/500）且无已知状态 → 显示「获取失败」而非引导
 */
export type ScheduleConnectionState = 'loading' | 'connected' | 'disconnected' | 'error'

/** status 是否包含本地日历账号（旧缓存快照可能没有 accounts 字段）。 */
export function hasLocalCalendarAccount(status: ScheduleStatus | null): boolean {
  return (status?.accounts ?? []).some((account) => account.provider === 'local')
}

const STORAGE_KEY = 'tracelog.scheduleStatus'

let cached: ScheduleStatus | null = null
let hydrated = false

/** 首次访问时从 sessionStorage 恢复缓存（惰性，仅执行一次）。 */
function hydrate(): void {
  if (hydrated) return
  hydrated = true
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    if (raw) cached = JSON.parse(raw) as ScheduleStatus
  } catch {
    cached = null
  }
}

/** 上次已知的日历连接状态；本会话从未成功获取过时为 null。 */
export function getCachedScheduleStatus(): ScheduleStatus | null {
  hydrate()
  return cached
}

/** 记住最新的 status，供其它页面挂载时立即渲染，避免跨页切换闪烁。 */
export function setCachedScheduleStatus(status: ScheduleStatus): void {
  hydrate()
  cached = status
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(status))
  } catch {
    /* sessionStorage 不可用时退回纯内存缓存 */
  }
}

/**
 * 失效缓存。退出登录 / 恢复默认或自定义 client_id 后调用：这些动作会清空后端
 * 登录态，若不失效，别的页面可能仍读到一个过期的「已连接」快照。
 */
export function invalidateScheduleStatusCache(): void {
  hydrate()
  cached = null
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    /* 忽略：sessionStorage 不可用时内存缓存已清空 */
  }
}

/**
 * 由「已知 status（缓存或新拉取）」+「本次拉取是否失败」派生 UI 三态。
 * 只要有已知 status（哪怕来自缓存）就按其 connected 渲染——一次瞬时失败不会把
 * 已知的「已连接」误降级为引导或错误。仅当完全没有已知状态时，才在 loading 与
 * error 之间取舍。
 */
export function scheduleConnectionState(
  status: ScheduleStatus | null,
  failed: boolean,
): ScheduleConnectionState {
  if (status) {
    return status.connected || hasLocalCalendarAccount(status) ? 'connected' : 'disconnected'
  }
  return failed ? 'error' : 'loading'
}
