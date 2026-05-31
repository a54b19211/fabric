import http from './http'

// ── 图数据同步 → WeKnora 的 Neo4j 拷贝库 ──────────────────────────────
// 后端路由统一在 /api/weknora/graph/ 下，前端按职责拆分为 Neo4j 页面调用

export const syncEntityDict = (limit = 500) =>
  http.post<any, { success: boolean; pushed: number; failed: number; message: string }>(
    '/weknora/graph/sync-entity-dict', { limit })

export const initPrimeKGCopies = (kbId = '') =>
  http.post<any, { success: boolean; pushed: number; message: string }>(
    '/weknora/graph/init-copies', null, { params: { kb_id: kbId } })

export const syncGraphEntities = (limit = 500) =>
  http.post<any, { success: boolean; pushed: number; failed: number; message: string }>(
    '/weknora/graph/sync-entities', { limit })

export const syncGraphRelations = (limit = 500) =>
  http.post<any, { success: boolean; pushed: number; failed: number; message: string }>(
    '/weknora/graph/sync-relations', { limit })

export const getGraphSyncStats = () =>
  http.get<any, {
    entity_dict: { pending: number; synced: number; failed: number }
    entities: { pending: number; pushed: number; failed: number }
    relations: { pending: number; pushed: number; failed: number }
  }>('/weknora/graph/stats')
