import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type ReflectionScope,
  type Soul,
  type SoulReflectionScope,
  type Todo,
  getModelSettings,
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
import { ReflectionsPage } from '@/pages/ReflectionsPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { Timeline } from '@/pages/Timeline'
import { TodosPage } from '@/pages/TodosPage'
import { formatRoute, parseRoute, type Route } from '@/router'
import { isTodoDone } from '@/utils/todo'
import styles from '@/components/AppShell.module.css'

export type PostMutationSignal = {
  postId: string
  kind: 'updated' | 'deleted'
  nonce: number
}

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash))
  const [modelConfigured, setModelConfigured] = useState<boolean | null>(null)
  const [souls, setSouls] = useState<Soul[]>([])
  const [todos, setTodos] = useState<Todo[]>([])
  const [globalReflection, setGlobalReflection] = useState<ReflectionScope | null>(null)
  const [soulReflections, setSoulReflections] = useState<SoulReflectionScope[]>([])
  const postMutationSignal: PostMutationSignal | null = null
  const homeScrollTopRef = useRef(0)
  const previousRouteKindRef = useRef(route.kind)
  const showRightPanel = route.kind === 'home'
  const navKey = navKeyFromRoute(route)

  const fetchSouls = useCallback(async () => {
    try {
      const data = await listSouls(true)
      setSouls(data)
    } catch {
      /* API might not be running yet */
    }
  }, [])

  const refreshHomeContext = useCallback(async () => {
    try {
      const [todoData, globalData, soulData] = await Promise.all([
        listTodos(),
        previewGlobalReflection(),
        previewSoulReflections(),
      ])
      setTodos(todoData)
      setGlobalReflection(globalData)
      setSoulReflections(soulData)
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

  const openReflections = useCallback(() => {
    navigateToPage('reflections')
  }, [navigateToPage])

  const openTodos = useCallback(() => {
    navigateToPage('todos')
  }, [navigateToPage])

  const openSettings = useCallback(() => {
    navigateToPage('settings')
  }, [navigateToPage])

  const checkModelConfiguration = useCallback(async () => {
    try {
      const settings = await getModelSettings()
      setModelConfigured(settings.configured)
      if (!settings.configured) navigateToPage('settings')
    } catch {
      /* API might not be running yet */
    }
  }, [navigateToPage])

  useEffect(() => {
    fetchSouls()
    checkModelConfiguration()
  }, [fetchSouls, checkModelConfiguration])

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

  useEffect(() => {
    if (showRightPanel) void refreshHomeContext()
  }, [showRightPanel, refreshHomeContext])

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
          />
        </div>
        {route.kind === 'post' && (
          <div className={styles.placeholderPage}>
            <button className={styles.backButton} onClick={() => navigate({ kind: 'home' })}>
              返回首页
            </button>
            <h1>记录详情</h1>
            <p>正在接入详情页：{route.postId}</p>
          </div>
        )}
        {route.kind === 'todos' && <TodosPage onTodosChanged={handleTodosChanged} />}
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
          activePage={navKey}
          onNavigate={navigateToPage}
          onAfterNavigate={closeMobileNav}
        />
      )}
      main={renderMain()}
      panel={showRightPanel ? (
        <RightPanel
          todos={todos}
          globalReflection={globalReflection}
          soulReflections={soulReflections}
          onTodoToggle={handleTodoToggle}
          onOpenTodos={openTodos}
          onOpenReflections={openReflections}
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
  if (page === 'reflections') return { kind: 'reflections' }
  if (page === 'settings') return { kind: 'settings' }
  if (page.startsWith('chat:')) return { kind: 'chat', soulName: page.slice('chat:'.length) }
  return { kind: 'home' }
}
