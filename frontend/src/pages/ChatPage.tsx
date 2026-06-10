import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import {
  type Attachment,
  type ChatMessage,
  type ChatThread,
  getChatThread,
  listChatThreads,
  rerunChatMessage,
  sendChatMessage,
  updateChatMessage,
} from '@/api/client'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { EvidencePanel } from '@/components/EvidencePanel'
import { ImageGrid } from '@/components/ImageGrid'
import { ImageUploader } from '@/components/ImageUploader'
import { LoadingDots, PencilIcon, RefreshCwIcon, SendIcon } from '@/components/icons'
import { LAYOUT } from '@/utils/constants'
import { formatAbsoluteTime, formatDateTimeAttribute, formatSmartTime } from '@/utils/date'
import { getSubmitShortcutTitle } from '@/utils/shortcuts'
import styles from './WorkspacePages.module.css'

interface ChatPageProps {
  soulName: string
  modelConfigured?: boolean | null
  onOpenSettings?: () => void
}

export function ChatPage({ soulName, modelConfigured, onOpenSettings }: ChatPageProps) {
  const [thread, setThread] = useState<ChatThread | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [busyMessageId, setBusyMessageId] = useState<number | null>(null)
  const [editingMessageId, setEditingMessageId] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState('')
  const [failedReplies, setFailedReplies] = useState<Record<number, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)
  const chatInputRef = useRef<HTMLTextAreaElement>(null)
  const submitShortcutTitle = getSubmitShortcutTitle()
  const modelUnavailable = modelConfigured === false
  const chatBusy = sending || busyMessageId !== null

  const fetchThread = useCallback(async () => {
    try {
      setLoading(true)
      const threads = await listChatThreads(soulName)
      const latestThread = threads[0]
      if (!latestThread) {
        setThread(null)
        setMessages([])
        setError(null)
        return
      }
      const detail = await getChatThread(latestThread.id)
      setThread(detail.thread)
      setMessages(detail.messages)
      setEditingMessageId(null)
      setEditDraft('')
      setFailedReplies(failedRepliesFromMessages(detail.messages))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [soulName])

  useEffect(() => {
    setDraft('')
    setAttachments([])
    setEditingMessageId(null)
    setEditDraft('')
    fetchThread()
  }, [fetchThread])

  useEffect(() => {
    const el = chatInputRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, LAYOUT.TEXTAREA_MAX_HEIGHT)}px`
    }
  }, [draft])

  const submitDraft = async () => {
    const body = draft.trim()
    if ((!body && attachments.length === 0) || chatBusy || modelUnavailable) return

    const submittedDraft = draft
    const submittedAttachments = attachments
    const optimisticUserId = -Date.now()
    const optimisticAssistantId = optimisticUserId - 1
    const createdAt = Date.now() / 1000
    const optimisticUser: ChatMessage = {
      id: optimisticUserId,
      thread_id: thread?.id ?? 0,
      role: 'user',
      content: body,
      created_at: createdAt,
      attachments,
    }
    const optimisticAssistant: ChatMessage = {
      id: optimisticAssistantId,
      thread_id: thread?.id ?? 0,
      role: 'assistant',
      content: '',
      created_at: createdAt,
      attachments: [],
    }
    setMessages((prev) => [...prev, optimisticUser, optimisticAssistant])
    setDraft('')
    setAttachments([])
    setSending(true)
    try {
      const response = await sendChatMessage(soulName, body, attachments.map((attachment) => attachment.id))
      setThread(response.thread)
      if (response.result.ok) {
        setMessages(response.messages)
        setFailedReplies(failedRepliesFromMessages(response.messages))
      } else {
        const failedMessages = response.messages.length > 0
          ? response.messages
          : [
              failedAssistantMessage(
                optimisticAssistantId,
                response.thread.id,
                createdAt,
                response.result.error ?? '回复生成失败',
              ),
            ]
        setMessages(failedMessages)
        setFailedReplies(failedRepliesFromMessages(failedMessages))
      }
      setError(null)
    } catch (err) {
      setMessages((prev) =>
        prev.map((message) =>
          message.id === optimisticAssistantId
            ? { ...message, content: '' }
            : message,
        ),
      )
      setDraft(submittedDraft)
      setAttachments(submittedAttachments)
      setFailedReplies({ [optimisticAssistantId]: err instanceof Error ? err.message : '发送失败' })
      setError(null)
    } finally {
      setSending(false)
    }
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    submitDraft()
  }

  const startEditMessage = (message: ChatMessage) => {
    setEditingMessageId(message.id)
    setEditDraft(message.content)
    setError(null)
  }

  const cancelEditMessage = () => {
    setEditingMessageId(null)
    setEditDraft('')
  }

  const saveEditMessage = async (message: ChatMessage) => {
    const body = editDraft.trim()
    if (!body && (message.attachments ?? []).length === 0) return
    const hasLaterMessages = messages.some((item) => item.id > message.id)
    if (hasLaterMessages) {
      setConfirmDialog({
        isOpen: true,
        title: '编辑消息',
        message: '保存后会删除这条消息之后的全部私聊内容，并根据修改后的消息重新生成一条回复。确定继续？',
        onConfirm: async () => {
          setConfirmDialog(null)
          await performSaveEdit(message, body)
        },
      })
      return
    }
    await performSaveEdit(message, body)
  }

  const performSaveEdit = async (message: ChatMessage, body: string) => {
    const previousMessages = messages
    const previousEditDraft = editDraft
    const pendingAssistantId = -Date.now()
    setBusyMessageId(message.id)
    setEditingMessageId(null)
    setEditDraft('')
    setMessages((prev) => withPendingReplyAfterUserEdit(prev, message, body, pendingAssistantId))
    try {
      const response = await updateChatMessage(
        message.id,
        body,
        (message.attachments ?? []).map((attachment) => attachment.id),
      )
      setThread(response.thread)
      if (response.result.ok) {
        setMessages(response.messages)
        setFailedReplies(failedRepliesFromMessages(response.messages))
      } else {
        const failedMessages = response.messages.length > 0
          ? response.messages
          : [
              failedAssistantMessage(
                pendingAssistantId,
                response.thread.id,
                Date.now() / 1000,
                response.result.error ?? '回复生成失败',
              ),
            ]
        setMessages(failedMessages)
        setFailedReplies(failedRepliesFromMessages(failedMessages))
      }
      setError(null)
    } catch (err) {
      setMessages(previousMessages)
      setEditingMessageId(message.id)
      setEditDraft(previousEditDraft)
      setError(err instanceof Error ? err.message : '编辑失败')
    } finally {
      setBusyMessageId(null)
    }
  }

  const rerunMessage = async (message: ChatMessage) => {
    const hasLaterMessages = messages.some((item) => item.id > message.id)
    if (hasLaterMessages) {
      setConfirmDialog({
        isOpen: true,
        title: '重跑消息',
        message: '重跑后会移除这条回复之后的私聊内容，确定继续？',
        onConfirm: async () => {
          setConfirmDialog(null)
          await performRerun(message)
        },
      })
      return
    }
    await performRerun(message)
  }

  const performRerun = async (message: ChatMessage) => {
    const previousMessages = messages
    const previousEditingMessageId = editingMessageId
    const previousEditDraft = editDraft
    setBusyMessageId(message.id)
    setEditingMessageId(null)
    setEditDraft('')
    setMessages((prev) => withPendingReplyForAssistantRerun(prev, message))
    try {
      const response = await rerunChatMessage(message.id)
      setThread(response.thread)
      setMessages(response.messages)
      setEditingMessageId(null)
      setEditDraft('')
      setFailedReplies({})
      setError(null)
    } catch (err) {
      setMessages(previousMessages)
      setEditingMessageId(previousEditingMessageId)
      setEditDraft(previousEditDraft)
      setError(err instanceof Error ? err.message : '重跑失败')
    } finally {
      setBusyMessageId(null)
    }
  }

  const retryFailedReply = async (message: ChatMessage) => {
    const userMessage = previousUserMessage(messages, message)
    if (!userMessage) return
    const previousMessages = messages
    const previousFailedReplies = failedReplies
    setBusyMessageId(message.id)
    setFailedReplies((prev) => {
      const next = { ...prev }
      delete next[message.id]
      return next
    })
    setMessages((prev) =>
      prev.map((item) =>
        item.id === message.id
          ? { ...item, content: '' }
          : item,
      ),
    )
    try {
      const attachmentIds = (userMessage.attachments ?? []).map((attachment) => attachment.id)
      const response = userMessage.id > 0
        ? await updateChatMessage(userMessage.id, userMessage.content, attachmentIds)
        : await sendChatMessage(soulName, userMessage.content, attachmentIds)
      setThread(response.thread)
      if (response.result.ok) {
        setMessages(response.messages)
        setFailedReplies(failedRepliesFromMessages(response.messages))
      } else {
        const failedMessages = response.messages.length > 0
          ? response.messages
          : [
              failedAssistantMessage(
                message.id,
                response.thread.id,
                Date.now() / 1000,
                response.result.error ?? '回复生成失败',
              ),
            ]
        setMessages(failedMessages)
        setFailedReplies(failedRepliesFromMessages(failedMessages))
      }
      setError(null)
    } catch (err) {
      setMessages(previousMessages)
      setFailedReplies(previousFailedReplies)
      setError(null)
    } finally {
      setBusyMessageId(null)
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.chatShell}>
        <header className={styles.header}>
          <div className={styles.titleGroup}>
            <h1 className={styles.title}>{soulName}</h1>
            <p className={styles.subtitle}>{thread?.title ?? '私聊'}</p>
          </div>
          <button className={styles.ghostButton} onClick={fetchThread} disabled={loading || chatBusy}>
            刷新
          </button>
        </header>

        {modelUnavailable && (
          <div className={styles.notice}>
            <div className={styles.noticeRow}>
              <span>主模型和 Embedding 尚未配置，配置完成后才能发送私聊消息。</span>
              {onOpenSettings && (
                <button className={styles.ghostButton} onClick={onOpenSettings}>
                  去设置
                </button>
              )}
            </div>
          </div>
        )}

        {error && <div className={styles.notice}>{error}</div>}

        <div className={styles.messages}>
          {loading ? (
            <div className={styles.empty}>加载中...</div>
          ) : messages.length === 0 ? (
            <div className={styles.empty}>还没有消息</div>
          ) : (
            messages.map((message) => (
              <MessageBubble
                key={message.id}
                soulName={soulName}
                message={message}
                busy={busyMessageId === message.id}
                failure={failedReplies[message.id] ?? null}
                editDraft={editingMessageId === message.id ? editDraft : null}
                onStartEdit={startEditMessage}
                onChangeEditDraft={setEditDraft}
                onCancelEdit={cancelEditMessage}
                onSaveEdit={saveEditMessage}
                onRerun={(target) => {
                  if (target.id < 0) {
                    retryFailedReply(target)
                    return
                  }
                  rerunMessage(target)
                }}
              />
            ))
          )}
        </div>

        <form className={styles.chatForm} onSubmit={handleSubmit}>
          <div className={styles.chatInputGroup}>
            <textarea
              ref={chatInputRef}
              className={styles.chatInput}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault()
                  submitDraft()
                }
              }}
              placeholder={`和 ${soulName} 说点什么...`}
              disabled={chatBusy || modelUnavailable}
              rows={2}
              aria-label="私聊消息"
            />
            <ImageUploader
              attachments={attachments}
              disabled={chatBusy || modelUnavailable}
              onChange={setAttachments}
              showControls={false}
            />
          </div>
          <div className={styles.chatFooter}>
            {(draft.length > 0 || attachments.length > 0) && (
              <span className={styles.chatHint}>
                {draft.length} 字{attachments.length > 0 ? ` · ${attachments.length} 图` : ''}
              </span>
            )}
            <div className={styles.chatActions}>
              <ImageUploader
                attachments={attachments}
                disabled={chatBusy || modelUnavailable}
                onChange={setAttachments}
                showPreview={false}
              />
              <span className={styles.buttonTooltipWrap} title={submitShortcutTitle}>
                <button
                  className={styles.chatSubmitButton}
                  disabled={(!draft.trim() && attachments.length === 0) || chatBusy || modelUnavailable}
                  aria-label="发送"
                >
                  {chatBusy ? <LoadingDots /> : <SendIcon />}
                </button>
              </span>
            </div>
          </div>
        </form>
      </div>

      {confirmDialog && (
        <ConfirmDialog
          isOpen={confirmDialog.isOpen}
          title={confirmDialog.title}
          message={confirmDialog.message}
          confirmText="继续"
          cancelText="取消"
          danger
          onConfirm={confirmDialog.onConfirm}
          onCancel={() => setConfirmDialog(null)}
        />
      )}
    </div>
  )
}

function withPendingReplyAfterUserEdit(
  messages: ChatMessage[],
  editedMessage: ChatMessage,
  content: string,
  pendingAssistantId: number,
): ChatMessage[] {
  const createdAt = Date.now() / 1000
  return [
    ...messages
      .filter((message) => message.id < editedMessage.id)
      .map((message) => ({ ...message })),
    {
      ...editedMessage,
      content,
      edited_at: createdAt,
    },
    {
      id: pendingAssistantId,
      thread_id: editedMessage.thread_id,
      role: 'assistant',
      content: '',
      created_at: createdAt,
      attachments: [],
    },
  ]
}

function failedAssistantMessage(id: number, threadId: number, createdAt: number, error?: string): ChatMessage {
  return {
    id,
    thread_id: threadId,
    role: 'assistant',
    content: '',
    created_at: createdAt,
    metadata: error ? JSON.stringify({ status: 'failed', error }) : null,
    attachments: [],
  }
}

function withPendingReplyForAssistantRerun(messages: ChatMessage[], rerunMessage: ChatMessage): ChatMessage[] {
  const targetIndex = messages.findIndex((message) => message.id === rerunMessage.id)
  if (targetIndex < 0) return messages
  const targetMessage = messages[targetIndex]
  if (!targetMessage) return messages
  const createdAt = Date.now() / 1000
  return [
    ...messages.slice(0, targetIndex).map((message) => ({ ...message })),
    {
      ...targetMessage,
      content: '',
      rerun_at: createdAt,
      attachments: [],
    },
  ]
}

function failedRepliesFromMessages(messages: ChatMessage[]): Record<number, string> {
  const failures: Record<number, string> = {}
  for (const message of messages) {
    const error = failedReplyError(message)
    if (error) failures[message.id] = error
  }
  return failures
}

function failedReplyError(message: ChatMessage): string | null {
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

function MessageBubble({
  soulName,
  message,
  busy,
  failure,
  editDraft,
  onStartEdit,
  onChangeEditDraft,
  onCancelEdit,
  onSaveEdit,
  onRerun,
}: {
  soulName: string
  message: ChatMessage
  busy: boolean
  failure: string | null
  editDraft: string | null
  onStartEdit: (message: ChatMessage) => void
  onChangeEditDraft: (value: string) => void
  onCancelEdit: () => void
  onSaveEdit: (message: ChatMessage) => void
  onRerun: (message: ChatMessage) => void
}) {
  const isUser = message.role === 'user'
  const isPersisted = message.id > 0
  const isFailedAssistant = message.role === 'assistant' && Boolean(failure)
  const isPendingAssistant = message.role === 'assistant' && !failure && !message.content && (message.id < 0 || busy)
  const editInputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const el = editInputRef.current
    if (el && editDraft !== null) {
      el.style.height = 'auto'
      const newHeight = Math.min(el.scrollHeight, 200)
      el.style.height = `${newHeight}px`
      // Only show scrollbar when content exceeds max height
      el.style.overflowY = el.scrollHeight > 200 ? 'auto' : 'hidden'
    }
  }, [editDraft])

  return (
    <article className={`${styles.message} ${isUser ? styles.messageUser : styles.messageAssistant}`}>
      <div className={styles.messageHeader}>
        <span className={styles.messageRole}>{isUser ? '我' : soulName}</span>
        <time
          className={styles.messageTime}
          dateTime={formatDateTimeAttribute(message.created_at)}
          title={formatAbsoluteTime(message.created_at)}
        >
          {formatSmartTime(message.created_at)}
        </time>
        {isPersisted && editDraft === null && !isFailedAssistant && (
          <div className={styles.messageActions}>
            {isUser ? (
              <button className={styles.messageAction} onClick={() => onStartEdit(message)} disabled={busy} title="编辑" aria-label="编辑私聊消息">
                <PencilIcon />
              </button>
            ) : (
              <button className={styles.messageAction} onClick={() => onRerun(message)} disabled={busy} title="重跑" aria-label={`重跑 ${soulName} 的回复`}>
                <RefreshCwIcon />
              </button>
            )}
          </div>
        )}
      </div>
      {isFailedAssistant ? (
        <ReplyFailure
          error={failure}
          busy={busy}
          onRetry={() => onRerun(message)}
        />
      ) : isPendingAssistant ? (
        <div className={styles.messagePending} aria-label={`${soulName} 正在回复`}>
          <LoadingDots />
        </div>
      ) : editDraft === null ? (
        <p className={styles.messageText}>{message.content}</p>
      ) : (
        <textarea
          ref={editInputRef}
          className={styles.messageEditInput}
          value={editDraft}
          onChange={(event) => onChangeEditDraft(event.target.value)}
          disabled={busy}
          rows={1}
          aria-label="编辑私聊消息"
        />
      )}
      <ImageGrid attachments={message.attachments ?? []} borderless={isUser} />
      {!isUser && !isFailedAssistant && !isPendingAssistant && editDraft === null && (
        <EvidencePanel metadata={message.metadata} channel="chat" messageId={message.id} />
      )}
      {editDraft !== null && (
        <div className={styles.messageMetaRow}>
          <div className={styles.messageActions}>
            <button className={styles.messageTextAction} onClick={onCancelEdit} disabled={busy}>
              取消
            </button>
            <button className={styles.messageTextAction} onClick={() => onSaveEdit(message)} disabled={busy}>
              保存
            </button>
          </div>
        </div>
      )}
    </article>
  )
}

function ReplyFailure({
  error,
  busy,
  onRetry,
}: {
  error: string | null
  busy: boolean
  onRetry: () => void
}) {
  return (
    <div className={styles.replyFailure}>
      <div className={styles.replyFailureMain}>
        <strong>回复生成失败</strong>
        <div className={styles.replyFailureActions}>
          <button className={styles.messageTextAction} onClick={onRetry} disabled={busy}>
            重试
          </button>
        </div>
      </div>
      <details className={styles.replyFailureDetails}>
        <summary>诊断信息</summary>
        <p>{error || '未知错误'}</p>
      </details>
    </div>
  )
}

function previousUserMessage(messages: ChatMessage[], assistantMessage: ChatMessage): ChatMessage | null {
  const index = messages.findIndex((message) => message.id === assistantMessage.id)
  if (index <= 0) return null
  for (let i = index - 1; i >= 0; i -= 1) {
    const candidate = messages[i]
    if (candidate?.role === 'user') return candidate
  }
  return null
}
