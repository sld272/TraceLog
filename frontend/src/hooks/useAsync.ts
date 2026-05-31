import { useCallback, useEffect, useRef, useState } from 'react'

type Status = 'idle' | 'loading' | 'success' | 'error'

interface UseAsyncState<T> {
  data: T | null
  status: Status
  error: string | null
  execute: () => Promise<void>
}

export function useAsync<T>(
  asyncFn: () => Promise<T>,
  immediate = true,
): UseAsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const execute = useCallback(async () => {
    setStatus('loading')
    setError(null)
    try {
      const result = await asyncFn()
      if (mountedRef.current) {
        setData(result)
        setStatus('success')
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : 'Unknown error')
        setStatus('error')
      }
    }
  }, [asyncFn])

  useEffect(() => {
    mountedRef.current = true
    if (immediate) {
      execute()
    }
    return () => {
      mountedRef.current = false
    }
  }, [execute, immediate])

  return { data, status, error, execute }
}
