import styles from './RightPanel.module.css'

interface RightPanelProps {
  profileContent: string | null
  souls: Array<{ name: string; description: string | null }>
}

export function RightPanel({ profileContent, souls }: RightPanelProps) {
  return (
    <div className={styles.panel}>
      {/* Profile card */}
      <section className={styles.card}>
        <h3 className={styles.cardTitle}>
          <ProfileIcon />
          <span>我的画像</span>
        </h3>
        <div className={styles.cardContent}>
          {profileContent ? (
            <p className={styles.profileText}>
              {profileContent.slice(0, 200)}
              {profileContent.length > 200 && '...'}
            </p>
          ) : (
            <p className={styles.empty}>画像尚未生成</p>
          )}
        </div>
      </section>

      {/* Active SOULs */}
      <section className={styles.card}>
        <h3 className={styles.cardTitle}>
          <SoulsIcon />
          <span>活跃 SOUL</span>
        </h3>
        <div className={styles.soulList}>
          {souls.map((soul) => {
            const hue = soul.name.split('').reduce((acc, c) => acc + c.charCodeAt(0), 0) % 360
            return (
              <div key={soul.name} className={styles.soulItem}>
                <span
                  className={styles.soulAvatar}
                  style={{ backgroundColor: `hsl(${hue}, 35%, 88%)`, color: `hsl(${hue}, 40%, 35%)` }}
                >
                  {soul.name.charAt(0).toUpperCase()}
                </span>
                <div className={styles.soulInfo}>
                  <span className={styles.soulName}>{soul.name}</span>
                  {soul.description && (
                    <span className={styles.soulDesc}>{soul.description}</span>
                  )}
                </div>
              </div>
            )
          })}
          {souls.length === 0 && (
            <p className={styles.empty}>暂无活跃 SOUL</p>
          )}
        </div>
      </section>
    </div>
  )
}

function ProfileIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  )
}

function SoulsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  )
}
