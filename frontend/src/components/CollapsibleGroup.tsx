import { useState, type ReactNode } from 'react'
import { ChevronRightIcon } from '@/components/icons'
import styles from '@/pages/WorkspacePages.module.css'

interface CollapsibleGroupProps {
  title: string
  count: number
  defaultCollapsed?: boolean
  accent?: boolean
  children: ReactNode
}

export function CollapsibleGroup({
  title,
  count,
  defaultCollapsed = false,
  accent = false,
  children,
}: CollapsibleGroupProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed)
  return (
    <div className={`${styles.group} ${accent ? styles.todayGroup : ''}`}>
      <div
        className={`${styles.groupHeader} ${collapsed ? styles.collapsed : ''}`}
        onClick={() => setCollapsed((value) => !value)}
      >
        <span className={styles.groupChevron}>
          <ChevronRightIcon width={16} height={16} />
        </span>
        <h2>{title}</h2>
        <span className={styles.groupCount}>{count}</span>
      </div>
      {!collapsed && children}
    </div>
  )
}
