/* 桌面通知：渲染进程直接用 Web Notification API。
 *
 * Electron 渲染进程原生支持它并映射到系统通知；生产模式下前端由 FastAPI 在
 * localhost 单端口伺服，localhost 属 secure context，浏览器里同样可用。所以一套
 * 实现覆盖两种运行方式，Electron 壳（desktop/shell/main.cjs）零改动——那里既没有
 * preload 也没有 IPC，走主进程发通知等于先建一套通道。
 *
 * 已知限制：关窗只剩托盘时渲染进程不存在，通知发不出来。信的频率本就低（全局冷却
 * 3 天），漏掉的那封下次开窗时靠未读点看到。
 */

/** 已通知过的主动私聊 message id。存 localStorage，避免刷新页面后重复弹。 */
const NOTIFIED_KEY = 'tracelog.notifiedProactiveMessageIds'
/** 只保留最近这么多条 id：判重只需要覆盖"还没读的那几封"。 */
const NOTIFIED_HISTORY_LIMIT = 50

export type NotificationPermissionState = 'unsupported' | NotificationPermission

export function notificationPermission(): NotificationPermissionState {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported'
  return Notification.permission
}

/**
 * 申请通知权限。只在用户主动打开桌面通知开关时调用——浏览器把未经用户手势的申请
 * 当骚扰，而被拒绝一次之后再也弹不出来了。
 */
export async function requestNotificationPermission(): Promise<NotificationPermissionState> {
  const current = notificationPermission()
  if (current !== 'default') return current
  try {
    return await Notification.requestPermission()
  } catch {
    return notificationPermission()
  }
}

export function showDesktopNotification(
  title: string,
  body: string,
  options: { tag?: string; onClick?: () => void } = {},
): boolean {
  if (notificationPermission() !== 'granted') return false
  const { tag, onClick } = options
  try {
    /* tag 按消息取，不能用固定值：多封信同时到达时固定 tag 会互相顶掉 */
    const notification = new Notification(title, { body, tag })
    if (onClick) {
      notification.onclick = () => {
        window.focus()
        notification.close()
        onClick()
      }
    }
    return true
  } catch {
    /* 某些环境（无通知服务的 Linux 桌面等）构造即抛，不该带崩轮询 */
    return false
  }
}

export function loadNotifiedMessageIds(): Set<number> {
  try {
    const raw = window.localStorage.getItem(NOTIFIED_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((id): id is number => typeof id === 'number'))
  } catch {
    return new Set()
  }
}

export function saveNotifiedMessageIds(ids: Set<number>): void {
  try {
    const trimmed = [...ids].sort((a, b) => a - b).slice(-NOTIFIED_HISTORY_LIMIT)
    window.localStorage.setItem(NOTIFIED_KEY, JSON.stringify(trimmed))
  } catch {
    /* 隐私模式下 localStorage 会抛；顶多重复通知一次，不值得中断 */
  }
}
