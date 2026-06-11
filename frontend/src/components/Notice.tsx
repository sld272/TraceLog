import { type ReactNode } from 'react'
import styles from './Notice.module.css'

export type NoticeKind = 'info' | 'success' | 'error'

interface NoticeProps {
  kind?: NoticeKind
  children: ReactNode
  /** 右侧操作按钮（按钮样式由调用方决定，默认随类型着色） */
  actions?: ReactNode
  /** 提供时显示关闭按钮 */
  onClose?: () => void
}

/** 页面级提示条：info 茶绿 / success 绿 / error 红，role 随类型自动设置。 */
export function Notice({ kind = 'info', children, actions, onClose }: NoticeProps) {
  return (
    <div className={`${styles.notice} ${styles[kind]}`} role={kind === 'error' ? 'alert' : 'status'}>
      <div className={styles.body}>{children}</div>
      {actions && <div className={styles.actions}>{actions}</div>}
      {onClose && (
        <button type="button" className={styles.close} onClick={onClose} aria-label="关闭提示">
          ×
        </button>
      )}
    </div>
  )
}
