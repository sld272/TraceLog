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
import { ImageGrid } from '@/components/ImageGrid'
import { ImageUploader } from '@/components/ImageUploader'
import { LoadingDots, PencilIcon, RefreshCwIcon, SendIcon } from '@/components/icons'
import { LAYOUT } from '@/utils/constants'
import { getSubmitShortcutTitle } from '@/utils/shortcuts'
import styles from './WorkspacePages.module.css'

interface ChatPageProps {
  soulName: string
}

export function ChatPage({ soulName }: ChatPageProps) {
  const [thread, setThread] = useState<ChatThread | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [busyMessageId, setBusyMessageId] = useState<number | null>(null)
  const [editingMessageId, setEditingMessageId] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)
  const chatInputRef = useRef<HTMLTextAreaElement>(null)
  const submitShortcutTitle = getSubmitShortcutTitle()

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
    if ((!body && attachments.length === 0) || sending) return

    const optimistic: ChatMessage = {
      id: Date.now() * -1,
      thread_id: thread?.id ?? 0,
      role: 'user',
      content: body,
      created_at: Date.now() / 1000,
      attachments,
    }
    setMessages((prev) => [...prev, optimistic])
    setDraft('')
    setAttachments([])
    setSending(true)
    try {
      const response = await sendChatMessage(soulName, body, attachments.map((attachment) => attachment.id))
      setThread(response.thread)
      setMessages(response.messages)
      setError(response.result.ok ? null : response.result.error ?? '回复失败')
    } catch (err) {
      setMessages((prev) => prev.filter((message) => message.id !== optimistic.id))
      setError(err instanceof Error ? err.message : '发送失败')
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
        message: '保存后会移除这条消息之后的私聊内容，确定继续？',
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
    setBusyMessageId(message.id)
    try {
      const response = await updateChatMessage(
        message.id,
        body,
        (message.attachments ?? []).map((attachment) => attachment.id),
      )
      setThread(response.thread)
      setMessages(response.messages)
      setEditingMessageId(null)
      setEditDraft('')
      setError(response.result.ok ? null : response.result.error ?? '回复失败')
    } catch (err) {
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
    setBusyMessageId(message.id)
    try {
      const response = await rerunChatMessage(message.id)
      setThread(response.thread)
      setMessages(response.messages)
      setEditingMessageId(null)
      setEditDraft('')
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重跑失败')
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
          <button className={styles.ghostButton} onClick={fetchThread} disabled={loading || sending}>
            刷新
          </button>
        </header>

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
                editDraft={editingMessageId === message.id ? editDraft : null}
                onStartEdit={startEditMessage}
                onChangeEditDraft={setEditDraft}
                onCancelEdit={cancelEditMessage}
                onSaveEdit={saveEditMessage}
                onRerun={rerunMessage}
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
              disabled={sending}
              rows={2}
              aria-label="私聊消息"
            />
            <ImageUploader
              attachments={attachments}
              disabled={sending}
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
                disabled={sending}
                onChange={setAttachments}
                showPreview={false}
              />
              <span className={styles.buttonTooltipWrap} title={submitShortcutTitle}>
                <button
                  className={styles.chatSubmitButton}
                  disabled={(!draft.trim() && attachments.length === 0) || sending}
                  aria-label="发送"
                >
                  {sending ? <LoadingDots /> : <SendIcon />}
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

function MessageBubble({
  soulName,
  message,
  busy,
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
  editDraft: string | null
  onStartEdit: (message: ChatMessage) => void
  onChangeEditDraft: (value: string) => void
  onCancelEdit: () => void
  onSaveEdit: (message: ChatMessage) => void
  onRerun: (message: ChatMessage) => void
}) {
  const isUser = message.role === 'user'
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
        {editDraft === null && (
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
      {editDraft === null ? (
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
      <ImageGrid attachments={message.attachments ?? []} />
      <div className={styles.messageMetaRow}>
        {editDraft !== null && (
          <div className={styles.messageActions}>
            <button className={styles.messageTextAction} onClick={onCancelEdit} disabled={busy}>
              取消
            </button>
            <button className={styles.messageTextAction} onClick={() => onSaveEdit(message)} disabled={busy}>
              保存
            </button>
          </div>
        )}
      </div>
    </article>
  )
}
