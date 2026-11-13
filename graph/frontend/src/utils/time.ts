export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `${seconds}s前`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h前`
  const days = Math.floor(hours / 24)
  return `${days}d前`
}
