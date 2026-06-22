import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type ReflectionScope,
  type Soul,
  type SoulReflectionScope,
  type Todo,
  getModelSettings,
  listGoals,
  listSouls,
  listTodos,
  previewGlobalReflection,
  previewSoulReflections,
  updateTodo,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { LeftNav } from '@/components/LeftNav'
import { RightPanel } from '@/components/RightPanel'
import { ChatPage } from '@/pages/ChatPage'
import { GoalsPage } from '@/pages/GoalsPage'
import { MemoryWorkbench } from '@/pages/MemoryWorkbench'
import { ReflectionsPage } from '@/pages/ReflectionsPage'
import { PostDetailPage } from '@/pages/PostDetailPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { Timeline } from '@/pages/Timeline'
import { TodosPage } from '@/pages/TodosPage'
import { formatRoute, parseRoute, type Route } from '@/router'
import { type PostMutationKind, type PostMutationSignal } from '@/types/postMutation'
import { isTodoDone } from '@/utils/todo'
import styles from '@/components/AppShell.module.css'

const MODEL_CONFIG_RETRY_DELAYS = [2_000, 5_000, 10_000, 30_000]
const SOULS_RETRY_DELAYS = [2_000, 5_000, 10_000, 30_000]
type SoulsLoadState = 'loading' | 'ready' | 'error'

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash))
  const [modelConfigured, setModelConfigured] = useState<boolean | null>(null)
  const [souls, setSouls] = useState<Soul[]>([])
  const [soulsLoadState, setSoulsLoadState] = useState<SoulsLoadState>('loading')
  const [todos, setTodos] = useState<Todo[]>([])
  const [activeGoalCount, setActiveGoalCount] = useState(0)
  const [globalReflection, setGlobalReflection] = useState<ReflectionScope | null>(null)
  const [soulReflections, setSoulReflections] = useState<SoulReflectionScope[]>([])
  const [postMutationSignal, setPostMutationSignal] = useState<PostMutationSignal | null>(null)
  const [homeSearch, setHomeSearch] = useState('')
  const homeScrollTopRef = useRef(0)
  const previousRouteKindRef = useRef(route.kind)
  const showRightPanel = route.kind === 'home'
  const navKey = navKeyFromRoute(route)
  const reflectionQueueCount = (globalReflection?.post_ids.length ?? 0)
    + soulReflections.reduce((total, scope) => total + Math.max(scope.interaction_count, 0), 0)
  const openTodoCount = todos.filter((todo) => !isTodoDone(todo)).length

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
      const [todoData, globalData, soulData, goalData] = await Promise.all([
        listTodos(),
        previewGlobalReflection(),
        previewSoulReflections(),
        listGoals({ status: 'active' }),
      ])
      setTodos(todoData)
      setGlobalReflection(globalData)
      setSoulReflections(soulData)
      setActiveGoalCount(goalData.length)
    } catch {
      /* Keep the right rail calm when optional context is unavailable. */
    }
  }, [])

  const refreshTodos = useCallback(async () => {
    const todoData = await listTodos()
    setTodos(todoData)
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

  const handleTodosChanged = useCallback((nextTodos?: Todo[]) => {
    if (nextTodos) {
      setTodos(nextTodos)
      return
    }
    void refreshTodos()
  }, [refreshTodos])

  const handleTodoToggle = useCallback(async (todo: Todo) => {
    await updateTodo(todo.id, { status: isTodoDone(todo) ? '未完成' : '已完成' })
    await refreshTodos()
  }, [refreshTodos])

  const openMemory = useCallback(() => {
    navigateToPage('memory')
  }, [navigateToPage])

  const openTodos = useCallback(() => {
    navigateToPage('todos')
  }, [navigateToPage])

  const openSettings = useCallback(() => {
    navigateToPage('settings')
  }, [navigateToPage])

  const loadModelConfiguration = useCallback(async () => {
    const settings = await getModelSettings()
    setModelConfigured(settings.configured)
    if (!settings.configured) navigateToPage('settings')
    return settings.configured
  }, [navigateToPage])

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
            onTodosChanged={refreshTodos}
            postMutationSignal={postMutationSignal}
            searchQuery={homeSearch}
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
            onTodosChanged={refreshTodos}
          />
        )}
        {route.kind === 'todos' && <TodosPage onTodosChanged={handleTodosChanged} />}
        {route.kind === 'goals' && <GoalsPage />}
        {route.kind === 'memory' && <MemoryWorkbench />}
        {route.kind === 'reflections' && <ReflectionsPage onReflectionSettled={refreshHomeContext} />}
        {route.kind === 'settings' && (
          <SettingsPage
            firstRun={modelConfigured === false}
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
          reflectionQueueCount={reflectionQueueCount}
          goalCount={activeGoalCount}
          todoCount={openTodoCount}
          activePage={navKey}
          onNavigate={navigateToPage}
          onAfterNavigate={closeMobileNav}
        />
      )}
      main={renderMain()}
      panel={showRightPanel ? (
        <RightPanel
          todos={todos}
          searchQuery={homeSearch}
          onSearchQueryChange={setHomeSearch}
          onTodoToggle={handleTodoToggle}
          onOpenTodos={openTodos}
          onOpenMemory={openMemory}
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
  if (page === 'todos') return { kind: 'todos' }
  if (page === 'goals') return { kind: 'goals' }
  if (page === 'memory') return { kind: 'memory' }
  if (page === 'reflections') return { kind: 'reflections' }
  if (page === 'settings') return { kind: 'settings' }
  if (page.startsWith('chat:')) return { kind: 'chat', soulName: page.slice('chat:'.length) }
  return { kind: 'home' }
}
