import { type Soul } from '@/api/client'
import { SoulAvatar } from './SoulAvatar'
import styles from './LeftNav.module.css'

interface LeftNavProps {
  souls: Soul[]
  soulsLoadState?: 'loading' | 'ready' | 'error'
  /** 待对账 evidence 数，>0 时在「记忆」项上显示 badge。 */
  memoryQueueCount?: number
  /** 进行中的目标数，>0 时在「目标」项上显示 badge */
  goalCount?: number
  /** 未完成的待办数，>0 时在「待办」项上显示 badge */
  todoCount?: number
  activePage: string
  onNavigate: (page: string) => void
  onAfterNavigate?: () => void
}

export function LeftNav({
  souls,
  soulsLoadState = 'ready',
  memoryQueueCount = 0,
  goalCount = 0,
  todoCount = 0,
  activePage,
  onNavigate,
  onAfterNavigate,
}: LeftNavProps) {
  const navigate = (page: string) => {
    onNavigate(page)
    onAfterNavigate?.()
  }

  return (
    <div className={styles.nav}>
      <div className={styles.navLogo}>
        <img className={styles.logoMark} src="/brand/tracelog-icon-transparent-256.png" alt="" aria-hidden="true" />
        <img className={styles.logoWordmark} src="/brand/shiji-wordmark-transparent.png" alt="拾迹" />
      </div>
      <section className={styles.section}>
        <NavItem
          icon={<HomeIcon />}
          label="首页"
          active={activePage === 'home'}
          onClick={() => navigate('home')}
        />
        <NavItem
          icon={<GoalIcon />}
          label="目标"
          badge={goalCount > 0 ? goalCount : undefined}
          active={activePage === 'goals'}
          onClick={() => navigate('goals')}
        />
        <NavItem
          icon={<TodoIcon />}
          label="待办"
          badge={todoCount > 0 ? todoCount : undefined}
          active={activePage === 'todos'}
          onClick={() => navigate('todos')}
        />
        <NavItem
          icon={<MemoryIcon />}
          label="记忆"
          badge={memoryQueueCount > 0 ? memoryQueueCount : undefined}
          active={activePage === 'memory'}
          onClick={() => navigate('memory')}
        />
      </section>

      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>私聊</h3>
        {souls.map((soul) => {
          const active = activePage === `chat:${soul.name}`
          return (
            <button
              key={soul.name}
              className={`${styles.dmItem} ${active ? styles.dmItemActive : ''}`}
              onClick={() => navigate(`chat:${soul.name}`)}
              aria-current={active ? 'page' : undefined}
            >
              <SoulAvatar name={soul.name} className={styles.dmAvatar} />
              <span className={styles.dmBody}>
                <span className={styles.dmName}>{soul.name}</span>
                {soul.description && <span className={styles.dmPreview}>{soul.description}</span>}
              </span>
            </button>
          )
        })}
        {souls.length === 0 && soulsLoadState === 'loading' && (
          <p className={styles.emptySoul}>加载中...</p>
        )}
        {souls.length === 0 && soulsLoadState === 'error' && (
          <p className={styles.emptySoul} role="alert">加载失败</p>
        )}
        {souls.length === 0 && soulsLoadState === 'ready' && (
          <p className={styles.emptySoul}>还没有人格，去设置里创建</p>
        )}
      </section>

      <section className={`${styles.section} ${styles.bottomSection}`}>
        <NavItem
          icon={<SettingsIcon />}
          label="设置"
          active={activePage === 'settings'}
          onClick={() => navigate('settings')}
        />
      </section>
    </div>
  )
}

interface NavItemProps {
  icon: React.ReactNode
  label: string
  badge?: number
  active: boolean
  onClick: () => void
}

function NavItem({ icon, label, badge, active, onClick }: NavItemProps) {
  return (
    <button
      className={`${styles.item} ${active ? styles.active : ''}`}
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
    >
      <span className={styles.icon}>{icon}</span>
      <span className={styles.label}>{label}</span>
      {badge !== undefined && (
        <span className={styles.badge} aria-label={`${badge} 条待处理`}>
          {badge > 99 ? '99+' : badge}
        </span>
      )}
    </button>
  )
}

/* Inline SVG icons */
function HomeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      <polyline points="9 22 9 12 15 12 15 22" />
    </svg>
  )
}

function TodoIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 11l3 3L22 4" />
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
    </svg>
  )
}

function GoalIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v3M22 12h-3M12 22v-3M2 12h3" />
    </svg>
  )
}

function MemoryIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3a4 4 0 0 0-4 4 4 4 0 0 0-1 7.9V18a3 3 0 0 0 6 0M12 3a4 4 0 0 1 4 4 4 4 0 0 1 1 7.9V18a3 3 0 0 1-6 0M12 3v15" />
    </svg>
  )
}

function SettingsIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.38a2 2 0 0 0-.73-2.73l-.15-.09a2 2 0 0 1-1-1.74v-.51a2 2 0 0 1 1-1.72l.15-.1a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  )
}
