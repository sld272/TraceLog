import { useCallback, useEffect, useState } from 'react'
import {
  type ReflectionScope,
  type Soul,
  type SoulReflectionScope,
  type Todo,
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

export function App() {
  const [activePage, setActivePage] = useState('home')
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
    setActivePage('reflections')
  }, [])

  const openTodos = useCallback(() => {
    setActivePage('todos')
  }, [])

  useEffect(() => {
    fetchSouls()
  }, [fetchSouls])

  useEffect(() => {
    if (showRightPanel) void refreshHomeContext()
  }, [showRightPanel, refreshHomeContext])

  const renderMain = () => {
    switch (activePage) {
      case 'home':
        return <Timeline onActivitySettled={refreshHomeContext} onTodosChanged={refreshTodos} />
      case 'todos':
        return <TodosPage onTodosChanged={handleTodosChanged} />
      case 'reflections':
        return <ReflectionsPage />
      case 'settings':
        return <SettingsPage onSoulsChanged={fetchSouls} />
      default:
        if (activePage.startsWith('chat:')) {
          const soulName = activePage.replace('chat:', '')
          return <ChatPage soulName={soulName} />
        }
        return <Timeline onActivitySettled={refreshHomeContext} onTodosChanged={refreshTodos} />
    }
  }

  return (
    <AppShell
      nav={(closeMobileNav) => (
        <LeftNav
          souls={souls}
          activePage={activePage}
          onNavigate={setActivePage}
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
