import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import styles from './AppShell.module.css'

interface AppShellProps {
  nav: ReactNode | ((closeMobileNav: () => void) => ReactNode)
  main: ReactNode
  panel?: ReactNode
  /** 'reading' 是 timeline / 详情 / 私聊的窄阅读栏；'workspace' 给目标、日程、
   *  记忆、设置这些需要横向铺开的页面，否则内容会缩在一条窄柱里、左侧留出大片空白。 */
  width?: 'reading' | 'workspace'
}

export function AppShell({ nav, main, panel, width = 'reading' }: AppShellProps) {
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [mobileLayout, setMobileLayout] = useState(() => window.innerWidth < 768)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const closeMobileNav = useCallback(() => {
    setMobileNavOpen(false)
    if (mobileLayout) menuButtonRef.current?.focus()
  }, [mobileLayout])

  /* Close on escape */
  useEffect(() => {
    if (!mobileNavOpen) return

    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeMobileNav()
    }
    window.addEventListener('keydown', handleKey)
    return () => {
      window.removeEventListener('keydown', handleKey)
    }
  }, [closeMobileNav, mobileNavOpen])

  /* Resizing an open 375px drawer past the 768px breakpoint switches back to desktop navigation. */
  useEffect(() => {
    const handleResize = () => {
      const nextMobileLayout = window.innerWidth < 768
      setMobileLayout(nextMobileLayout)
      if (!nextMobileLayout) setMobileNavOpen(false)
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  /* Lock body scroll while drawer is open on mobile */
  useEffect(() => {
    if (!mobileNavOpen) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [mobileNavOpen])

  const navContent = typeof nav === 'function' ? nav(closeMobileNav) : nav

  return (
    <div className={styles.shell}>
      <button
        ref={menuButtonRef}
        type="button"
        className={styles.menuButton}
        onClick={() => setMobileNavOpen((open) => !open)}
        aria-expanded={mobileNavOpen}
        aria-controls="app-shell-nav"
        aria-label={mobileNavOpen ? '关闭导航' : '打开导航'}
      >
        <MenuIcon />
      </button>
      <div className={styles.body}>
        <nav
          id="app-shell-nav"
          className={`${styles.nav} ${mobileNavOpen ? styles.navOpen : ''}`}
          aria-label="主导航"
          aria-hidden={!mobileNavOpen ? undefined : false}
          inert={mobileLayout && !mobileNavOpen}
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
        <main
          className={`${styles.main} ${width === 'workspace' ? styles.mainWide : ''}`}
          inert={mobileLayout && mobileNavOpen}
        >
          {main}
        </main>
        {panel && (
          <aside className={styles.panel} aria-label="信息面板" inert={mobileLayout && mobileNavOpen}>
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
