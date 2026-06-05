import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import {
  type Attachment,
  type Comment,
  type CommentConversation,
  type CommentMessage,
  type Post,
} from '@/api/client'
import { ImageGrid } from './ImageGrid'
import { ImageUploader } from './ImageUploader'
import { ChatIcon, LoadingDots, SendIcon, StarIcon } from '@/components/icons'
import { LAYOUT } from '@/utils/constants'
import { formatRelativeTime } from '@/utils/date'
import { getSubmitShortcutTitle } from '@/utils/shortcuts'
import styles from './PostCard.module.css'

export interface CommentConversationState {
  conversation?: CommentConversation
  messages: CommentMessage[]
  sending?: boolean
  error?: string | null
}

interface PostCardProps {
  post: Post
  comments?: Comment[]
  commentConversations?: Record<string, CommentConversationState>
  onExpand?: () => void
  onReply?: (soulName: string, content: string, attachments: Attachment[]) => Promise<void>
}

export function PostCard({
  post,
  comments = [],
  commentConversations = {},
  onExpand,
  onReply,
}: PostCardProps) {
  const timeAgo = formatRelativeTime(post.ts)

  return (
    <article className={styles.card}>
      <div className={styles.header}>
        <div className={styles.author}>
          <span className={styles.userAvatar}>我</span>
          <div>
            <span className={styles.userName}>你</span>
            <time className={styles.time} dateTime={post.ts} title={post.ts}>
              {timeAgo}
            </time>
          </div>
        </div>
        {post.importance > 0.7 && (
          <span className={styles.importanceBadge} title={`重要性: ${post.importance.toFixed(2)}`}>
            <StarIcon />
          </span>
        )}
      </div>

      {post.content && <div className={styles.content}>{post.content}</div>}
      <ImageGrid attachments={post.attachments ?? []} />

      {comments.length > 0 && (
        <div className={styles.comments}>
          {comments.map((comment) => (
            <CommentPreview
              key={comment.id}
              comment={comment}
              conversation={commentConversations[comment.soul_name]}
              onReply={onReply}
            />
          ))}
        </div>
      )}

      {post.comment_count > 0 && comments.length === 0 && (
        <button className={styles.expandBtn} onClick={onExpand}>
          <ChatIcon />
          <span>查看 {post.comment_count} 条回应</span>
        </button>
      )}

      {post.latest_event_type && post.latest_event_type !== 'pipeline_done' && (
        <div className={styles.processing}>
          <LoadingDots />
          <span>人格正在思考...</span>
        </div>
      )}
    </article>
  )
}
function CommentPreview({
  comment,
  conversation,
  onReply,
}: {
  comment: Comment
  conversation?: CommentConversationState
  onReply?: (soulName: string, content: string, attachments: Attachment[]) => Promise<void>
}) {
  const [reply, setReply] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const replyInputRef = useRef<HTMLTextAreaElement>(null)
  const soulName = comment.soul_name
  const hue = soulName.split('').reduce((acc, c) => acc + c.charCodeAt(0), 0) % 360
  const trimmed = reply.trim()
  const submitShortcutTitle = getSubmitShortcutTitle()

  useEffect(() => {
    const el = replyInputRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, LAYOUT.REPLY_TEXTAREA_MAX_HEIGHT)}px`
    }
  }, [reply])

  const handleSubmit = async () => {
    if ((!trimmed && attachments.length === 0) || conversation?.sending || !onReply) return
    await onReply(soulName, trimmed, attachments)
    setReply('')
    setAttachments([])
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      handleSubmit()
    }
  }

  return (
    <div className={styles.commentThread}>
      <div className={styles.comment} style={{ backgroundColor: `hsl(${hue}, 30%, 97%)` }}>
        <span
          className={styles.soulBadge}
          style={{ backgroundColor: `hsl(${hue}, 35%, 88%)`, color: `hsl(${hue}, 40%, 35%)` }}
        >
          {soulName.charAt(0).toUpperCase()}
        </span>
        <div className={styles.commentBody}>
          <span className={styles.soulName}>{soulName}</span>
          {comment.content && <p className={styles.commentText}>{comment.content}</p>}
          <ImageGrid attachments={comment.attachments ?? []} />
        </div>
      </div>

      {conversation?.messages && conversation.messages.some((message) => message.seq > 0) && (
        <div className={styles.threadMessages}>
          {conversation.messages.filter((message) => message.seq > 0).map((message) => (
            <ThreadMessage key={message.id} message={message} soulName={soulName} />
          ))}
        </div>
      )}

      <div className={styles.replyBox}>
        <div className={styles.replyInputGroup}>
          <textarea
            ref={replyInputRef}
            className={styles.replyInput}
            value={reply}
            onChange={(event) => setReply(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`回复 ${soulName}...`}
            rows={1}
            disabled={conversation?.sending}
            aria-label={`回复 ${soulName}`}
          />
          <ImageUploader
            attachments={attachments}
            compact
            disabled={conversation?.sending}
            onChange={setAttachments}
            showControls={false}
          />
        </div>
        <div className={styles.replyFooter}>
          {(reply.length > 0 || attachments.length > 0) && (
            <span className={styles.replyHint}>
              {reply.length} 字{attachments.length > 0 ? ` · ${attachments.length} 图` : ''}
            </span>
          )}
          <div className={styles.replyActions}>
            <ImageUploader
              attachments={attachments}
              compact
              disabled={conversation?.sending}
              onChange={setAttachments}
              showPreview={false}
            />
            <span className={styles.replyButtonWrap} title={submitShortcutTitle}>
              <button
                className={styles.replyButton}
                onClick={handleSubmit}
                disabled={(!trimmed && attachments.length === 0) || conversation?.sending || !onReply}
                aria-label={`发送给 ${soulName}`}
              >
                {conversation?.sending ? <LoadingDots /> : <SendIcon width={14} height={14} />}
              </button>
            </span>
          </div>
        </div>
      </div>
      {conversation?.error && <p className={styles.threadError}>{conversation.error}</p>}
    </div>
  )
}

function ThreadMessage({ message, soulName }: { message: CommentMessage; soulName: string }) {
  const isUser = message.role === 'user'
  return (
    <div className={`${styles.threadMessage} ${isUser ? styles.threadMessageUser : styles.threadMessageSoul}`}>
      <span className={styles.threadRole}>{isUser ? '你' : soulName}</span>
      {message.content && <p>{message.content}</p>}
      <ImageGrid attachments={message.attachments ?? []} />
    </div>
  )
}
