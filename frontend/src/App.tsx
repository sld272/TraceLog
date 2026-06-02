import { useCallback, useEffect, useState } from 'react'
import {
  type ReflectionScope,
  type Soul,
  type SoulReflectionScope,
  type Todo,
  getProfile,
  listSouls,
  listTodos,
  previewGlobalReflection,
  previewSoulReflections,
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
  const [profileContent, setProfileContent] = useState<string | null>(null)
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

  const fetchProfile = useCallback(async () => {
    try {
      const data = await getProfile()
      setProfileContent(data.content)
    } catch {
      /* API might not be running yet */
    }
  }, [])

  const fetchRightPanelData = useCallback(async () => {
    try {
      const [todoData, globalData, soulData] = await Promise.all([
        listTodos(),
        previewGlobalReflection(20),
        previewSoulReflections(20),
      ])
      setTodos(todoData)
      setGlobalReflection(globalData)
      setSoulReflections(soulData)
    } catch {
      /* Keep the right rail calm when optional context is unavailable. */
    }
  }, [])

  useEffect(() => {
    fetchSouls()
    fetchProfile()
    fetchRightPanelData()
  }, [fetchSouls, fetchProfile, fetchRightPanelData])

  const renderMain = () => {
    switch (activePage) {
      case 'home':
        return <Timeline />
      case 'todos':
        return <TodosPage />
      case 'reflections':
        return <ReflectionsPage />
      case 'settings':
        return <SettingsPage onSoulsChanged={fetchSouls} />
      default:
        if (activePage.startsWith('chat:')) {
          const soulName = activePage.replace('chat:', '')
          return <ChatPage soulName={soulName} />
        }
        return <Timeline />
    }
  }

  return (
    <AppShell
      nav={
        <LeftNav
          souls={souls}
          activePage={activePage}
          onNavigate={setActivePage}
        />
      }
      main={renderMain()}
      panel={showRightPanel ? (
        <RightPanel
          profileContent={profileContent}
          todos={todos}
          globalReflection={globalReflection}
          soulReflections={soulReflections}
        />
      ) : undefined}
    />
  )
}
