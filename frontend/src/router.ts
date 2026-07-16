export type Route =
  | { kind: 'home'; date?: string }
  | { kind: 'goals' }
  | { kind: 'schedule' }
  | { kind: 'memory' }
  | { kind: 'settings' }
  | { kind: 'chat'; soulName: string }
  | { kind: 'post'; postId: string; highlight?: string }

const DATE_KEY_PATTERN = /^\d{4}-\d{2}-\d{2}$/

export function parseRoute(hash: string): Route {
  const raw = hash.replace(/^#/, '').replace(/^\//, '')
  const [path = '', query = ''] = raw.split('?')
  if (!path || path === 'home') {
    const date = parseRouteQuery(query).get('date') ?? undefined
    return date && DATE_KEY_PATTERN.test(date) ? { kind: 'home', date } : { kind: 'home' }
  }
  if (path === 'goals') return { kind: 'goals' }
  if (path === 'schedule') return { kind: 'schedule' }
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
      return route.date ? `#/?date=${encodeURIComponent(route.date)}` : '#/'
    case 'goals':
      return '#/goals'
    case 'schedule':
      return '#/schedule'
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
