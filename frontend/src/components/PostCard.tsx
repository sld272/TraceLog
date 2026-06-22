import { memo, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import {
  type Attachment,
  type Comment,
  type CommentConversation,
  type CommentMessage,
  type PipelineJobSummary,
  type Post,
  parseMessageSuggestions,
} from '@/api/client'
import { EvidencePanel } from './EvidencePanel'
import { ImageGrid } from './ImageGrid'
import { ImageUploader } from './ImageUploader'
import { InlineSuggestions } from './InlineSuggestions'
import { SoulAvatar } from './SoulAvatar'
import { ChatIcon, LoadingDots, RefreshCwIcon, SendIcon, TrashIcon } from '@/components/icons'
import { LAYOUT } from '@/utils/constants'
import { formatAbsoluteTime, formatDateTimeAttribute, formatSmartTime } from '@/utils/date'
import { soulColors } from '@/utils/soulColor'
import styles from './PostCard.module.css'

const FEED_MAX_THREAD_MESSAGES = 4

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
  busyCommentId?: number | null
  retryingJobId?: number | null
  deletingPost?: boolean
  detailHref?: string
  variant?: 'feed' | 'detail'
  modelConfigured?: boolean | null
  expandLoading?: boolean
  expandError?: string | null
  onExpand?: () => void
  onReply?: (soulName: string, content: string, attachments: Attachment[]) => Promise<void>
  onDeletePost?: () => Promise<void>
  onDeleteComment?: (commentId: number) => Promise<void>
  onRerunComment?: (commentId: number) => Promise<void>
  onRetryFailedJobs?: (jobIds: number[]) => Promise<void>
}

export const PostCard = memo(function PostCard({
  post,
  comments = [],
  commentConversations = {},
  busyCommentId = null,
  retryingJobId = null,
  deletingPost = false,
  detailHref,
  variant = 'feed',
  modelConfigured = true,
  expandLoading = false,
  expandError = null,
  onExpand,
  onReply,
  onDeletePost,
  onDeleteComment,
  onRerunComment,
  onRetryFailedJobs,
}: PostCardProps) {
  const timeAgo = formatSmartTime(post.ts)
  const [showComments, setShowComments] = useState(true)
  const toggleComments = () => {
    const next = !showComments
    setShowComments(next)
    if (next && comments.length === 0 && onExpand) onExpand()
  }

  return (
    <article id={`post-${post.post_id}`} className={styles.card}>
      <div className={styles.header}>
        <div className={styles.author}>
          <span className={styles.userAvatar}>我</span>
          <div>
            <span className={styles.userName}>我</span>
            {detailHref ? (
              <a className={styles.timeLink} href={detailHref}>
                <time className={styles.time} dateTime={formatDateTimeAttribute(post.ts)} title={formatAbsoluteTime(post.ts)}>
                  {timeAgo}
                </time>
              </a>
            ) : (
              <time className={styles.time} dateTime={formatDateTimeAttribute(post.ts)} title={formatAbsoluteTime(post.ts)}>
                {timeAgo}
              </time>
            )}
          </div>
        </div>
        {onDeletePost && (
          <button className={styles.postAction} onClick={onDeletePost} disabled={deletingPost} title="删除记录" aria-label="删除记录">
            <TrashIcon />
          </button>
        )}
      </div>

      {post.content && <div id={`post-content-${post.post_id}`} className={styles.content}>{post.content}</div>}
      <ImageGrid attachments={post.attachments ?? []} />

      {post.comment_count > 0 && (
        <div className={styles.commentBar}>
          <button
            className={`${styles.commentToggle} ${showComments ? styles.commentToggleOn : ''}`}
            onClick={toggleComments}
            disabled={expandLoading}
            aria-expanded={showComments}
          >
            {expandLoading ? <LoadingDots /> : <ChatIcon />}
            <span>评论 {post.comment_count}</span>
          </button>
        </div>
      )}

      {showComments && comments.length > 0 && (
        <div className={styles.comments}>
          {comments.map((comment) => (
            <CommentPreview
              key={comment.id}
              comment={comment}
              conversation={commentConversations[comment.soul_name]}
              busyCommentId={busyCommentId}
              onReply={onReply}
              onDelete={onDeleteComment}
              onRerun={onRerunComment}
              modelConfigured={modelConfigured}
              detailHref={detailHref}
              variant={variant}
            />
          ))}
        </div>
      )}

      {showComments && expandError && (
        <p className={styles.expandError}>
          加载失败，点击重试：{expandError}
        </p>
      )}

      <PipelineNotice
        post={post}
        retryingJobId={retryingJobId}
        onRetryFailedJobs={onRetryFailedJobs}
      />
    </article>
  )
})

function PipelineNotice({
  post,
  retryingJobId,
  onRetryFailedJobs,
}: {
  post: Post
  retryingJobId: number | null
  onRetryFailedJobs?: (jobIds: number[]) => Promise<void>
}) {
  const status = post.pipeline_status
  const failedJobs = status?.failed_jobs ?? []
  const retryableJobIds = failedJobs.filter((job) => job.retryable).map((job) => job.id)

  if (failedJobs.length > 0) {
    return (
      <div className={styles.pipelineFailure}>
        <div className={styles.pipelineFailureMain}>
          <strong>{pipelineFailureTitle(failedJobs)}</strong>
          <div className={styles.pipelineActions}>
            {onRetryFailedJobs && retryableJobIds.length > 0 && (
              <button
                className={styles.pipelineRetryButton}
                onClick={() => onRetryFailedJobs(retryableJobIds)}
                disabled={retryingJobId !== null}
                title="重试"
                aria-label="重试失败处理"
              >
                {retryingJobId !== null ? <LoadingDots /> : <RefreshCwIcon />}
                <span>重试</span>
              </button>
            )}
          </div>
        </div>
        <details className={styles.pipelineDetails}>
          <summary>诊断信息</summary>
          <div className={styles.pipelineDiagnostics}>
            {failedJobs.map((job) => (
              <p key={job.id}>{formatPipelineError(job.error)}</p>
            ))}
          </div>
        </details>
      </div>
    )
  }

  if (status?.state === 'retrying') {
    return (
      <div className={styles.processing}>
        <LoadingDots />
        <span>正在自动重试...</span>
      </div>
    )
  }

  const isProcessing = status?.state === 'running'
    || (!status && post.latest_event_type && post.latest_event_type !== 'pipeline_done')
  if (!isProcessing) return null

  return (
    <div className={styles.processing}>
      <LoadingDots />
      <span>TA 们正在思考...</span>
    </div>
  )
}

function pipelineFailureTitle(failedJobs: PipelineJobSummary[]): string {
  return failedJobs.some((job) => job.type === 'generate_post_replies')
    ? 'AI 回复失败'
    : '处理失败'
}

function formatPipelineError(error: PipelineJobSummary['error']): string {
  const text = (error ?? '').trim()
  return text || '未知错误'
}
function CommentPreview({
  comment,
  conversation,
  busyCommentId,
  onReply,
  onDelete,
  onRerun,
  modelConfigured,
  detailHref,
  variant,
}: {
  comment: Comment
  conversation?: CommentConversationState
  busyCommentId: number | null
  onReply?: (soulName: string, content: string, attachments: Attachment[]) => Promise<void>
  onDelete?: (commentId: number) => Promise<void>
  onRerun?: (commentId: number) => Promise<void>
  modelConfigured: boolean | null
  detailHref?: string
  variant: 'feed' | 'detail'
}) {
  const [reply, setReply] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [replyOpen, setReplyOpen] = useState(false)
  const replyInputRef = useRef<HTMLTextAreaElement>(null)
  const soulName = comment.soul_name
  const colors = soulColors(soulName)
  const trimmed = reply.trim()
  const messages = conversation?.messages ?? []
  const threadMessages = messages.filter((message) => message.seq > 0)
  const visibleThreadMessages = visibleMessagesForVariant(threadMessages, variant)
  const isThreadTruncated = visibleThreadMessages.length < threadMessages.length
  const latestMessage = latestConversationMessage(comment, messages)
  const canRerunRoot = latestMessage?.id === comment.id && latestMessage.role === 'assistant'
  const rootBusy = busyCommentId === comment.id
  const rootPending = rootBusy && comment.role === 'assistant'
  const replyBusy = Boolean(conversation?.sending || rootBusy)
  const replyInputDisabled = !onReply || modelConfigured === false
  const replySubmitDisabled = replyInputDisabled || replyBusy

  useEffect(() => {
    const el = replyInputRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, LAYOUT.REPLY_TEXTAREA_MAX_HEIGHT)}px`
    }
  }, [reply])

  useEffect(() => {
    if (replyOpen) replyInputRef.current?.focus()
  }, [replyOpen])

  const handleSubmit = async () => {
    if ((!trimmed && attachments.length === 0) || replySubmitDisabled) return
    const submittedReply = reply
    const submittedAttachments = attachments
    setReply('')
    setAttachments([])
    try {
      await onReply(soulName, trimmed, attachments)
    } catch (err) {
      setReply((current) => current ? current : submittedReply)
      setAttachments((current) => current.length > 0 ? current : submittedAttachments)
    }
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      handleSubmit()
    }
  }

  return (
    <div id={`comment-${comment.id}`} className={styles.commentThread}>
      <div className={styles.comment}>
        <SoulAvatar name={soulName} className={styles.soulBadge} />
        <div className={styles.commentBody}>
          <div className={styles.commentHeader}>
            <span className={styles.soulName} style={{ color: colors.badgeText }}>{soulName}</span>
            <time
              className={styles.commentTime}
              dateTime={formatDateTimeAttribute(comment.created_at)}
              title={formatAbsoluteTime(comment.created_at)}
            >
              {formatSmartTime(comment.created_at)}
            </time>
            <div className={styles.messageActions}>
              {!rootPending && <RerunMarker at={comment.rerun_at} className={styles.messageMarker} />}
              {canRerunRoot && onRerun && (
                <button className={styles.inlineAction} onClick={() => onRerun(comment.id)} disabled={rootBusy} title="重跑" aria-label={`重跑 ${soulName} 的回复`}>
                  <RefreshCwIcon />
                </button>
              )}
            </div>
          </div>
          {rootPending ? (
            <div className={styles.threadPending} aria-label={`${soulName} 正在回复`}>
              <LoadingDots />
            </div>
          ) : (
            <>
              {comment.content && <p className={styles.commentText}>{comment.content}</p>}
              <ImageGrid attachments={comment.attachments ?? []} />
              <InlineSuggestions suggestions={parseMessageSuggestions(comment.metadata)} />
              <EvidencePanel
                metadata={comment.metadata}
                channel="public_post"
                messageId={comment.id}
                compact
              />
            </>
          )}
        </div>
      </div>

      {threadMessages.length > 0 && (
        <div className={styles.threadMessages}>
          {isThreadTruncated && detailHref && (
            <a className={styles.threadMoreLink} href={detailHref}>
              在详情中查看完整对话（共 {threadMessages.length + 1} 条）→
            </a>
          )}
          {visibleThreadMessages.map((message) => (
            <ThreadMessage
              key={message.id}
              message={message}
              soulName={soulName}
              isLatest={latestMessage?.id === message.id}
              busy={busyCommentId === message.id}
              onDelete={onDelete}
              onRerun={onRerun}
            />
          ))}
        </div>
      )}

      {onReply && (
      <div className={styles.replyArea}>
        <button
          type="button"
          className={`${styles.replyTrigger} ${replyOpen ? styles.replyTriggerOn : ''}`}
          onClick={() => setReplyOpen((open) => !open)}
          disabled={replyInputDisabled}
        >
          回复
        </button>
        {replyOpen && (
        <div className={styles.replyBox}>
          {attachments.length > 0 && (
            <ImageUploader
              attachments={attachments}
              compact
              disabled={replyInputDisabled}
              onChange={setAttachments}
              showControls={false}
            />
          )}
          <div className={styles.replyRow}>
          <textarea
            ref={replyInputRef}
            className={styles.replyInput}
            value={reply}
            onChange={(event) => setReply(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`回复 ${soulName}...`}
            rows={1}
            disabled={replyInputDisabled}
            aria-label={`回复 ${soulName}`}
          />
          <ImageUploader
            attachments={attachments}
            compact
            disabled={replyInputDisabled}
            onChange={setAttachments}
            showPreview={false}
          />
          <span className={`${styles.replyButtonWrap} kbdTip`}>
            <button
              className={styles.replyButton}
              onClick={handleSubmit}
              disabled={(!trimmed && attachments.length === 0) || replySubmitDisabled}
              aria-label={`发送给 ${soulName}`}
            >
              {replyBusy ? <LoadingDots /> : <SendIcon width={14} height={14} />}
            </button>
            <span className="kbdTipBubble" role="tooltip">
              发送 <span className="kbdTipKey">Enter</span>
            </span>
          </span>
          </div>
        </div>
        )}
      </div>
      )}
      {conversation?.error && (
        <ReplyFailureInline
          error={conversation.error}
          onRetry={latestMessage.role === 'assistant' && onRerun ? () => onRerun(latestMessage.id) : undefined}
          busy={replyBusy}
        />
      )}
    </div>
  )
}

function visibleMessagesForVariant(
  messages: CommentMessage[],
  variant: 'feed' | 'detail',
): CommentMessage[] {
  if (variant === 'detail' || messages.length <= FEED_MAX_THREAD_MESSAGES - 1) return messages
  const tail = messages.slice(-(FEED_MAX_THREAD_MESSAGES - 1))
  const optimistic = messages.filter((message) => message.id < 0 && !tail.some((item) => item.id === message.id))
  const visibleIds = new Set([...tail, ...optimistic].map((message) => message.id))
  return messages.filter((message) => visibleIds.has(message.id))
}

function ReplyFailureInline({
  error,
  onRetry,
  busy,
}: {
  error: string
  onRetry?: () => void
  busy: boolean
}) {
  return (
    <div className={styles.threadError}>
      <div className={styles.threadErrorMain}>
        <strong>回复生成失败</strong>
        <div className={styles.threadErrorActions}>
          {onRetry && (
            <button className={styles.pipelineRetryButton} onClick={onRetry} disabled={busy}>
              <RefreshCwIcon />
              <span>重试</span>
            </button>
          )}
        </div>
      </div>
      <details className={styles.pipelineDetails}>
        <summary>诊断信息</summary>
        <div className={styles.pipelineDiagnostics}>
          <p>{error}</p>
        </div>
      </details>
    </div>
  )
}

function ThreadMessage({
  message,
  soulName,
  isLatest,
  busy,
  onDelete,
  onRerun,
}: {
  message: CommentMessage
  soulName: string
  isLatest: boolean
  busy: boolean
  onDelete?: (commentId: number) => Promise<void>
  onRerun?: (commentId: number) => Promise<void>
}) {
  const isUser = message.role === 'user'
  const isPersisted = message.id > 0
  const failure = failedCommentReplyError(message)
  const isFailedAssistant = message.role === 'assistant' && Boolean(failure)
  const isPendingAssistant = message.role === 'assistant' && !failure && !message.content && (message.id < 0 || busy)
  return (
    <div id={`comment-${message.id}`} className={`${styles.threadRow} ${isUser ? styles.threadRowUser : ''}`}>
      {isUser ? (
        <span className={`${styles.threadAvatar} ${styles.threadAvatarMe}`} aria-hidden="true">我</span>
      ) : (
        <SoulAvatar name={soulName} className={styles.threadAvatar} />
      )}
      <div className={styles.threadCol}>
        <div className={styles.threadHeader}>
          <span className={styles.threadRole}>{isUser ? '我' : soulName}</span>
          <time
            className={styles.threadTime}
            dateTime={formatDateTimeAttribute(message.created_at)}
            title={formatAbsoluteTime(message.created_at)}
          >
            {formatSmartTime(message.created_at)}
          </time>
          <div className={styles.threadActionRow}>
            {!isPendingAssistant && <RerunMarker at={message.rerun_at} className={styles.threadMarker} />}
            {isPersisted && isLatest && message.role === 'assistant' && !isFailedAssistant && onRerun && (
              <button className={styles.threadAction} onClick={() => onRerun(message.id)} disabled={busy} title="重跑" aria-label={`重跑 ${soulName} 的回复`}>
                <RefreshCwIcon />
              </button>
            )}
            {isPersisted && isUser && onDelete && (
              <button className={styles.threadDanger} onClick={() => onDelete(message.id)} disabled={busy} title="删除追问" aria-label="删除追问">
                <TrashIcon />
              </button>
            )}
          </div>
        </div>
        {isPendingAssistant ? (
          <div className={`${styles.threadBubble} ${styles.threadBubbleSoul}`}>
            <div className={styles.threadPending} aria-label={`${soulName} 正在回复`}>
              <LoadingDots />
            </div>
          </div>
        ) : isFailedAssistant ? (
          <ReplyFailureBubble
            error={failure}
            onRetry={isPersisted && onRerun ? () => onRerun(message.id) : undefined}
            busy={busy}
          />
        ) : message.content ? (
          <div className={`${styles.threadBubble} ${isUser ? styles.threadBubbleUser : styles.threadBubbleSoul}`}>
            <p>{message.content}</p>
          </div>
        ) : null}
        <ImageGrid attachments={message.attachments ?? []} borderless={isUser} />
        {!isUser && !isFailedAssistant && !isPendingAssistant && (
          <>
            <InlineSuggestions suggestions={parseMessageSuggestions(message.metadata)} />
            <EvidencePanel metadata={message.metadata} channel="comment" messageId={message.id} compact />
          </>
        )}
      </div>
    </div>
  )
}

function RerunMarker({ at, className }: { at?: number | null; className?: string }) {
  if (!at) return null
  return (
    <span className={className} title={formatAbsoluteTime(at)}>
      已重新生成 · {formatSmartTime(at)}
    </span>
  )
}

function ReplyFailureBubble({
  error,
  onRetry,
  busy,
}: {
  error: string | null
  onRetry?: () => void
  busy: boolean
}) {
  return (
    <div className={styles.threadFailureBubble}>
      <div className={styles.threadErrorMain}>
        <strong>回复生成失败</strong>
        <div className={styles.threadErrorActions}>
          {onRetry && (
            <button className={styles.pipelineRetryButton} onClick={onRetry} disabled={busy}>
              <RefreshCwIcon />
              <span>重试</span>
            </button>
          )}
        </div>
      </div>
      <details className={styles.pipelineDetails}>
        <summary>诊断信息</summary>
        <div className={styles.pipelineDiagnostics}>
          <p>{error || '未知错误'}</p>
        </div>
      </details>
    </div>
  )
}

function failedCommentReplyError(message: CommentMessage): string | null {
  if (message.role !== 'assistant' || !message.metadata) return null
  try {
    const parsed = JSON.parse(message.metadata) as { status?: unknown; error?: unknown }
    if (parsed.status !== 'failed') return null
    return typeof parsed.error === 'string' && parsed.error.trim()
      ? parsed.error.trim()
      : '回复生成失败'
  } catch {
    return null
  }
}

function latestConversationMessage(root: Comment, messages: CommentMessage[]): Comment | CommentMessage {
  if (messages.length === 0) return root
  return [...messages].sort((a, b) => {
    if (a.seq !== b.seq) return b.seq - a.seq
    return b.id - a.id
  })[0] ?? root
}
