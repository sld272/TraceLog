import { type Soul } from '@/api/client'
import { SoulAvatar } from '@/components/SoulAvatar'
import pageStyles from './WorkspacePages.module.css'
import styles from './ChatsPage.module.css'

interface ChatsPageProps {
  souls: Soul[]
  loadState: 'loading' | 'ready' | 'error'
  onOpenChat: (soulName: string) => void
}

/** 全部私聊：左栏放不下时的完整好友列表入口。 */
export function ChatsPage({ souls, loadState, onOpenChat }: ChatsPageProps) {
  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.header}>
        <div className={pageStyles.titleGroup}>
          <h1 className={pageStyles.title}>私聊</h1>
          <p className={pageStyles.subtitle}>全部 AI 好友都在这里，挑一位继续聊。</p>
        </div>
      </header>
      {souls.length === 0 ? (
        <p className={styles.empty} role={loadState === 'error' ? 'alert' : undefined}>
          {loadState === 'loading'
            ? '加载中...'
            : loadState === 'error'
              ? '加载失败，稍后再试。'
              : '还没有人格，去设置里创建一个吧。'}
        </p>
      ) : (
        <div className={styles.grid}>
          {souls.map((soul) => (
            <button key={soul.name} type="button" className={styles.card} onClick={() => onOpenChat(soul.name)}>
              <SoulAvatar name={soul.name} className={styles.avatar} />
              <span className={styles.body}>
                <span className={styles.name}>{soul.name}</span>
                {soul.description && <span className={styles.desc}>{soul.description}</span>}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
