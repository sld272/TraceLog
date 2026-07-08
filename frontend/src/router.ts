export type Route =
  | { kind: 'home' }
  | { kind: 'todos' }
  | { kind: 'goals' }
  | { kind: 'memory' }
  | { kind: 'settings' }
  | { kind: 'chat'; soulName: string }
  | { kind: 'post'; postId: string; highlight?: string }

export function parseRoute(hash: string): Route {
  const raw = hash.replace(/^#/, '').replace(/^\//, '')
  const [path = '', query = ''] = raw.split('?')
  if (!path || path === 'home') return { kind: 'home' }
  if (path === 'todos') return { kind: 'todos' }
  if (path === 'goals') return { kind: 'goals' }
  if (path === 'memory') return { kind: 'memory' }
  if (path === 'settings') return { kind: 'settings' }
  if (path.startsWith('chat/')) {
    const soulName = decodeRouteSegment(path.slice('chat/'.length))
    return soulName ? { kind: 'chat', soulName } : { kind: 'home' }
  }
  if (path.startsWith('post/')) {
    const postId = decodeRouteSegment(path.slice('post/'.length))
    if (!postId) return { kind: 'home' }
    const highlight = parseRouteQuery(query).get('highlight') ?? undefined
    return { kind: 'post', postId, highlight: highlight || undefined }
  }
  return { kind: 'home' }
}

export function formatRoute(route: Route): string {
  switch (route.kind) {
    case 'home':
      return '#/'
    case 'todos':
      return '#/todos'
    case 'goals':
      return '#/goals'
    case 'memory':
      return '#/memory'
    case 'settings':
      return '#/settings'
    case 'chat':
      return `#/chat/${encodeURIComponent(route.soulName)}`
    case 'post': {
      const query = route.highlight ? `?highlight=${encodeURIComponent(route.highlight)}` : ''
      return `#/post/${encodeURIComponent(route.postId)}${query}`
    }
  }
}

function parseRouteQuery(query: string): URLSearchParams {
  try {
    return new URLSearchParams(query)
  } catch {
    return new URLSearchParams()
  }
}

function decodeRouteSegment(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}
