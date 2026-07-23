import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type MemoryStatus,
  type Soul,
  getMemoryStatus,
  getModelSettings,
  listGoals,
  listSouls,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { LeftNav } from '@/components/LeftNav'
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
type SoulsLoadState = 'loading' | 'ready' | 'error'

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash))
  const [modelConfigured, setModelConfigured] = useState<boolean | null>(null)
  const [souls, setSouls] = useState<Soul[]>([])
  const [soulsLoadState, setSoulsLoadState] = useState<SoulsLoadState>('loading')
  const [activeGoalCount, setActiveGoalCount] = useState(0)
  const [memoryStatus, setMemoryStatus] = useState<MemoryStatus | null>(null)
  const [postMutationSignal, setPostMutationSignal] = useState<PostMutationSignal | null>(null)
  const [homeSearch, setHomeSearch] = useState('')
  const homeScrollTopRef = useRef(0)
  const previousRouteKindRef = useRef(route.kind)
  const showRightPanel = route.kind === 'home'
  const navKey = navKeyFromRoute(route)
  const memoryQueueCount = memoryStatus?.pending_event_count ?? 0
  const selectedDate = route.kind === 'home' ? route.date ?? null : null

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
          />
        )}
      </>
    )
  }

  return (
    <AppShell
      nav={(closeMobileNav) => (
        <LeftNav
          souls={souls}
          soulsLoadState={soulsLoadState}
          memoryQueueCount={memoryQueueCount}
          goalCount={activeGoalCount}
          activePage={navKey}
          onNavigate={navigateToPage}
          onAfterNavigate={closeMobileNav}
        />
      )}
      main={renderMain()}
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
  )
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
