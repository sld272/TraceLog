import { type Post } from '@/api/client'
import styles from './PostCard.module.css'

interface PostCardProps {
  post: Post
  comments?: Array<{ soul_name: string; content: string }>
  onExpand?: () => void
}

export function PostCard({ post, comments = [], onExpand }: PostCardProps) {
  const timeAgo = formatRelativeTime(post.ts)

  return (
    <article className={styles.card}>
      <div className={styles.header}>
        <time className={styles.time} dateTime={post.ts} title={post.ts}>
          {timeAgo}
        </time>
        {post.importance > 0.7 && (
          <span className={styles.importanceBadge} title={`重要性: ${post.importance.toFixed(2)}`}>
            <StarIcon />
          </span>
        )}
      </div>

      <div className={styles.content}>
        {post.content}
      </div>

      {comments.length > 0 && (
        <div className={styles.comments}>
          {comments.map((comment, i) => (
            <CommentPreview key={i} soulName={comment.soul_name} content={comment.content} />
          ))}
        </div>
      )}

      {post.comment_count > 0 && comments.length === 0 && (
        <button className={styles.expandBtn} onClick={onExpand}>
          <ChatIcon />
          <span>{post.comment_count} 条回应</span>
        </button>
      )}

      {post.latest_event_type && post.latest_event_type !== 'pipeline_done' && (
        <div className={styles.processing}>
          <LoadingIndicator />
          <span>SOUL 正在思考...</span>
        </div>
      )}
    </article>
  )
}

function CommentPreview({ soulName, content }: { soulName: string; content: string }) {
  const hue = soulName.split('').reduce((acc, c) => acc + c.charCodeAt(0), 0) % 360
  return (
    <div className={styles.comment} style={{ backgroundColor: `hsl(${hue}, 30%, 97%)` }}>
      <span
        className={styles.soulBadge}
        style={{ backgroundColor: `hsl(${hue}, 35%, 88%)`, color: `hsl(${hue}, 40%, 35%)` }}
      >
        {soulName.charAt(0).toUpperCase()}
      </span>
      <div className={styles.commentBody}>
        <span className={styles.soulName}>{soulName}</span>
        <p className={styles.commentText}>{content}</p>
      </div>
    </div>
  )
}

function StarIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
    </svg>
  )
}

function ChatIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  )
}

function LoadingIndicator() {
  return (
    <span className={styles.loadingDots}>
      <span className={styles.dot} />
      <span className={styles.dot} />
      <span className={styles.dot} />
    </span>
  )
}

/* Time formatting */
function formatRelativeTime(ts: string): string {
  const date = new Date(ts)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffMin = Math.floor(diffMs / 60000)
  const diffHour = Math.floor(diffMs / 3600000)
  const diffDay = Math.floor(diffMs / 86400000)

  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin} 分钟前`
  if (diffHour < 24) return `${diffHour} 小时前`
  if (diffDay < 7) return `${diffDay} 天前`

  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}
