import { useCallback, useEffect, useState } from 'react'
import { type Soul, listSouls, getProfile } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { LeftNav } from '@/components/LeftNav'
import { RightPanel } from '@/components/RightPanel'
import { Timeline } from '@/pages/Timeline'

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
        return <PlaceholderPage title="待办" description="待办功能即将上线" />
      case 'reflections':
        return <PlaceholderPage title="反思" description="反思功能即将上线" />
      default:
        if (activePage.startsWith('chat:')) {
          const soulName = activePage.replace('chat:', '')
          return <PlaceholderPage title={`与 ${soulName} 私聊`} description="私聊功能即将上线" />
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

function PlaceholderPage({ title, description }: { title: string; description: string }) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '4rem 1rem',
      color: 'var(--color-text-tertiary)',
    }}>
      <h2 style={{ fontSize: 'var(--text-xl)', color: 'var(--color-text-secondary)', marginBottom: '0.5rem' }}>
        {title}
      </h2>
      <p style={{ fontSize: 'var(--text-sm)' }}>{description}</p>
    </div>
  )
}
