import { useCallback, useEffect, useState } from 'react'
import { type Soul, listSouls, getProfile } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { LeftNav } from '@/components/LeftNav'
import { RightPanel } from '@/components/RightPanel'
import { ChatPage } from '@/pages/ChatPage'
import { ReflectionsPage } from '@/pages/ReflectionsPage'
import { Timeline } from '@/pages/Timeline'
import { TodosPage } from '@/pages/TodosPage'

export function App() {
  const [activePage, setActivePage] = useState('home')
  const [souls, setSouls] = useState<Soul[]>([])
  const [profileContent, setProfileContent] = useState<string | null>(null)

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

  useEffect(() => {
    fetchSouls()
    fetchProfile()
  }, [fetchSouls, fetchProfile])

  const renderMain = () => {
    switch (activePage) {
      case 'home':
        return <Timeline />
      case 'todos':
        return <TodosPage />
      case 'reflections':
        return <ReflectionsPage />
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
      panel={
        <RightPanel
          profileContent={profileContent}
          souls={souls.map((s) => ({ name: s.name, description: s.description }))}
        />
      }
    />
  )
}
