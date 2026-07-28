import { memo, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import {
  type Attachment,
  type Comment,
  type CommentConversation,
  type CommentMessage,
  type GoalActivity,
  type GoalActivityKind,
  type PipelineJobSummary,
  type Post,
  type Suggestion,
  parseMessageSuggestions,
  rejectGoalActivity,
} from '@/api/client'
import { DiagnosticsButton } from './DiagnosticsButton'
import { EvidencePanel } from './EvidencePanel'
import { GoalActivityTagger } from './GoalActivityTagger'
import { ImageGrid } from './ImageGrid'
import { ImageUploader } from './ImageUploader'
import { InlineSuggestions } from './InlineSuggestions'
import { SoulAvatar } from './SoulAvatar'
import {
  ChatIcon,
  ChevronRightIcon,
  LoadingDots,
  MoreHorizontalIcon,
  RefreshCwIcon,
  SendIcon,
  TrashIcon,
} from '@/components/icons'
import { LAYOUT } from '@/utils/constants'
import { formatAbsoluteTime, formatDateTimeAttribute, formatSmartTime, formatTimeOfDay } from '@/utils/date'
import { ACTIVITY_KIND_LABELS } from '@/utils/goalActivity'
import { useSoulColors } from './SoulColorContext'
import styles from './PostCard.module.css'

/* 首页每位 SOUL 只留最新一个来回（我的追问 + TA 的回复），更早的去详情页看。
   折叠掉的是已经读过的开头，眼前留的是刚发生的事——反过来会让人以为消息没发出去。 */
const FEED_THREAD_TAIL = 2

export interface CommentConversationState {
  conversation?: CommentConversation
  messages: CommentMessage[]
  /** 追问总条数（含未随首页返回的部分）。缺省时按手上的消息数算。 */
  threadTotal?: number
  sending?: boolean
  error?: string | null
}

interface PostCardProps {
  post: Post
  comments?: Comment[]
  suggestions?: Suggestion[]
  commentConversations?: Record<string, CommentConversationState>
  busyCommentId?: number | null
  retryingJobId?: number | null
  deletingPost?: boolean
  detailHref?: string
  variant?: 'feed' | 'detail'
  /** 'clock' 只显示时刻，留给日期已由时间线分组锚交代过的场景。 */
  timeStyle?: 'smart' | 'clock'
  modelConfigured?: boolean | null
  expandLoading?: boolean
  expandError?: boolean
  onExpand?: () => void
  onReply?: (soulName: string, content: string, attachments: Attachment[]) => Promise<void>
  onDeletePost?: () => Promise<void>
  onDeleteComment?: (commentId: number) => Promise<void>
  onRerunComment?: (commentId: number) => Promise<void>
  onRetryFailedJobs?: (jobIds: number[]) => Promise<void>
  /** 处理迟迟没有结果时，用户主动放弃这一次并重排。 */
  onRestartStuckJobs?: (jobIds: number[]) => Promise<void>
}

export const PostCard = memo(function PostCard({
  post,
  comments = [],
  suggestions = [],
  commentConversations = {},
  busyCommentId = null,
  retryingJobId = null,
  deletingPost = false,
  detailHref,
  variant = 'feed',
  timeStyle = 'smart',
  modelConfigured = true,
  expandLoading = false,
  expandError = false,
  onExpand,
  onReply,
  onDeletePost,
  onDeleteComment,
  onRerunComment,
  onRetryFailedJobs,
  onRestartStuckJobs,
}: PostCardProps) {
  const timeAgo = timeStyle === 'clock' ? formatTimeOfDay(post.ts) : formatSmartTime(post.ts)
  const [goalActivities, setGoalActivities] = useState(post.goal_activities)
  const [rejectingActivityId, setRejectingActivityId] = useState<number | null>(null)
  const [activityError, setActivityError] = useState<string | null>(null)
  const [taggerOpen, setTaggerOpen] = useState(false)
  /* 好友的回应是这个产品的主要内容，评论一旦拿到就展开 —— 折叠它等于把首页
     变成一列自言自语。null 表示"跟随数据"，用户手动折叠后才固定下来。 */
  const [commentsToggled, setCommentsToggled] = useState<boolean | null>(null)
  const showComments = commentsToggled ?? (variant === 'detail' || comments.length > 0)
  const evidenceRef = `post:${post.post_id}`
  useEffect(() => {
    setGoalActivities(post.goal_activities)
    setActivityError(null)
  }, [post.goal_activities])

  /* 幂等键是 (goal_id, evidence_ref)，所以只有帖子自己那条 ref 会挡住补标；
     挂在本帖评论上的动态用的是 comment: 前缀，不构成冲突。 */
  const taggedKindByGoal = useMemo(() => {
    const map = new Map<string, GoalActivityKind>()
    for (const activity of goalActivities) {
      if (activity.evidence_ref === evidenceRef) map.set(activity.goal_id, activity.kind)
    }
    return map
  }, [goalActivities, evidenceRef])

  const closeTagger = useCallback(() => setTaggerOpen(false), [])

  const addActivity = useCallback((activity: GoalActivity, goalTitle: string) => {
    setActivityError(null)
    setGoalActivities((current) => [
      ...current.filter((item) => item.id !== activity.id),
      { ...activity, goal_title: goalTitle },
    ])
  }, [])

  const rejectActivity = async (activityId: number) => {
    setRejectingActivityId(activityId)
    setActivityError(null)
    try {
      await rejectGoalActivity(activityId)
      setGoalActivities((current) => current.filter((activity) => activity.id !== activityId))
    } catch {
      setActivityError('撤销失败，请重试。')
    } finally {
      setRejectingActivityId(null)
    }
  }

  const toggleComments = () => {
    const next = !showComments
    setCommentsToggled(next)
    if (next && comments.length === 0 && onExpand) onExpand()
  }

  return (
    <article id={`post-${post.post_id}`} className={`${styles.card} ${taggerOpen ? styles.cardRaised : ''}`}>
      {/* 这里只有一个作者，头像和"我"两个字不承载任何信息，只留时刻 */}
      <div className={styles.header}>
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
        <div className={styles.headerActions}>
          {/* 补标是自动识别漏了才用的低频兜底，收进「更多」里，不占显眼位置。
              stopPropagation：搜索结果里整张卡是 role="link"，点击和 Enter/空格
              都会跳详情页，不掐断就一打开弹层立刻被导航走；Escape 走 document
              监听，所以这里只拦这三种。 */}
          <div
            className={styles.postMoreWrap}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') event.stopPropagation()
            }}
          >
            <button
              className={`${styles.postAction} ${styles.postActionQuiet} ${taggerOpen ? styles.postActionOn : ''}`}
              onClick={() => setTaggerOpen((open) => !open)}
              data-tip="更多"
              data-tip-align="end"
              aria-label="更多操作"
              aria-haspopup="dialog"
              aria-expanded={taggerOpen}
            >
              <MoreHorizontalIcon />
            </button>
            {taggerOpen && (
              <GoalActivityTagger
                evidenceRef={evidenceRef}
                taggedKindByGoal={taggedKindByGoal}
                onClose={closeTagger}
                onRecorded={addActivity}
              />
            )}
          </div>
          {onDeletePost && (
            <button className={styles.postAction} onClick={onDeletePost} disabled={deletingPost} data-tip="删除记录" data-tip-align="end" aria-label="删除记录">
              <TrashIcon />
            </button>
          )}
        </div>
      </div>

      {post.content && <div id={`post-content-${post.post_id}`} className={styles.content}>{post.content}</div>}
      <ImageGrid attachments={post.attachments ?? []} />

      {goalActivities.length > 0 && (
        <div className={styles.goalActivityChips}>
          {goalActivities.map((activity) => {
            const label = `${ACTIVITY_KIND_LABELS[activity.kind]} · ${activity.goal_title}`
            return (
              <span
                key={activity.id}
                className={styles.goalActivityChip}
                title={activity.evidence_span ?? undefined}
              >
                <span>{label}</span>
                {/* stopPropagation：搜索结果里整张卡是 role="link"，
                    不掐断冒泡会边撤销边跳走 */}
                <button
                  type="button"
                  className={styles.goalActivityUndo}
                  aria-label={`撤销${label}`}
                  disabled={rejectingActivityId !== null}
                  onClick={(event) => {
                    event.stopPropagation()
                    void rejectActivity(activity.id)
                  }}
                >
                  ×
                </button>
              </span>
            )
          })}
        </div>
      )}
      {activityError && <p className={styles.goalActivityError}>{activityError}</p>}

      <InlineSuggestions suggestions={suggestions} />

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
            <span className={`${styles.commentToggleChevron} ${showComments ? styles.commentToggleChevronOpen : ''}`}>
              <ChevronRightIcon width={14} height={14} />
            </span>
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
        <button
          type="button"
          className={styles.expandError}
          onClick={onExpand}
          disabled={expandLoading}
        >
          这条回应暂时没有加载出来，点击重试。
        </button>
      )}

      <PipelineNotice
        post={post}
        retryingJobId={retryingJobId}
        onRetryFailedJobs={onRetryFailedJobs}
        onRestartStuckJobs={onRestartStuckJobs}
      />
    </article>
  )
})

function PipelineNotice({
  post,
  retryingJobId,
  onRetryFailedJobs,
  onRestartStuckJobs,
}: {
  post: Post
  retryingJobId: number | null
  onRetryFailedJobs?: (jobIds: number[]) => Promise<void>
  onRestartStuckJobs?: (jobIds: number[]) => Promise<void>
}) {
  const status = post.pipeline_status
  const failedJobs = status?.failed_jobs ?? []
  const retryableJobIds = failedJobs.filter((job) => job.retryable).map((job) => job.id)
  const overdue = useOverdue(status?.unfinished_since ?? null)

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
        <p className={styles.pipelineDiagnostics}>请稍后重试。</p>
        <DiagnosticsButton
          context={`处理记录 ${post.post_id}`}
          detail={failedJobs.map((job) => `${job.type}: ${job.error ?? '未知错误'}`).join('\n')}
        />
      </div>
    )
  }

  const isProcessing = status?.state === 'running'
    || status?.state === 'retrying'
    || (!status && post.latest_event_type && post.latest_event_type !== 'pipeline_done')
  if (!isProcessing) return null

  /* 卡住的处理和正常的处理长得一模一样，用户只能干等。等得够久就给一个出口，
     由他自己决定要不要放弃这一次重来——系统不去猜一个还在跑的任务是不是死了。 */
  const stuckJobIds = status?.unfinished_job_ids ?? []
  if (overdue && onRestartStuckJobs && stuckJobIds.length > 0) {
    return (
      <div className={styles.processing}>
        <LoadingDots />
        <span>处理得有点久了</span>
        <button
          className={styles.processingRetry}
          onClick={() => onRestartStuckJobs(stuckJobIds)}
          disabled={retryingJobId !== null}
        >
          重试
        </button>
      </div>
    )
  }

  return (
    <div className={styles.processing}>
      <LoadingDots />
      <span>{status?.state === 'retrying' ? '正在自动重试...' : 'TA 们正在思考...'}</span>
    </div>
  )
}

/** 处理超过这么久还没结果，就该让用户能自己叫停重来。 */
const PIPELINE_OVERDUE_SECONDS = 120

/** 到点之前不重渲染，到点之后不再计时——只为翻一次牌子。 */
function useOverdue(since: number | null): boolean {
  const [overdue, setOverdue] = useState(
    () => since !== null && Date.now() / 1000 - since >= PIPELINE_OVERDUE_SECONDS,
  )
  useEffect(() => {
    if (since === null) {
      setOverdue(false)
      return
    }
    const remainingMs = (since + PIPELINE_OVERDUE_SECONDS) * 1000 - Date.now()
    if (remainingMs <= 0) {
      setOverdue(true)
      return
    }
    setOverdue(false)
    const timer = window.setTimeout(() => setOverdue(true), remainingMs)
    return () => window.clearTimeout(timer)
  }, [since])
  return overdue
}

function pipelineFailureTitle(failedJobs: PipelineJobSummary[]): string {
  return failedJobs.some((job) => job.type === 'generate_post_replies')
    ? '回应暂时没有生成'
    : '这条记录暂时没有处理完'
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
  const [repliedHere, setRepliedHere] = useState(false)
  const replyInputRef = useRef<HTMLTextAreaElement>(null)
  const soulName = comment.soul_name
  const colors = useSoulColors(soulName)
  const trimmed = reply.trim()
  const messages = conversation?.messages ?? []
  const threadMessages = messages.filter((message) => message.seq > 0)
  /* 你在这张卡上追问过之后，这段就一直摊开——折叠是用来省掉读过的历史的，
     不该把你正在进行的对话藏起来。刷新或离开首页后回到默认的折叠状态。 */
  const expanded = variant === 'detail' || repliedHere
  const visibleThreadMessages = expanded
    ? threadMessages
    : threadMessages.slice(-FEED_THREAD_TAIL)
  const threadTotal = Math.max(
    conversation?.threadTotal ?? threadMessages.length,
    threadMessages.length,
  )
  const hiddenThreadCount = expanded ? 0 : threadTotal - visibleThreadMessages.length
  const isThreadTruncated = hiddenThreadCount > 0
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
    setRepliedHere(true)
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
            <span className={styles.soulName} style={{ color: colors.accent }}>{soulName}</span>
            <time
              className={styles.commentTime}
              dateTime={formatDateTimeAttribute(comment.created_at)}
              title={formatAbsoluteTime(comment.created_at)}
            >
              {formatSmartTime(comment.created_at)}
            </time>
            {!rootPending && comment.rerun_at && (
              <RerunMarker at={comment.rerun_at} className={styles.messageMarker} />
            )}
          </div>
          {rootPending ? (
            <div className={styles.threadPending} aria-label={`${soulName} 正在回复`}>
              <LoadingDots />
            </div>
          ) : (
            <div className={styles.commentMain}>
              {comment.content && <p className={styles.commentText}>{comment.content}</p>}
              <ImageGrid attachments={comment.attachments ?? []} />
              <div className={styles.commentFooter}>
                <EvidencePanel
                  metadata={comment.metadata}
                  channel="public_post"
                  messageId={comment.id}
                  compact
                />
                {canRerunRoot && onRerun && (
                  <button
                    type="button"
                    className={`${styles.quietAction} ${styles.actHover}`}
                    onClick={() => onRerun(comment.id)}
                    disabled={rootBusy}
                    title="重跑"
                    aria-label={`重跑 ${soulName} 的回复`}
                  >
                    <RefreshCwIcon />
                    重跑
                  </button>
                )}
                {onReply && latestMessage?.id === comment.id && (
                  <button
                    type="button"
                    className={`${styles.quietAction} ${styles.quietPrimary} ${styles.replyTrigger} ${replyOpen ? styles.quietOn : ''}`}
                    onClick={() => setReplyOpen((open) => !open)}
                    disabled={replyInputDisabled}
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 17l-5-5 5-5M4 12h11a4 4 0 0 1 4 4v1" /></svg>
                    回复
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 一条从头像下方垂下来的细线把整段追问收在一起：缩进 + 竖线才有线程感，
          光靠留白只会读成几条彼此无关的评论。 */}
      {(threadMessages.length > 0 || (onReply && replyOpen)) && (
      <div className={styles.threadBranch}>
        {isThreadTruncated && detailHref && (
          <a className={styles.threadMoreLink} href={detailHref}>
            还有 {hiddenThreadCount} 条 · 在详情页查看完整对话
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
            onReplyTrigger={onReply ? () => setReplyOpen((open) => !open) : undefined}
            replyOpen={replyOpen}
            replyDisabled={replyInputDisabled}
          />
        ))}

        {onReply && replyOpen && (
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
      <p className={styles.pipelineDiagnostics}>请稍后重试。</p>
      <DiagnosticsButton context="生成回应" detail={error} />
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
  onReplyTrigger,
  replyOpen = false,
  replyDisabled = false,
}: {
  message: CommentMessage
  soulName: string
  isLatest: boolean
  busy: boolean
  onDelete?: (commentId: number) => Promise<void>
  onRerun?: (commentId: number) => Promise<void>
  onReplyTrigger?: () => void
  replyOpen?: boolean
  replyDisabled?: boolean
}) {
  const isUser = message.role === 'user'
  const colors = useSoulColors(soulName)
  const isPersisted = message.id > 0
  const isFailedAssistant = message.role === 'assistant' && hasFailedCommentReply(message)
  const isPendingAssistant = message.role === 'assistant' && !isFailedAssistant && !message.content && (message.id < 0 || busy)
  return (
    <div id={`comment-${message.id}`} className={styles.threadRow}>
      <div className={styles.threadHeader}>
        <span className={styles.threadRole} style={isUser ? undefined : { color: colors.accent }}>
          {isUser ? '我' : soulName}
        </span>
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
            <button className={`${styles.quietAction} ${styles.iconOnly} ${styles.actHover}`} onClick={() => onRerun(message.id)} disabled={busy} data-tip="重跑" aria-label={`重跑 ${soulName} 的回复`}>
              <RefreshCwIcon />
            </button>
          )}
          {isPersisted && isUser && onDelete && (
            <button className={`${styles.quietAction} ${styles.quietDanger} ${styles.iconOnly} ${styles.actHover}`} onClick={() => onDelete(message.id)} disabled={busy} data-tip="删除追问" aria-label="删除追问">
              <TrashIcon />
            </button>
          )}
        </div>
      </div>
      {isPendingAssistant ? (
        <div className={styles.threadPending} aria-label={`${soulName} 正在回复`}>
          <LoadingDots />
        </div>
      ) : isFailedAssistant ? (
        <ReplyFailureBubble
          error={failedCommentReplyError(message)}
          onRetry={isPersisted && onRerun ? () => onRerun(message.id) : undefined}
          busy={busy}
        />
      ) : message.content ? (
        <p className={`${styles.threadText} ${isUser ? styles.threadTextUser : styles.threadTextSoul}`}>
          {message.content}
        </p>
      ) : null}
      <ImageGrid attachments={message.attachments ?? []} borderless={isUser} />
      {!isUser && !isFailedAssistant && !isPendingAssistant && (
        <>
          <InlineSuggestions suggestions={parseMessageSuggestions(message.metadata)} />
          <div className={styles.commentFooter}>
            <EvidencePanel metadata={message.metadata} channel="comment" messageId={message.id} compact />
            {isPersisted && isLatest && onReplyTrigger && (
              <button
                type="button"
                className={`${styles.quietAction} ${styles.quietPrimary} ${styles.replyTrigger} ${replyOpen ? styles.quietOn : ''}`}
                onClick={onReplyTrigger}
                disabled={replyDisabled}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 17l-5-5 5-5M4 12h11a4 4 0 0 1 4 4v1" /></svg>
                回复
              </button>
            )}
          </div>
        </>
      )}
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
      <p className={styles.pipelineDiagnostics}>请稍后重试。</p>
      <DiagnosticsButton context="生成回应" detail={error} />
    </div>
  )
}

function hasFailedCommentReply(message: CommentMessage): boolean {
  if (message.role !== 'assistant' || !message.metadata) return false
  try {
    const parsed = JSON.parse(message.metadata) as { status?: unknown }
    return parsed.status === 'failed'
  } catch {
    return false
  }
}

/** 失败回复里存着的原始报错。界面上不显示，只在展开诊断信息时才拿出来。 */
function failedCommentReplyError(message: CommentMessage): string | null {
  if (!message.metadata) return null
  try {
    const parsed = JSON.parse(message.metadata) as { error?: unknown }
    return typeof parsed.error === 'string' ? parsed.error : null
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
