import http from './http'

// ── 图数据同步 → WeKnora 的 Neo4j 拷贝库 ──────────────────────────────

export const syncEntityDict = (limit = 500) =>
  http.post<any, { success: boolean; pushed: number; failed: number; message: string }>(
    '/weknora/graph/sync-entity-dict', { limit })

export const syncGraphEntities = (kbId = '') =>
  http.post<any, { success: boolean; pushed: number; failed: number; message: string }>(
    '/weknora/graph/sync-entities', null, { params: { kb_id: kbId } })

export const syncGraphRelations = (kbId = '') =>
  http.post<any, { success: boolean; pushed: number; failed: number; message: string }>(
    '/weknora/graph/sync-relations', null, { params: { kb_id: kbId } })

export const getGraphSyncStats = () =>
  http.get<any, {
    entity_dict: { pending: number; synced: number; failed: number }
    entities: { pending: number; pushed: number; failed: number }
    relations: { pending: number; pushed: number; failed: number }
  }>('/weknora/graph/stats')
