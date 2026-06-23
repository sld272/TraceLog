import { useEffect, useMemo, useRef, useState } from 'react'
import {
  deletePost,
  listPendingSuggestions,
  postIdFromEvidenceRef,
  type Attachment,
  type Post,
  type Suggestion,
} from '@/api/client'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { Notice } from '@/components/Notice'
import { LoadingDots } from '@/components/icons'
import { PostCard } from '@/components/PostCard'
import { usePostDetail } from '@/hooks/usePostDetail'
import { formatRoute } from '@/router'
import styles from './PostDetailPage.module.css'

interface PostDetailPageProps {
  postId: string
  highlight?: string
  modelConfigured?: boolean | null
  onOpenSettings?: () => void
  onPostMutated?: (postId: string, kind: 'updated' | 'deleted') => void
  onTodosChanged?: () => void
}

export function PostDetailPage({
  postId,
  highlight,
  modelConfigured,
  onOpenSettings,
  onPostMutated,
  onTodosChanged,
}: PostDetailPageProps) {
  const detail = usePostDetail(postId, onTodosChanged)
  const [deletingPost, setDeletingPost] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const highlightDoneRef = useRef(false)

  /* Pending suggestions belong to the post itself; fetch them independently
     so the prompt shows under the post regardless of comment state. */
  const latestEventType = detail.post?.latest_event_type
  useEffect(() => {
    let cancelled = false
    listPendingSuggestions()
      .then((all) => {
        if (cancelled) return
        setSuggestions(all.filter((item) => postIdFromEvidenceRef(item.evidence_ref) === postId))
      })
      .catch(() => {
        /* keep prior suggestions on a transient failure */
      })
    return () => {
      cancelled = true
    }
  }, [postId, latestEventType])
  const [confirmDialog, setConfirmDialog] = useState<{
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)
  const modelUnavailable = modelConfigured === false
  const postForCard = useMemo<Post | null>(() => {
    if (!detail.post) return null
    return {
      post_id: detail.post.post_id,
      ts: detail.post.ts,
      content: detail.post.content,
      importance: detail.post.importance,
      comment_count: detail.comments.length,
      latest_event_type: detail.post.latest_event_type ?? null,
      pipeline_status: detail.post.pipeline_status,
      attachments: detail.post.attachments,
    }
  }, [detail.comments.length, detail.post])

  /* The feed may be scrolled deep when the user jumps in; without this the
     browser only clamps the old scroll offset to the new page height. */
  useEffect(() => {
    window.scrollTo({ top: 0 })
  }, [])

  useEffect(() => {
    if (!highlight || detail.loading || detail.notFound || highlightDoneRef.current) return
    const targetId = highlightTargetId(highlight, postId)
    if (!targetId) return
    const target = document.getElementById(targetId) ?? document.getElementById(`post-${postId}`)
    if (!target) return
    highlightDoneRef.current = true
    target.scrollIntoView({ block: 'center', behavior: 'smooth' })
    target.classList.add('post-detail-flash')
    const timer = window.setTimeout(() => target.classList.remove('post-detail-flash'), 2200)
    return () => {
      window.clearTimeout(timer)
      target.classList.remove('post-detail-flash')
    }
  }, [detail.loading, detail.notFound, highlight, postId])

  const goBack = () => {
    if (window.history.length > 1) {
      window.history.back()
      return
    }
    window.location.hash = formatRoute({ kind: 'home' })
  }

  const goHome = () => {
    window.location.hash = formatRoute({ kind: 'home' })
  }

  const handleDeletePost = async () => {
    setConfirmDialog({
      title: '删除记录',
      message: '删除这条记录会同时删除 TA 们的所有回应和追问，关联待办会保留但不再指向来源记录，且不会自动恢复。确定删除吗？',
      onConfirm: async () => {
        setConfirmDialog(null)
        setDeletingPost(true)
        setActionError(null)
        try {
          await deletePost(postId)
          onPostMutated?.(postId, 'deleted')
          onTodosChanged?.()
          goHome()
        } catch (err) {
          setActionError(err instanceof Error ? err.message : '删除失败')
        } finally {
          setDeletingPost(false)
        }
      },
    })
  }

  const handleDeleteComment = async (commentId: number) => {
    setConfirmDialog({
      title: '删除追问',
      message: '删除这条追问会同时删除它之后的这段对话，且不会自动恢复。确定删除吗？',
      onConfirm: async () => {
        setConfirmDialog(null)
        setActionError(null)
        try {
          await detail.deleteComment(commentId)
          onPostMutated?.(postId, 'updated')
        } catch (err) {
          setActionError(err instanceof Error ? err.message : '删除追问失败')
        }
      },
    })
  }

  const handleReply = async (soulName: string, content: string, attachments: Attachment[]) => {
    await detail.reply(soulName, content, attachments)
    onPostMutated?.(postId, 'updated')
  }

  const handleRerunComment = async (commentId: number) => {
    setActionError(null)
    try {
      await detail.rerunComment(commentId)
      onPostMutated?.(postId, 'updated')
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '重新生成失败')
    }
  }

  const handleRetryJobs = async (jobIds: number[]) => {
    await detail.retryJobs(jobIds)
    onPostMutated?.(postId, 'updated')
  }

  if (detail.loading) {
    return (
      <section className={styles.page}>
        <DetailHeader onBack={goBack} />
        <div className={styles.loading}>
          <LoadingDots />
          <span>加载记录中...</span>
        </div>
      </section>
    )
  }

  if (detail.notFound) {
    return (
      <section className={styles.page}>
        <DetailHeader onBack={goBack} />
        <div className={styles.empty}>
          <h2>这条记录不存在或已被删除</h2>
          <button className={styles.primaryButton} onClick={goHome}>返回首页</button>
        </div>
      </section>
    )
  }

  return (
    <section className={styles.page}>
      <DetailHeader onBack={goBack} />
      {modelUnavailable && (
        <Notice
          kind="info"
          actions={onOpenSettings && <button onClick={onOpenSettings}>去设置</button>}
        >
          主模型和 Embedding 尚未配置，配置完成后才能继续追问。
        </Notice>
      )}
      {(actionError ?? detail.error) && (
        <Notice kind="error" onClose={actionError ? () => setActionError(null) : undefined}>
          {actionError ?? detail.error}
        </Notice>
      )}
      {postForCard && (
        <ErrorBoundary
          variant="inline"
          title="此条内容无法显示"
          message="记录仍然保留，可以返回首页或刷新页面后再试。"
        >
          <PostCard
            post={postForCard}
            variant="detail"
            comments={detail.comments}
            suggestions={suggestions}
            commentConversations={detail.conversations}
            busyCommentId={detail.busyCommentId}
            deletingPost={deletingPost}
            retryingJobId={detail.retryingJobId}
            modelConfigured={modelConfigured}
            onReply={handleReply}
            onDeletePost={handleDeletePost}
            onDeleteComment={handleDeleteComment}
            onRerunComment={handleRerunComment}
            onRetryFailedJobs={handleRetryJobs}
          />
        </ErrorBoundary>
      )}
      {confirmDialog && (
        <ConfirmDialog
          isOpen
          title={confirmDialog.title}
          message={confirmDialog.message}
          confirmText="删除"
          cancelText="取消"
          danger
          onConfirm={confirmDialog.onConfirm}
          onCancel={() => setConfirmDialog(null)}
        />
      )}
    </section>
  )
}

function highlightTargetId(docId: string, postId: string): string | null {
  if (docId === `post-${postId}` || docId === `post-vision-${postId}`) {
    return `post-content-${postId}`
  }
  const commentMatch = /^comment-(\d+)$/.exec(docId)
  if (commentMatch?.[1]) return `comment-${commentMatch[1]}`
  return null
}

function DetailHeader({ onBack }: { onBack: () => void }) {
  return (
    <header className={styles.header}>
      <button className={styles.backButton} onClick={onBack}>← 返回</button>
      <div>
        <h1>记录详情</h1>
        <p>完整对话、引用和处理状态</p>
      </div>
    </header>
  )
}
