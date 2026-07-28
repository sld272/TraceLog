import {
  type Attachment,
  type Comment,
  type CommentConversation,
  type CommentMessage,
  type PostConversationThread,
  type PostEvent,
} from '@/api/client'
import { type CommentConversationState } from '@/components/PostCard'

/** 帖子详情自带的整段对话 → 按 SOUL 归档的状态。
 *
 * `previous` 里正在发送的那条会原样留着：服务端快照是发送前拍的，直接盖上去会
 * 让刚打出去的追问先消失、等回复到了再闪回来。 */
export function conversationsFromThreads(
  threads: PostConversationThread[] | undefined,
  previous?: Record<string, CommentConversationState>,
): Record<string, CommentConversationState> {
  const next: Record<string, CommentConversationState> = Object.fromEntries(
    (threads ?? []).map((thread) => [
      thread.conversation.soul_name,
      toConversationState(thread.conversation, thread.messages, thread.thread_total),
    ]),
  )
  for (const [soulName, state] of Object.entries(previous ?? {})) {
    if (state.sending) next[soulName] = state
  }
  return next
}

export function toConversationState(
  conversation: CommentConversation,
  messages: CommentMessage[],
  threadTotal?: number,
): CommentConversationState {
  return {
    conversation,
    messages,
    threadTotal: threadTotal ?? messages.filter((message) => message.seq > 0).length,
    sending: false,
    error: null,
  }
}

export function failedCommentState(
  conversation: CommentConversation,
  messages: CommentMessage[],
  error: string | null,
): CommentConversationState {
  return {
    conversation,
    messages,
    sending: false,
    error: error && messages.length === 0 ? error : null,
  }
}

export function buildSendingCommentState(
  current: CommentConversationState | undefined,
  postId: string,
  soulName: string,
  content: string,
  attachments: Attachment[],
  optimisticUserId: number,
  optimisticAssistantId: number,
): CommentConversationState {
  const messages = current?.messages ?? []
  const nextSeq = Math.max(0, ...messages.map((message) => message.seq)) + 1
  const createdAt = Date.now() / 1000
  const optimisticUserMessage: CommentMessage = {
    id: optimisticUserId,
    post_id: postId,
    soul_name: soulName,
    role: 'user',
    content,
    seq: nextSeq,
    created_at: createdAt,
    attachments,
  }
  const optimisticAssistantMessage: CommentMessage = {
    id: optimisticAssistantId,
    post_id: postId,
    soul_name: soulName,
    role: 'assistant',
    content: '',
    seq: nextSeq + 1,
    created_at: createdAt,
    attachments: [],
  }
  return {
    ...(current ?? { messages: [] }),
    messages: [...messages, optimisticUserMessage, optimisticAssistantMessage],
    threadTotal: (current?.threadTotal ?? 0) + 2,
    sending: true,
    error: null,
  }
}

export function withPendingCommentRerun(
  conversationsByPost: Record<string, Record<string, CommentConversationState>>,
  postId: string,
  commentId: number,
  rootComments: Comment[],
): Record<string, Record<string, CommentConversationState>> {
  const postConversations = conversationsByPost[postId] ?? {}
  const createdAt = Date.now() / 1000
  let foundMessage = false
  const nextPostConversations = Object.fromEntries(
    Object.entries(postConversations).map(([soulName, conversation]) => {
      const targetIndex = conversation.messages.findIndex((message) => message.id === commentId)
      if (targetIndex < 0) return [soulName, conversation]
      foundMessage = true
      return [soulName, withPendingCommentMessage(conversation, targetIndex, createdAt)]
    }),
  )

  const rootComment = rootComments.find((comment) => comment.id === commentId && comment.role === 'assistant')
  if (!foundMessage && rootComment) {
    const current = nextPostConversations[rootComment.soul_name] ?? { messages: [] }
    nextPostConversations[rootComment.soul_name] = {
      ...current,
      messages: [],
      sending: true,
      error: null,
    }
  }

  if (!foundMessage && !rootComment) return conversationsByPost

  return {
    ...conversationsByPost,
    [postId]: nextPostConversations,
  }
}

export function shouldRefreshPostDetail(event: PostEvent): boolean {
  return [
    'reply_succeeded',
    'reply_failed',
    'pipeline_done',
  ].includes(event.event_type)
}

export function latestEventId(events: PostEvent[]): number {
  return events.reduce((latest, event) => Math.max(latest, event.id), 0)
}

function withPendingCommentMessage(
  conversation: CommentConversationState,
  targetIndex: number,
  rerunAt: number,
): CommentConversationState {
  const targetMessage = conversation.messages[targetIndex]
  if (!targetMessage) return conversation
  return {
    ...conversation,
    messages: [
      ...conversation.messages.slice(0, targetIndex).map((message) => ({ ...message })),
      {
        ...targetMessage,
        content: '',
        metadata: null,
        rerun_at: rerunAt,
        attachments: [],
      },
    ],
    sending: true,
    error: null,
  }
}
