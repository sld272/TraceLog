import { type ReactNode } from 'react'
import styles from './AppShell.module.css'

interface AppShellProps {
  nav: ReactNode
  main: ReactNode
  panel: ReactNode
}

export function AppShell({ nav, main, panel }: AppShellProps) {
  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.logo}>
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z"
              fill="currentColor"
              opacity="0.7"
            />
          </svg>
          <span className={styles.logoText}>拾迹</span>
        </div>
      </header>
      <div className={styles.body}>
        <nav className={styles.nav} aria-label="主导航">
          {nav}
        </nav>
        <main className={styles.main}>
          {main}
        </main>
        <aside className={styles.panel} aria-label="信息面板">
          {panel}
        </aside>
      </div>
    </div>
  )
}
