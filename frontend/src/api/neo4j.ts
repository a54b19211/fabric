import http from './http'

export const syncEntities = (limit = 100) =>
  http.post<any, { pushed: number; failed: number; message: string }>('/neo4j/sync/entities', { limit })

export const syncRelations = (limit = 100) =>
  http.post<any, { pushed: number; failed: number; message: string }>('/neo4j/sync/relations', { limit })

export const retryNeo4j = (object_type: 'entity' | 'relation', object_id: number) =>
  http.post<any, { success: boolean; error: string }>('/neo4j/retry', { object_type, object_id })

export const getNeo4jStats = () =>
  http.get<any, {
    entities: { pending: number; pushed: number; failed: number }
    relations: { pending: number; pushed: number; failed: number }
  }>('/neo4j/stats')

export const getNeo4jFailed = (object_type = '', limit = 50) =>
  http.get<any, {
    entities: any[]
    relations: any[]
  }>('/neo4j/failed', { params: { object_type, limit } })
