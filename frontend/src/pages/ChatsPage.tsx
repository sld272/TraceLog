import { type Soul } from '@/api/client'
import { ChevronRightIcon } from '@/components/icons'
import { SoulAvatar } from '@/components/SoulAvatar'
import pageStyles from './WorkspacePages.module.css'
import styles from './ChatsPage.module.css'

interface ChatsPageProps {
  souls: Soul[]
  loadState: 'loading' | 'ready' | 'error'
  /** 每位好友的未读消息数，>0 时在卡片上打点。 */
  unreadBySoul?: Record<string, number>
  onOpenChat: (soulName: string) => void
}

/** 全部私聊：左栏放不下时的完整好友列表入口。 */
export function ChatsPage({ souls, loadState, unreadBySoul, onOpenChat }: ChatsPageProps) {
  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.header}>
        <div className={pageStyles.titleGroup}>
          <h1 className={pageStyles.title}>私聊</h1>
          <p className={pageStyles.subtitle}>{souls.length} 位好友</p>
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
        <div className={styles.list}>
          {souls.map((soul) => {
            const unread = unreadBySoul?.[soul.name] ?? 0
            return (
              <button key={soul.name} type="button" className={styles.row} onClick={() => onOpenChat(soul.name)}>
                <SoulAvatar name={soul.name} className={styles.avatar} />
                <span className={styles.body}>
                  <span className={styles.nameLine}>
                    <span className={styles.name}>{soul.name}</span>
                    {unread > 0 && <span className={styles.unread} role="img" aria-label={`${unread} 条未读`} />}
                  </span>
                  {/* 人格说明在左栏永远会被截断，这一页是它唯一能说全的地方 */}
                  {soul.description && <span className={styles.desc}>{soul.description}</span>}
                </span>
                {/* 行尾的箭头顶住右边缘：整行是可点的，右半边不能只是一片空白 */}
                <span className={styles.rowEnd} aria-hidden="true">
                  <ChevronRightIcon width={16} height={16} />
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
