interface PollUntilOptions<T> {
  intervalMs: number
  timeoutMs?: number | null
  tick: () => Promise<T>
  isDone: (value: T) => boolean
  signal?: AbortSignal
}

export class PollTimeoutError extends Error {
  constructor(message = 'Polling timed out') {
    super(message)
    this.name = 'PollTimeoutError'
  }
}

export async function pollUntil<T>({
  intervalMs,
  timeoutMs,
  tick,
  isDone,
  signal,
}: PollUntilOptions<T>): Promise<T> {
  const startedAt = Date.now()

  while (true) {
    throwIfAborted(signal)
    const value = await tick()
    if (isDone(value)) return value
    const elapsedMs = Date.now() - startedAt
    if (timeoutMs != null && elapsedMs >= timeoutMs) {
      throw new PollTimeoutError()
    }
    await wait(timeoutMs == null ? intervalMs : Math.min(intervalMs, timeoutMs - elapsedMs), signal)
  }
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  if (ms <= 0) return Promise.resolve()

  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)

    const onAbort = () => {
      window.clearTimeout(timeoutId)
      reject(new DOMException('Polling aborted', 'AbortError'))
    }

    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function throwIfAborted(signal?: AbortSignal) {
  if (signal?.aborted) {
    throw new DOMException('Polling aborted', 'AbortError')
  }
}
