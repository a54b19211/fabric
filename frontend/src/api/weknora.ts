import http from './http'

export const getWeknoraPending = (limit = 100) =>
  http.get<any, { total: number; items: any[] }>('/weknora/pending', { params: { limit } })

export const syncWeknora = (uuid: string) =>
  http.post<any, { success: boolean; message: string }>(`/weknora/sync/${uuid}`)

export const syncWeknoraBatch = (limit = 100) =>
  http.post<any, { triggered: number }>('/weknora/sync-batch', { limit })

export const getWeknoraStats = () =>
  http.get<any, { pending: number; synced: number; failed: number }>('/weknora/stats')
