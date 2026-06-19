import { FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  type Attachment,
  type ChatMessage,
  type ChatThread,
  getChatThread,
  listChatThreads,
  parseMessageSuggestions,
  rerunChatMessage,
  sendChatMessage,
  streamChatMessages,
  updateChatMessage,
} from '@/api/client'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { EvidencePanel } from '@/components/EvidencePanel'
import { ImageGrid } from '@/components/ImageGrid'
import { ImageUploader } from '@/components/ImageUploader'
import { InlineSuggestions } from '@/components/InlineSuggestions'
import { Notice } from '@/components/Notice'
import { LoadingDots, PencilIcon, RefreshCwIcon, SendIcon } from '@/components/icons'
import { LAYOUT } from '@/utils/constants'
import { formatAbsoluteTime, formatDateTimeAttribute, formatSmartTime } from '@/utils/date'
import styles from './WorkspacePages.module.css'

const CHAT_HISTORY_PAGE_SIZE = 50

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
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [hasMoreHistory, setHasMoreHistory] = useState(false)
  const [editingMessageId, setEditingMessageId] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState('')
  const [failedReplies, setFailedReplies] = useState<Record<number, string>>({})
  const [retryErrors, setRetryErrors] = useState<Record<number, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)
  const chatInputRef = useRef<HTMLTextAreaElement>(null)
  const messagesRef = useRef<HTMLDivElement>(null)
  const historySentinelRef = useRef<HTMLDivElement>(null)
  const chatStreamUnsubscribeRef = useRef<(() => void) | null>(null)
  const preserveScrollHeightRef = useRef<number | null>(null)
  const stickToBottomRef = useRef(true)
  const forceScrollRef = useRef(false)
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
        setHasMoreHistory(false)
        setError(null)
        return
      }
      const detail = await getChatThread(latestThread.id, CHAT_HISTORY_PAGE_SIZE)
      forceScrollRef.current = true
      setThread(detail.thread)
      setMessages(detail.messages)
      setHasMoreHistory(detail.messages.length >= CHAT_HISTORY_PAGE_SIZE)
      setEditingMessageId(null)
      setEditDraft('')
      setFailedReplies(failedRepliesFromMessages(detail.messages))
      setRetryErrors({})
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [soulName])

  const loadEarlier = useCallback(async () => {
    if (!thread?.id || loadingEarlier || !hasMoreHistory) return
    const oldestMessageId = minRealMessageId(messages)
    if (oldestMessageId === null) {
      setHasMoreHistory(false)
      return
    }
    const scroller = messagesRef.current
    preserveScrollHeightRef.current = scroller?.scrollHeight ?? null
    setLoadingEarlier(true)
    try {
      const detail = await getChatThread(thread.id, CHAT_HISTORY_PAGE_SIZE, oldestMessageId)
      setThread(detail.thread)
      setMessages((prev) => mergeMessages(prev, detail.messages))
      setHasMoreHistory(detail.messages.length >= CHAT_HISTORY_PAGE_SIZE)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载更早消息失败')
      preserveScrollHeightRef.current = null
    } finally {
      setLoadingEarlier(false)
    }
  }, [hasMoreHistory, loadingEarlier, messages, thread?.id])

  useEffect(() => {
    setDraft('')
    setAttachments([])
    setEditingMessageId(null)
    setEditDraft('')
    fetchThread()
  }, [fetchThread])

  useEffect(() => {
    setFailedReplies(failedRepliesFromMessages(messages))
  }, [messages])

  useEffect(() => {
    if (!thread?.id) return
    const afterId = maxRealMessageId(messages)
    const unsubscribe = streamChatMessages(
      thread.id,
      (message) => {
        setMessages((prev) => mergeMessages(prev, [message]))
      },
      { afterId },
    )
    chatStreamUnsubscribeRef.current = unsubscribe
    return () => {
      unsubscribe()
      if (chatStreamUnsubscribeRef.current === unsubscribe) {
        chatStreamUnsubscribeRef.current = null
      }
    }
  }, [thread?.id])

  useEffect(() => {
    if (loading || !hasMoreHistory) return
    const root = messagesRef.current
    const sentinel = historySentinelRef.current
    if (!root || !sentinel) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void loadEarlier()
    }, { root, threshold: 0.01 })
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [hasMoreHistory, loadEarlier, loading, messages.length])

  useEffect(() => {
    const el = chatInputRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, LAYOUT.TEXTAREA_MAX_HEIGHT)}px`
    }
  }, [draft])

  const handleMessagesScroll = () => {
    const el = messagesRef.current
    if (!el) return
    stickToBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight <= LAYOUT.CHAT_STICK_BOTTOM_THRESHOLD
  }

  // 初次加载、自己发送后强制滚到底;其余更新(新回复、重跑)仅当用户本就在底部附近时跟随
  useLayoutEffect(() => {
    const el = messagesRef.current
    if (!el) return
    if (preserveScrollHeightRef.current !== null) {
      const previousHeight = preserveScrollHeightRef.current
      preserveScrollHeightRef.current = null
      el.scrollTop += el.scrollHeight - previousHeight
      return
    }
    if (forceScrollRef.current || stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight
      forceScrollRef.current = false
      stickToBottomRef.current = true
    }
  }, [loading, messages])

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
    forceScrollRef.current = true
    setDraft('')
    setAttachments([])
    setSending(true)
    try {
      const response = await sendChatMessage(soulName, body, attachments.map((attachment) => attachment.id))
      setThread(response.thread)
      if (response.result.ok) {
        setMessages((prev) => mergeMessageWindow(prev, response.messages))
        setRetryErrors({})
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
        setMessages((prev) => mergeMessageWindow(prev, failedMessages))
        setRetryErrors({})
      }
      setError(null)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : '发送失败'
      setMessages((prev) =>
        prev.map((message) =>
          message.id === optimisticAssistantId
            ? {
                ...message,
                content: '',
                metadata: JSON.stringify({ status: 'failed', error: errorMessage }),
              }
            : message,
        ),
      )
      setDraft((current) => current ? current : submittedDraft)
      setAttachments((current) => current.length > 0 ? current : submittedAttachments)
      setRetryErrors({})
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
        setMessages((prev) => mergeMessageWindow(prev, response.messages))
        setRetryErrors({})
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
        setMessages((prev) => mergeMessageWindow(prev, failedMessages))
        setRetryErrors({})
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
      setMessages((prev) => mergeMessageWindow(prev, response.messages))
      setEditingMessageId(null)
      setEditDraft('')
      setRetryErrors({})
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
    setBusyMessageId(message.id)
    setRetryErrors((prev) => {
      const next = { ...prev }
      delete next[message.id]
      return next
    })
    setMessages((prev) =>
      prev.map((item) =>
        item.id === message.id
          ? { ...item, content: '', metadata: null }
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
        setMessages((prev) => mergeMessageWindow(prev, response.messages))
        setRetryErrors({})
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
        setMessages((prev) => mergeMessageWindow(prev, failedMessages))
        setRetryErrors({})
      }
      setError(null)
    } catch (err) {
      const retryError = err instanceof Error ? err.message : '重试失败'
      setMessages(previousMessages)
      setRetryErrors((prev) => ({ ...prev, [message.id]: retryError }))
      setError(retryError)
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
            {thread?.title && <p className={styles.subtitle}>{thread.title}</p>}
          </div>
          <button className={styles.ghostButton} onClick={fetchThread} disabled={loading || chatBusy}>
            刷新
          </button>
        </header>

        {modelUnavailable && (
          <Notice
            kind="info"
            actions={onOpenSettings && (
              <button className={styles.ghostButton} onClick={onOpenSettings}>
                去设置
              </button>
            )}
          >
            主模型和 Embedding 尚未配置，配置完成后才能发送私聊消息。
          </Notice>
        )}

        {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}

        <div className={styles.messages} ref={messagesRef} onScroll={handleMessagesScroll}>
          {loading ? (
            <div className={styles.empty}>加载中...</div>
          ) : messages.length === 0 ? (
            <div className={styles.empty}>还没有消息。和 {soulName} 说点什么吧，TA 会记得你们的对话。</div>
          ) : (
            <>
              <div className={styles.historySentinel} ref={historySentinelRef}>
                {loadingEarlier ? (
                  <span>加载更早的消息...</span>
                ) : hasMoreHistory ? (
                  <button type="button" onClick={loadEarlier}>
                    加载更早的消息
                  </button>
                ) : (
                  <span>已经是最早的消息</span>
                )}
              </div>
              {messages.map((message) => (
                <MessageBubble
                  key={message.id}
                  soulName={soulName}
                  message={message}
                  busy={busyMessageId === message.id}
                  failure={failedReplies[message.id] ?? null}
                  retryError={retryErrors[message.id] ?? null}
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
              ))}
            </>
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
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault()
                  submitDraft()
                }
              }}
              placeholder={`和 ${soulName} 说点什么...`}
              disabled={modelUnavailable}
              rows={2}
              aria-label="私聊消息"
            />
            <ImageUploader
              attachments={attachments}
              disabled={modelUnavailable}
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
                disabled={modelUnavailable}
                onChange={setAttachments}
                showPreview={false}
              />
              <span className={`${styles.buttonTooltipWrap} kbdTip`}>
                <button
                  className={styles.chatSubmitButton}
                  disabled={(!draft.trim() && attachments.length === 0) || chatBusy || modelUnavailable}
                  aria-label="发送"
                >
                  {chatBusy ? <LoadingDots /> : <SendIcon />}
                </button>
                <span className="kbdTipBubble" role="tooltip">
                  发送 <span className="kbdTipKey">Enter</span>
                </span>
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

function mergeMessageWindow(current: ChatMessage[], windowMessages: ChatMessage[]): ChatMessage[] {
  if (windowMessages.length === 0) return current
  const realIds = windowMessages
    .map((message) => message.id)
    .filter((id) => id > 0)
  if (realIds.length === 0) return mergeMessages(current, windowMessages)
  const oldestWindowId = Math.min(...realIds)
  return mergeMessages(
    current.filter((message) => message.id > 0 && message.id < oldestWindowId),
    windowMessages,
  )
}

function mergeMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  if (incoming.length === 0) return current
  const incomingIds = new Set(incoming.map((message) => message.id))
  const incomingRealMessages = incoming.filter((message) => message.id > 0)
  const shouldDropOptimisticAssistant = incomingRealMessages.some((message) => message.role === 'assistant')

  const keptCurrent = current.filter((message) => {
    if (incomingIds.has(message.id)) return false
    if (message.id > 0) return true
    if (shouldDropOptimisticAssistant && message.role === 'assistant') return false
    return !incomingRealMessages.some((incomingMessage) => isOptimisticMatch(message, incomingMessage))
  })

  return [...keptCurrent, ...incoming]
    .sort((a, b) => {
      if (a.id > 0 && b.id > 0 && a.id !== b.id) return a.id - b.id
      if (a.created_at !== b.created_at) return a.created_at - b.created_at
      if (a.id !== b.id) return a.id - b.id
      return a.created_at - b.created_at
    })
}

function isOptimisticMatch(optimistic: ChatMessage, persisted: ChatMessage): boolean {
  if (optimistic.id > 0 || persisted.id <= 0) return false
  if (optimistic.role !== persisted.role) return false
  if (optimistic.content.trim() !== persisted.content.trim()) return false
  return attachmentIds(optimistic).join('\0') === attachmentIds(persisted).join('\0')
}

function attachmentIds(message: ChatMessage): string[] {
  return (message.attachments ?? [])
    .map((attachment) => attachment.id)
    .sort()
}

function maxRealMessageId(messages: ChatMessage[]): number {
  return messages.reduce((maxId, message) => message.id > 0 ? Math.max(maxId, message.id) : maxId, 0)
}

function minRealMessageId(messages: ChatMessage[]): number | null {
  const ids = messages
    .map((message) => message.id)
    .filter((id) => id > 0)
  return ids.length > 0 ? Math.min(...ids) : null
}

function MessageBubble({
  soulName,
  message,
  busy,
  failure,
  retryError,
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
  retryError: string | null
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
        {!isPendingAssistant && message.rerun_at ? (
          <span className={styles.messageMarker} title={formatAbsoluteTime(message.rerun_at)}>
            已重新生成 · {formatSmartTime(message.rerun_at)}
          </span>
        ) : isUser && message.edited_at ? (
          <span className={styles.messageMarker} title={formatAbsoluteTime(message.edited_at)}>
            已编辑
          </span>
        ) : null}
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
          retryError={retryError}
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
        <>
          <InlineSuggestions suggestions={parseMessageSuggestions(message.metadata)} />
          <EvidencePanel metadata={message.metadata} channel="chat" messageId={message.id} />
        </>
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
  retryError,
  busy,
  onRetry,
}: {
  error: string | null
  retryError: string | null
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
      {retryError && (
        <p className={styles.replyRetryError}>
          重试也失败了：{retryError}
        </p>
      )}
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
