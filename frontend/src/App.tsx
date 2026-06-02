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
import { Timeline } from '@/pages/Timeline'
import { TodosPage } from '@/pages/TodosPage'
import workspaceStyles from '@/pages/WorkspacePages.module.css'

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
        return <SettingsPlaceholder />
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

function SettingsPlaceholder() {
  return (
    <div className={workspaceStyles.page}>
      <header className={workspaceStyles.header}>
        <div className={workspaceStyles.titleGroup}>
          <h1 className={workspaceStyles.title}>设置</h1>
          <p className={workspaceStyles.subtitle}>配置入口已预留</p>
        </div>
      </header>
      <section className={workspaceStyles.card}>
        <p className={workspaceStyles.meta}>这里之后可以接入模型、工作区和 SOUL 配置。</p>
      </section>
    </div>
  )
}
