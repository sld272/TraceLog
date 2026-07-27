import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type MemoryStatus,
  type Soul,
  type UnreadThread,
  getChatUnread,
  getMemoryStatus,
  getModelSettings,
  listGoals,
  listSouls,
} from '@/api/client'
import {
  loadNotifiedMessageIds,
  saveNotifiedMessageIds,
  showDesktopNotification,
} from '@/utils/notifications'
import { AppShell } from '@/components/AppShell'
import { LeftNav } from '@/components/LeftNav'
import { SoulColorProvider } from '@/components/SoulColorContext'
import { RightPanel } from '@/components/RightPanel'
import { ChatPage } from '@/pages/ChatPage'
import { ChatsPage } from '@/pages/ChatsPage'
import { GoalsPage } from '@/pages/GoalsPage'
import { MemoryWorkbench } from '@/pages/MemoryWorkbench'
import { PostDetailPage } from '@/pages/PostDetailPage'
import { SchedulePage } from '@/pages/SchedulePage'
import { SettingsPage } from '@/pages/SettingsPage'
import { Timeline } from '@/pages/Timeline'
import { formatRoute, parseRoute, type Route } from '@/router'
import { type PostMutationKind, type PostMutationSignal } from '@/types/postMutation'
import styles from '@/components/AppShell.module.css'

const MODEL_CONFIG_RETRY_DELAYS = [2_000, 5_000, 10_000, 30_000]
const SOULS_RETRY_DELAYS = [2_000, 5_000, 10_000, 30_000]
/* 未读轮询间隔。主动私聊全局冷却 3 天，半分钟的投递延迟无关紧要，
 * 而这一跳只是一条 SQL，静默期恒定为空列表。 */
const UNREAD_POLL_INTERVAL_MS = 30_000
/** 需要横向工作区的页面；其余路由走窄阅读栏。
 *  帖子详情看的还是一张卡，跟首页里那张一样宽才连贯——铺满只会让卡右半边空着。 */
const WORKSPACE_ROUTES = new Set<Route['kind']>(['goals', 'schedule', 'memory', 'settings', 'chats', 'chat'])
type SoulsLoadState = 'loading' | 'ready' | 'error'

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash))
  const [modelConfigured, setModelConfigured] = useState<boolean | null>(null)
  const [souls, setSouls] = useState<Soul[]>([])
  const [soulsLoadState, setSoulsLoadState] = useState<SoulsLoadState>('loading')
  const [activeGoalCount, setActiveGoalCount] = useState(0)
  const [memoryStatus, setMemoryStatus] = useState<MemoryStatus | null>(null)
  const [unreadThreads, setUnreadThreads] = useState<UnreadThread[]>([])
  const notifyDesktopRef = useRef(false)
  const notifiedMessageIdsRef = useRef<Set<number>>(loadNotifiedMessageIds())
  const [postMutationSignal, setPostMutationSignal] = useState<PostMutationSignal | null>(null)
  const [homeSearch, setHomeSearch] = useState('')
  const homeScrollTopRef = useRef(0)
  const previousRouteKindRef = useRef(route.kind)
  const showRightPanel = route.kind === 'home'
  /* 工作台页面横向铺开，阅读类页面（timeline / 详情 / 私聊）保持窄栏 */
  const shellWidth = WORKSPACE_ROUTES.has(route.kind) ? 'workspace' : 'reading'
  const navKey = navKeyFromRoute(route)
  const memoryQueueCount = memoryStatus?.pending_event_count ?? 0
  const selectedDate = route.kind === 'home' ? route.date ?? null : null
  const unreadBySoul = unreadCountsBySoul(unreadThreads)

  const loadSouls = useCallback(async () => {
    const data = await listSouls(true)
    setSouls(data)
    setSoulsLoadState('ready')
    return data
  }, [])

  const fetchSouls = useCallback(() => {
    if (souls.length === 0) setSoulsLoadState('loading')
    void loadSouls().catch(() => {
      setSoulsLoadState('error')
    })
  }, [loadSouls, souls.length])

  const refreshHomeContext = useCallback(async () => {
    try {
      const [memoryData, goalData] = await Promise.all([
        getMemoryStatus(),
        listGoals({ status: 'active' }),
      ])
      setMemoryStatus(memoryData)
      setActiveGoalCount(goalData.length)
    } catch {
      /* Keep the right rail calm when optional context is unavailable. */
    }
  }, [])

  const navigate = useCallback((nextRoute: Route) => {
    const nextHash = formatRoute(nextRoute)
    if (window.location.hash !== nextHash) {
      window.location.hash = nextHash
    }
    setRoute(nextRoute)
  }, [])

  const navigateToPage = useCallback((page: string) => {
    navigate(routeFromNavKey(page))
  }, [navigate])

  /* 未读轮询同时承担两件事：给私聊列表打点，以及把新到的主动私聊弹成桌面通知。
   * 用户几乎不主动进私聊，没有这条投递路径，信写得再好等于没发。 */
  const refreshUnread = useCallback(async () => {
    try {
      const data = await getChatUnread()
      setUnreadThreads(data.threads)
      if (!notifyDesktopRef.current) return
      const notified = notifiedMessageIdsRef.current
      let changed = false
      for (const thread of data.threads) {
        const messageId = thread.proactive_message_id
        if (messageId === null || notified.has(messageId)) continue
        const shown = showDesktopNotification(
          thread.soul_name,
          thread.proactive_preview ?? '给你发了条消息',
          {
            tag: `tracelog-letter-${messageId}`,
            onClick: () => navigate({ kind: 'chat', soulName: thread.soul_name }),
          },
        )
        if (!shown) continue
        notified.add(messageId)
        changed = true
      }
      if (changed) saveNotifiedMessageIds(notified)
    } catch {
      /* 未读只是装饰，拿不到就保持上一次的结果 */
    }
  }, [navigate])

  const notifyPostMutated = useCallback((postId: string, kind: PostMutationKind) => {
    setPostMutationSignal({ postId, kind, nonce: Date.now() })
  }, [])

  const openMemory = useCallback(() => {
    navigateToPage('memory')
  }, [navigateToPage])

  const openSettings = useCallback(() => {
    navigateToPage('settings')
  }, [navigateToPage])

  const openSchedule = useCallback(() => {
    navigateToPage('schedule')
  }, [navigateToPage])

  /* 日期透镜：点日期进入，再点同一日期退出（回到最新流）。 */
  const selectDate = useCallback((date: string) => {
    setRoute((current) => {
      const nextDate = current.kind === 'home' && current.date === date ? undefined : date
      const nextRoute: Route = { kind: 'home', date: nextDate }
      const nextHash = formatRoute(nextRoute)
      if (window.location.hash !== nextHash) window.location.hash = nextHash
      return nextRoute
    })
  }, [])

  const exitDateLens = useCallback(() => {
    navigate({ kind: 'home' })
  }, [navigate])

  const loadModelConfiguration = useCallback(async () => {
    const settings = await getModelSettings()
    setModelConfigured(settings.configured)
    const proactive = settings.proactive_message
    notifyDesktopRef.current = Boolean(proactive?.enabled && proactive.notify_desktop)
    return settings.configured
  }, [])

  const checkModelConfiguration = useCallback(() => {
    void loadModelConfiguration().catch(() => {
      /* API might not be running yet */
    })
  }, [loadModelConfiguration])

  useEffect(() => {
    let cancelled = false
    let retryTimer: number | null = null
    let retryIndex = 0

    const load = async () => {
      try {
        await loadSouls()
      } catch {
        if (cancelled) return
        setSoulsLoadState('error')
        const delay = SOULS_RETRY_DELAYS[Math.min(retryIndex, SOULS_RETRY_DELAYS.length - 1)]
        retryIndex += 1
        retryTimer = window.setTimeout(() => {
          retryTimer = null
          void load()
        }, delay)
      }
    }

    void load()

    return () => {
      cancelled = true
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer)
      }
    }
  }, [loadSouls])

  useEffect(() => {
    let cancelled = false
    let retryTimer: number | null = null
    let retryIndex = 0

    const check = async () => {
      try {
        await loadModelConfiguration()
      } catch {
        if (cancelled) return
        const delay = MODEL_CONFIG_RETRY_DELAYS[Math.min(retryIndex, MODEL_CONFIG_RETRY_DELAYS.length - 1)]
        retryIndex += 1
        retryTimer = window.setTimeout(() => {
          retryTimer = null
          void check()
        }, delay)
      }
    }

    void check()

    return () => {
      cancelled = true
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer)
      }
    }
  }, [loadModelConfiguration])

  useEffect(() => {
    const handleHashChange = () => {
      setRoute(parseRoute(window.location.hash))
    }
    window.addEventListener('hashchange', handleHashChange)
    return () => window.removeEventListener('hashchange', handleHashChange)
  }, [])

  useEffect(() => {
    const previousKind = previousRouteKindRef.current
    if (previousKind === 'home' && route.kind !== 'home') {
      homeScrollTopRef.current = window.scrollY
    }
    if (previousKind !== 'home' && route.kind === 'home') {
      window.requestAnimationFrame(() => window.scrollTo({ top: homeScrollTopRef.current }))
    }
    previousRouteKindRef.current = route.kind
  }, [route.kind])

  /* 进入首页时刷新右栏；挂载时也拉一次，保证导航 badge 在任意入口路由下有数据 */
  useEffect(() => {
    if (showRightPanel) void refreshHomeContext()
  }, [showRightPanel, refreshHomeContext])

  useEffect(() => {
    void refreshHomeContext()
  }, [refreshHomeContext])

  useEffect(() => {
    void refreshUnread()
    const timer = window.setInterval(() => {
      void refreshUnread()
    }, UNREAD_POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [refreshUnread])

  const renderMain = () => {
    const isHome = route.kind === 'home'
    return (
      <>
        <div
          className={isHome ? undefined : styles.hiddenPage}
          inert={!isHome || undefined}
          aria-hidden={!isHome || undefined}
        >
          <Timeline
            modelConfigured={modelConfigured}
            onOpenSettings={openSettings}
            onActivitySettled={refreshHomeContext}
            postMutationSignal={postMutationSignal}
            searchQuery={homeSearch}
            selectedDate={selectedDate}
            onExitDateLens={exitDateLens}
          />
        </div>
        {route.kind === 'post' && (
          <PostDetailPage
            key={route.postId}
            postId={route.postId}
            highlight={route.highlight}
            modelConfigured={modelConfigured}
            onOpenSettings={openSettings}
            onPostMutated={notifyPostMutated}
          />
        )}
        {route.kind === 'goals' && <GoalsPage />}
        {route.kind === 'chats' && (
          <ChatsPage
            souls={souls}
            loadState={soulsLoadState}
            unreadBySoul={unreadBySoul}
            onOpenChat={(soulName) => navigate({ kind: 'chat', soulName })}
          />
        )}
        {route.kind === 'schedule' && <SchedulePage onOpenSettings={openSettings} />}
        {route.kind === 'memory' && <MemoryWorkbench />}
        {route.kind === 'settings' && (
          <SettingsPage
            firstRun={modelConfigured === false}
            initialTab={route.tab}
            onModelSettingsChanged={checkModelConfiguration}
            onSoulsChanged={fetchSouls}
          />
        )}
        {route.kind === 'chat' && (
          <ChatPage
            key={route.soulName}
            soulName={route.soulName}
            modelConfigured={modelConfigured}
            onOpenSettings={openSettings}
            onThreadRead={refreshUnread}
          />
        )}
      </>
    )
  }

  return (
    <SoulColorProvider soulNames={souls.map((soul) => soul.name)}>
    <AppShell
      nav={(closeMobileNav) => (
        <LeftNav
          souls={souls}
          soulsLoadState={soulsLoadState}
          memoryQueueCount={memoryQueueCount}
          goalCount={activeGoalCount}
          unreadBySoul={unreadBySoul}
          activePage={navKey}
          onNavigate={navigateToPage}
          onAfterNavigate={closeMobileNav}
        />
      )}
      main={renderMain()}
      width={shellWidth}
      panel={showRightPanel ? (
        <RightPanel
          searchQuery={homeSearch}
          onSearchQueryChange={setHomeSearch}
          onOpenMemory={openMemory}
          selectedDate={selectedDate}
          onSelectDate={selectDate}
          onOpenSchedule={openSchedule}
          onOpenSettings={openSettings}
        />
      ) : undefined}
    />
    </SoulColorProvider>
  )
}

function unreadCountsBySoul(threads: UnreadThread[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const thread of threads) {
    counts[thread.soul_name] = (counts[thread.soul_name] ?? 0) + thread.unread_count
  }
  return counts
}

function navKeyFromRoute(route: Route): string {
  if (route.kind === 'chat') return `chat:${route.soulName}`
  if (route.kind === 'post') return 'home'
  return route.kind
}

function routeFromNavKey(page: string): Route {
  if (page === 'goals') return { kind: 'goals' }
  if (page === 'schedule') return { kind: 'schedule' }
  if (page === 'memory') return { kind: 'memory' }
  if (page === 'settings') return { kind: 'settings' }
  if (page === 'chats') return { kind: 'chats' }
  if (page.startsWith('chat:')) return { kind: 'chat', soulName: page.slice('chat:'.length) }
  return { kind: 'home' }
}
