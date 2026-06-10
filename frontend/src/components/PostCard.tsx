import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import {
  type Attachment,
  type Comment,
  type CommentConversation,
  type CommentMessage,
  type PipelineJobSummary,
  type Post,
} from '@/api/client'
import { EvidencePanel } from './EvidencePanel'
import { ImageGrid } from './ImageGrid'
import { ImageUploader } from './ImageUploader'
import { ChatIcon, LoadingDots, RefreshCwIcon, SendIcon, StarIcon, TrashIcon } from '@/components/icons'
import { LAYOUT } from '@/utils/constants'
import { formatAbsoluteTime, formatDateTimeAttribute, formatSmartTime } from '@/utils/date'
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
  busyCommentId?: number | null
  regeneratedCommentId?: number | null
  retryingJobId?: number | null
  deletingPost?: boolean
  expandLoading?: boolean
  expandError?: string | null
  onExpand?: () => void
  onReply?: (soulName: string, content: string, attachments: Attachment[]) => Promise<void>
  onDeletePost?: () => Promise<void>
  onDeleteComment?: (commentId: number) => Promise<void>
  onRerunComment?: (commentId: number) => Promise<void>
  onRetryFailedJobs?: (jobIds: number[]) => Promise<void>
}

export function PostCard({
  post,
  comments = [],
  commentConversations = {},
  busyCommentId = null,
  regeneratedCommentId = null,
  retryingJobId = null,
  deletingPost = false,
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

  return (
    <article className={styles.card}>
      <div className={styles.header}>
        <div className={styles.author}>
          <span className={styles.userAvatar}>我</span>
          <div>
            <span className={styles.userName}>你</span>
            <time className={styles.time} dateTime={formatDateTimeAttribute(post.ts)} title={formatAbsoluteTime(post.ts)}>
              {timeAgo}
            </time>
          </div>
        </div>
        {post.importance > 0.7 && (
          <span className={styles.importanceBadge} title={`重要性: ${post.importance.toFixed(2)}`}>
            <StarIcon />
          </span>
        )}
        {onDeletePost && (
          <button className={styles.postAction} onClick={onDeletePost} disabled={deletingPost} title="删除 post" aria-label="删除 post">
            <TrashIcon />
          </button>
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
              busyCommentId={busyCommentId}
              regeneratedCommentId={regeneratedCommentId}
              onReply={onReply}
              onDelete={onDeleteComment}
              onRerun={onRerunComment}
            />
          ))}
        </div>
      )}

      {post.comment_count > 0 && comments.length === 0 && (
        <>
          <button className={styles.expandBtn} onClick={onExpand} disabled={expandLoading}>
            {expandLoading ? <LoadingDots /> : <ChatIcon />}
            <span>{expandLoading ? '加载回应中...' : `查看 ${post.comment_count} 条回应`}</span>
          </button>
          {expandError && (
            <p className={styles.expandError}>
              加载失败，点击重试：{expandError}
            </p>
          )}
        </>
      )}

      <PipelineNotice
        post={post}
        retryingJobId={retryingJobId}
        onRetryFailedJobs={onRetryFailedJobs}
      />
    </article>
  )
}

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
      <span>TA 正在思考...</span>
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
  regeneratedCommentId,
  onReply,
  onDelete,
  onRerun,
}: {
  comment: Comment
  conversation?: CommentConversationState
  busyCommentId: number | null
  regeneratedCommentId: number | null
  onReply?: (soulName: string, content: string, attachments: Attachment[]) => Promise<void>
  onDelete?: (commentId: number) => Promise<void>
  onRerun?: (commentId: number) => Promise<void>
}) {
  const [reply, setReply] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const replyInputRef = useRef<HTMLTextAreaElement>(null)
  const soulName = comment.soul_name
  const hue = soulName.split('').reduce((acc, c) => acc + c.charCodeAt(0), 0) % 360
  const trimmed = reply.trim()
  const submitShortcutTitle = getSubmitShortcutTitle()
  const messages = conversation?.messages ?? []
  const latestMessage = latestConversationMessage(comment, messages)
  const canRerunRoot = latestMessage?.id === comment.id && latestMessage.role === 'assistant'
  const rootBusy = busyCommentId === comment.id
  const rootPending = rootBusy && comment.role === 'assistant'
  const replyBusy = Boolean(conversation?.sending || rootBusy)

  useEffect(() => {
    const el = replyInputRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, LAYOUT.REPLY_TEXTAREA_MAX_HEIGHT)}px`
    }
  }, [reply])

  const handleSubmit = async () => {
    if ((!trimmed && attachments.length === 0) || replyBusy || !onReply) return
    const submittedReply = reply
    const submittedAttachments = attachments
    setReply('')
    setAttachments([])
    try {
      await onReply(soulName, trimmed, attachments)
    } catch (err) {
      setReply(submittedReply)
      setAttachments(submittedAttachments)
    }
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
          <div className={styles.commentHeader}>
            <span className={styles.soulName}>{soulName}</span>
            <time
              className={styles.commentTime}
              dateTime={formatDateTimeAttribute(comment.created_at)}
              title={formatAbsoluteTime(comment.created_at)}
            >
              {formatSmartTime(comment.created_at)}
            </time>
            <div className={styles.messageActions}>
              {regeneratedCommentId === comment.id && <span className={styles.messageMarker}>已重新生成</span>}
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

      {messages.some((message) => message.seq > 0) && (
        <div className={styles.threadMessages}>
          {messages.filter((message) => message.seq > 0).map((message) => (
            <ThreadMessage
              key={message.id}
              message={message}
              soulName={soulName}
              isLatest={latestMessage?.id === message.id}
              busy={busyCommentId === message.id}
              regenerated={regeneratedCommentId === message.id}
              onDelete={onDelete}
              onRerun={onRerun}
            />
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
            disabled={replyBusy}
            aria-label={`回复 ${soulName}`}
          />
          <ImageUploader
            attachments={attachments}
            compact
            disabled={replyBusy}
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
              disabled={replyBusy}
              onChange={setAttachments}
              showPreview={false}
            />
            <span className={styles.replyButtonWrap} title={submitShortcutTitle}>
              <button
                className={styles.replyButton}
                onClick={handleSubmit}
                disabled={(!trimmed && attachments.length === 0) || replyBusy || !onReply}
                aria-label={`发送给 ${soulName}`}
              >
                {replyBusy ? <LoadingDots /> : <SendIcon width={14} height={14} />}
              </button>
            </span>
          </div>
        </div>
      </div>
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
  regenerated,
  onDelete,
  onRerun,
}: {
  message: CommentMessage
  soulName: string
  isLatest: boolean
  busy: boolean
  regenerated: boolean
  onDelete?: (commentId: number) => Promise<void>
  onRerun?: (commentId: number) => Promise<void>
}) {
  const isUser = message.role === 'user'
  const isPersisted = message.id > 0
  const failure = failedCommentReplyError(message)
  const isFailedAssistant = message.role === 'assistant' && Boolean(failure)
  const isPendingAssistant = message.role === 'assistant' && !failure && !message.content && (message.id < 0 || busy)
  return (
    <div className={`${styles.threadMessage} ${isUser ? styles.threadMessageUser : styles.threadMessageSoul}`}>
      <div className={styles.threadHeader}>
        <span className={styles.threadRole}>{isUser ? '你' : soulName}</span>
        <time
          className={styles.threadTime}
          dateTime={formatDateTimeAttribute(message.created_at)}
          title={formatAbsoluteTime(message.created_at)}
        >
          {formatSmartTime(message.created_at)}
        </time>
        <div className={styles.threadActionRow}>
          {regenerated && <span className={styles.threadMarker}>已重新生成</span>}
          {isPersisted && isLatest && message.role === 'assistant' && !isFailedAssistant && onRerun && (
            <button className={styles.threadAction} onClick={() => onRerun(message.id)} disabled={busy} title="重跑" aria-label={`重跑 ${soulName} 的回复`}>
              <RefreshCwIcon />
            </button>
          )}
          {isPersisted && isUser && onDelete && (
            <button className={styles.threadDanger} onClick={() => onDelete(message.id)} disabled={busy} title="删除评论" aria-label="删除评论">
              <TrashIcon />
            </button>
          )}
        </div>
      </div>
      {message.content && <p>{message.content}</p>}
      {isFailedAssistant && (
        <ReplyFailureBubble
          error={failure}
          onRetry={isPersisted && onRerun ? () => onRerun(message.id) : undefined}
          busy={busy}
        />
      )}
      {isPendingAssistant && (
        <div className={styles.threadPending} aria-label={`${soulName} 正在回复`}>
          <LoadingDots />
        </div>
      )}
      <ImageGrid attachments={message.attachments ?? []} />
      {!isUser && !isFailedAssistant && !isPendingAssistant && (
        <EvidencePanel metadata={message.metadata} channel="comment" messageId={message.id} compact />
      )}
    </div>
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
