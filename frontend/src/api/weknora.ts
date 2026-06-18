import http from './http'

export const getWeknoraPending = (limit = 20, offset = 0, sourceSite = '') =>
  http.get<any, { total: number; items: any[] }>('/weknora/pending', {
    params: { limit, offset, ...(sourceSite ? { source_site: sourceSite } : {}) },
  })

export const syncWeknora = (uuid: string, kbId = '') =>
  http.post<any, { success: boolean; message: string }>(
    `/weknora/sync/${uuid}`,
    null,
    { params: { ...(kbId ? { kb_id: kbId } : {}) } },
  )

export const syncWeknoraBatch = (count = 100, kbId = '', sourceSite = '') =>
  http.post<any, { triggered: number }>(
    '/weknora/sync-batch',
    { limit: count },
    { params: { ...(kbId ? { kb_id: kbId } : {}), ...(sourceSite ? { source_site: sourceSite } : {}) } },
  )

export const getWeknoraStats = () =>
  http.get<any, { pending: number; synced: number; failed: number }>('/weknora/stats')
