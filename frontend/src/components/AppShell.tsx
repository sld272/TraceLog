import { useEffect, useState, type ReactNode } from 'react'
import styles from './AppShell.module.css'

interface AppShellProps {
  nav: ReactNode | ((closeMobileNav: () => void) => ReactNode)
  main: ReactNode
  panel?: ReactNode
}

export function AppShell({ nav, main, panel }: AppShellProps) {
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  /* Close on escape and when window grows past mobile breakpoint */
  useEffect(() => {
    if (!mobileNavOpen) return

    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileNavOpen(false)
    }
    const handleResize = () => {
      if (window.innerWidth >= 768) setMobileNavOpen(false)
    }
    window.addEventListener('keydown', handleKey)
    window.addEventListener('resize', handleResize)
    return () => {
      window.removeEventListener('keydown', handleKey)
      window.removeEventListener('resize', handleResize)
    }
  }, [mobileNavOpen])

  /* Lock body scroll while drawer is open on mobile */
  useEffect(() => {
    if (!mobileNavOpen) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [mobileNavOpen])

  const closeMobileNav = () => setMobileNavOpen(false)
  const navContent = typeof nav === 'function' ? nav(closeMobileNav) : nav

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.topbarLeft}>
          <button
            type="button"
            className={styles.menuButton}
            onClick={() => setMobileNavOpen((open) => !open)}
            aria-expanded={mobileNavOpen}
            aria-controls="app-shell-nav"
            aria-label={mobileNavOpen ? '关闭导航' : '打开导航'}
          >
            <MenuIcon />
          </button>
          <div className={styles.logo}>
            <img className={styles.logoMark} src="/brand/tracelog-icon-transparent-256.png" alt="" aria-hidden="true" />
            <img className={styles.logoWordmark} src="/brand/shiji-wordmark-transparent.png" alt="拾迹" />
          </div>
        </div>
        <div className={styles.topbarHint}>TraceLog</div>
      </header>
      <div className={styles.body}>
        <nav
          id="app-shell-nav"
          className={`${styles.nav} ${mobileNavOpen ? styles.navOpen : ''}`}
          aria-label="主导航"
          aria-hidden={!mobileNavOpen ? undefined : false}
        >
          {navContent}
        </nav>
        {mobileNavOpen && (
          <div
            className={styles.navBackdrop}
            onClick={closeMobileNav}
            aria-hidden="true"
          />
        )}
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

function MenuIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="4" y1="6" x2="20" y2="6" />
      <line x1="4" y1="12" x2="20" y2="12" />
      <line x1="4" y1="18" x2="20" y2="18" />
    </svg>
  )
}
