import { type Soul } from '@/api/client'
import { useMeasuredHeight } from '@/hooks/useMeasuredHeight'
import { ChevronRightIcon } from '@/components/icons'
import { SoulAvatar } from './SoulAvatar'
import styles from './LeftNav.module.css'

/* 私聊区不滚动：按实测可用高度决定显示几个私聊入口。
 * 高度常量与 LeftNav.module.css 对应：dmItem 38px 头像 + 上下 8px padding，列表 gap 4px。 */
const DM_ITEM_HEIGHT = 54
const DM_GAP = 4
const VIEW_ALL_HEIGHT = 40
/** 再高的屏幕也最多显示这么多私聊，保持导航清爽。 */
const MAX_VISIBLE_DMS = 8

function dmFitCount(height: number): number {
  return Math.max(0, Math.floor((height + DM_GAP) / (DM_ITEM_HEIGHT + DM_GAP)))
}

interface LeftNavProps {
  souls: Soul[]
  soulsLoadState?: 'loading' | 'ready' | 'error'
  /** 待对账 evidence 数，>0 时在「记忆」项上显示 badge。 */
  memoryQueueCount?: number
  /** 进行中的目标数，>0 时在「目标」项上显示 badge */
  goalCount?: number
  activePage: string
  onNavigate: (page: string) => void
  onAfterNavigate?: () => void
}

export function LeftNav({
  souls,
  soulsLoadState = 'ready',
  memoryQueueCount = 0,
  goalCount = 0,
  activePage,
  onNavigate,
  onAfterNavigate,
}: LeftNavProps) {
  const navigate = (page: string) => {
    onNavigate(page)
    onAfterNavigate?.()
  }

  const [dmListRef, dmListHeight] = useMeasuredHeight<HTMLDivElement>()
  const fitAll = Math.min(dmFitCount(dmListHeight), MAX_VISIBLE_DMS)
  const needsViewAll = souls.length > fitAll
  let visibleSouls = needsViewAll
    ? souls.slice(0, Math.min(dmFitCount(dmListHeight - VIEW_ALL_HEIGHT - DM_GAP), MAX_VISIBLE_DMS))
    : souls
  /* 正在聊的好友被裁掉时顶进最后一个可见位置，保证左栏始终有高亮 */
  const activeSoulName = activePage.startsWith('chat:') ? activePage.slice('chat:'.length) : null
  if (activeSoulName && visibleSouls.length > 0 && !visibleSouls.some((soul) => soul.name === activeSoulName)) {
    const activeSoul = souls.find((soul) => soul.name === activeSoulName)
    if (activeSoul) visibleSouls = [...visibleSouls.slice(0, -1), activeSoul]
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
          icon={<ScheduleIcon />}
          label="日程"
          active={activePage === 'schedule'}
          onClick={() => navigate('schedule')}
        />
        <NavItem
          icon={<MemoryIcon />}
          label="记忆"
          badge={memoryQueueCount > 0 ? memoryQueueCount : undefined}
          active={activePage === 'memory'}
          onClick={() => navigate('memory')}
        />
      </section>

      <section className={`${styles.section} ${styles.dmSection}`}>
        <h3 className={styles.sectionTitle}>私聊</h3>
        <div className={styles.dmList} ref={dmListRef}>
          {visibleSouls.map((soul) => {
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
          {needsViewAll && (
            <button
              className={`${styles.viewAll} ${activePage === 'chats' ? styles.viewAllActive : ''}`}
              onClick={() => navigate('chats')}
              aria-current={activePage === 'chats' ? 'page' : undefined}
            >
              查看全部 {souls.length} 位好友
              <ChevronRightIcon width={13} height={13} />
            </button>
          )}
          {souls.length === 0 && soulsLoadState === 'loading' && (
            <p className={styles.emptySoul}>加载中...</p>
          )}
          {souls.length === 0 && soulsLoadState === 'error' && (
            <p className={styles.emptySoul} role="alert">加载失败</p>
          )}
          {souls.length === 0 && soulsLoadState === 'ready' && (
            <p className={styles.emptySoul}>还没有人格，去设置里创建</p>
          )}
        </div>
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

function GoalIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v3M22 12h-3M12 22v-3M2 12h3" />
    </svg>
  )
}

function ScheduleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="18" rx="2" />
      <line x1="16" y1="2" x2="16" y2="6" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
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
