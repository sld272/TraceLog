import { type ReactNode } from 'react'
import styles from './AppShell.module.css'

interface AppShellProps {
  nav: ReactNode
  main: ReactNode
  panel?: ReactNode
}

export function AppShell({ nav, main, panel }: AppShellProps) {
  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.logo}>
          <span className={styles.logoMark} aria-hidden="true">T</span>
          <span className={styles.logoText}>拾迹</span>
        </div>
        <div className={styles.topbarHint}>TraceLog</div>
      </header>
      <div className={styles.body}>
        <nav className={styles.nav} aria-label="主导航">
          {nav}
        </nav>
        <main className={styles.main}>
          {main}
        </main>
        {panel && (
          <aside className={styles.panel} aria-label="信息面板">
            {panel}
          </aside>
        )}
      </div>
    </div>
  )
}
