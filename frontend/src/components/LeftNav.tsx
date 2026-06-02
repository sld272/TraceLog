import { type Soul } from '@/api/client'
import styles from './LeftNav.module.css'

interface LeftNavProps {
  souls: Soul[]
  activePage: string
  onNavigate: (page: string) => void
}

export function LeftNav({ souls, activePage, onNavigate }: LeftNavProps) {
  return (
    <div className={styles.nav}>
      <section className={styles.section}>
        <NavItem
          icon={<HomeIcon />}
          label="首页"
          active={activePage === 'home'}
          onClick={() => onNavigate('home')}
        />
        <NavItem
          icon={<TodoIcon />}
          label="待办"
          active={activePage === 'todos'}
          onClick={() => onNavigate('todos')}
        />
        <NavItem
          icon={<ReflectIcon />}
          label="反思"
          active={activePage === 'reflections'}
          onClick={() => onNavigate('reflections')}
        />
      </section>

      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>私聊</h3>
        {souls.map((soul) => (
          <NavItem
            key={soul.name}
            icon={<SoulIcon name={soul.name} />}
            label={soul.name}
            active={activePage === `chat:${soul.name}`}
            onClick={() => onNavigate(`chat:${soul.name}`)}
          />
        ))}
        {souls.length === 0 && (
          <p className={styles.emptySoul}>暂无活跃人格</p>
        )}
      </section>

      <section className={`${styles.section} ${styles.bottomSection}`}>
        <NavItem
          icon={<SettingsIcon />}
          label="设置"
          active={activePage === 'settings'}
          onClick={() => onNavigate('settings')}
        />
      </section>
    </div>
  )
}

interface NavItemProps {
  icon: React.ReactNode
  label: string
  active: boolean
  onClick: () => void
}

function NavItem({ icon, label, active, onClick }: NavItemProps) {
  return (
    <button
      className={`${styles.item} ${active ? styles.active : ''}`}
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
    >
      <span className={styles.icon}>{icon}</span>
      <span className={styles.label}>{label}</span>
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

function ReflectIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4" />
      <path d="M12 8h.01" />
    </svg>
  )
}

function SoulIcon({ name }: { name: string }) {
  const initial = name.charAt(0).toUpperCase()
  const hue = name.split('').reduce((acc, c) => acc + c.charCodeAt(0), 0) % 360
  return (
    <span
      className={styles.soulIcon}
      style={{ backgroundColor: `hsl(${hue}, 35%, 88%)`, color: `hsl(${hue}, 40%, 35%)` }}
    >
      {initial}
    </span>
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
