import { useCallback, useEffect, useState } from 'react'
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

const DEFAULT_PAGE = 'home'

export function App() {
  const [activePage, setActivePage] = useState(() => pageFromHash(window.location.hash))
  const [modelConfigured, setModelConfigured] = useState<boolean | null>(null)
  const [souls, setSouls] = useState<Soul[]>([])
  const [todos, setTodos] = useState<Todo[]>([])
  const [globalReflection, setGlobalReflection] = useState<ReflectionScope | null>(null)
  const [soulReflections, setSoulReflections] = useState<SoulReflectionScope[]>([])
  const showRightPanel = activePage === 'home'

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

  const navigateToPage = useCallback((page: string) => {
    const nextHash = hashFromPage(page)
    if (window.location.hash !== nextHash) {
      window.location.hash = nextHash
    }
    setActivePage(page)
  }, [])

  const handleTodosChanged = useCallback((nextTodos?: Todo[]) => {
    if (nextTodos) {
      setTodos(nextTodos)
      return
    }
    void refreshTodos()
  }, [refreshTodos])

  const handleTodoToggle = useCallback(async (todo: Todo) => {
    await updateTodo(todo.id, { status: '已完成' })
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
      setActivePage(pageFromHash(window.location.hash))
    }
    window.addEventListener('hashchange', handleHashChange)
    return () => window.removeEventListener('hashchange', handleHashChange)
  }, [])

  useEffect(() => {
    if (showRightPanel) void refreshHomeContext()
  }, [showRightPanel, refreshHomeContext])

  const renderMain = () => {
    switch (activePage) {
      case 'home':
        return (
          <Timeline
            modelConfigured={modelConfigured}
            onOpenSettings={openSettings}
            onActivitySettled={refreshHomeContext}
            onTodosChanged={refreshTodos}
          />
        )
      case 'todos':
        return <TodosPage onTodosChanged={handleTodosChanged} />
      case 'reflections':
        return <ReflectionsPage onReflectionSettled={refreshHomeContext} />
      case 'settings':
        return (
          <SettingsPage
            firstRun={modelConfigured === false}
            onModelSettingsChanged={checkModelConfiguration}
            onSoulsChanged={fetchSouls}
          />
        )
      default:
        if (activePage.startsWith('chat:')) {
          const soulName = activePage.slice('chat:'.length)
          return <ChatPage soulName={soulName} modelConfigured={modelConfigured} onOpenSettings={openSettings} />
        }
        return (
          <Timeline
            modelConfigured={modelConfigured}
            onOpenSettings={openSettings}
            onActivitySettled={refreshHomeContext}
            onTodosChanged={refreshTodos}
          />
        )
    }
  }

  return (
    <AppShell
      nav={(closeMobileNav) => (
        <LeftNav
          souls={souls}
          activePage={activePage}
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

function pageFromHash(hash: string): string {
  const path = hash.replace(/^#/, '').replace(/^\//, '')
  if (!path) return DEFAULT_PAGE
  if (path === 'home') return 'home'
  if (path === 'todos') return 'todos'
  if (path === 'reflections') return 'reflections'
  if (path === 'settings') return 'settings'
  if (path.startsWith('chat/')) {
    const encodedSoulName = path.slice('chat/'.length)
    const soulName = decodeRouteSegment(encodedSoulName)
    return soulName ? `chat:${soulName}` : DEFAULT_PAGE
  }
  return DEFAULT_PAGE
}

function hashFromPage(page: string): string {
  if (page === 'home') return '#/'
  if (page === 'todos') return '#/todos'
  if (page === 'reflections') return '#/reflections'
  if (page === 'settings') return '#/settings'
  if (page.startsWith('chat:')) {
    const soulName = page.slice('chat:'.length)
    return `#/chat/${encodeURIComponent(soulName)}`
  }
  return '#/'
}

function decodeRouteSegment(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}
