import http from './http'

export interface DisambigCandidate {
  dict_id: number
  name: string
  similarity_score: number
  match_type: string
}

export interface DisambigInfo {
  status: string
  candidates: DisambigCandidate[]
  match_step: string
}

export interface EntityItem {
  id: number
  source_doc_uuid: string
  entity_type: string
  entity_name: string
  entity_data: Record<string, any>
  confidence_score: number
  confidence_reason: string
  source_text: string
  review_status: string
  dict_id: number | null
  disambig: DisambigInfo | null
}

export interface RelationItem {
  id: number
  source_doc_uuid: string
  relation_type: string
  from_entity_id: number | null
  from_entity_name: string
  to_entity_id: number | null
  to_entity_name: string
  relation_props: Record<string, any>
  confidence_score: number
  source_text: string
  review_status: string
}

export const getEntities = (params: {
  page?: number
  page_size?: number
  entity_type?: string
  source_site?: string
  source_doc_uuid?: string
  review_status?: string
}) => http.get<any, { total: number; items: EntityItem[] }>('/review/entities', { params })

export const getEntity = (id: number) =>
  http.get<any, EntityItem>(`/review/entities/${id}`)

export const approveEntity = (id: number, reviewer: string, note = '', forceNew = false, dictId: number | null = null) =>
  http.post<any, { success: boolean }>(`/review/entities/${id}/approve`, { reviewer, note, force_new: forceNew, dict_id: dictId })

export const rejectEntity = (id: number, reviewer: string, note = '') =>
  http.post<any, { success: boolean }>(`/review/entities/${id}/reject`, { reviewer, note })

export const getRelations = (params: {
  page?: number
  page_size?: number
  source_site?: string
  source_doc_uuid?: string
  review_status?: string
}) => http.get<any, { total: number; items: RelationItem[] }>('/review/relations', { params })

export const approveRelation = (id: number, reviewer: string, note = '') =>
  http.post<any, { success: boolean }>(`/review/relations/${id}/approve`, { reviewer, note })

export const rejectRelation = (id: number, reviewer: string, note = '') =>
  http.post<any, { success: boolean }>(`/review/relations/${id}/reject`, { reviewer, note })

// 批量审核接口
export const batchApproveEntities = (params: {
  source_doc_uuid?: string
  entity_type?: string
  source_site?: string
  min_confidence?: number
  reviewer: string
  note?: string
}) => http.post<any, { success: boolean; message?: string }>('/review/entities/batch/approve', params)

export const batchRejectEntities = (params: {
  source_doc_uuid?: string
  entity_type?: string
  source_site?: string
  reviewer: string
  note?: string
}) => http.post<any, { success: boolean; message?: string }>('/review/entities/batch/reject', params)

export const batchApproveRelations = (params: {
  source_doc_uuid?: string
  source_site?: string
  min_confidence?: number
  reviewer: string
  note?: string
}) => http.post<any, { success: boolean; message?: string }>('/review/relations/batch/approve', params)

export const batchRejectRelations = (params: {
  source_doc_uuid?: string
  source_site?: string
  reviewer: string
  note?: string
}) => http.post<any, { success: boolean; message?: string }>('/review/relations/batch/reject', params)

// 合并队列
export interface MergeQueueItem {
  id: number
  source_entity_id: number
  target_dict_id: number
  field_name: string
  existing_value: any
  new_value: any
  source_site: string
  resolution: string
  created_at: string | null
}

export const getMergeQueue = (limit = 50) =>
  http.get<any, { total: number; items: MergeQueueItem[] }>('/review/merge-queue', { params: { limit } })

export const resolveMerge = (mergeId: number, reviewer: string, resolution: string, resolvedValue?: any) =>
  http.post<any, { success: boolean }>(`/review/merge-queue/${mergeId}/resolve`, {
    reviewer, resolution, resolved_value: resolvedValue,
  })

// ── 图谱 ────────────────────────────────────────────────────────────────

export interface GraphNode {
  id: number
  entity_type: string
  entity_name: string
  entity_data: Record<string, any>
  dict_id: number | null
  review_status: string
  source_doc_uuid: string
  source_site: string
  confidence_score: number
  is_verified: boolean
}

export interface GraphEdge {
  id: number
  relation_type: string
  from_entity_id: number | null
  to_entity_id: number | null
  review_status: string
  relation_props: Record<string, any>
  source_doc_uuid: string
}

export interface GraphResponse {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export const getReviewGraph = (params: {
  entity_types?: string
  source_site?: string
  review_status?: string
  source_doc_uuid?: string
  include_relations?: boolean
  limit?: number
}) => http.get<any, GraphResponse>('/review/graph', { params })

// ── 仪表盘统计 ──────────────────────────────────────────────────────────

export interface StatusCounts {
  pending: number
  approved: number
  rejected: number
}

export interface ReviewStats {
  entities: StatusCounts
  relations: StatusCounts
  by_type: Record<string, StatusCounts>
  by_site: Record<string, StatusCounts>
}

export const getReviewStats = () =>
  http.get<any, ReviewStats>('/review/stats')

// ── 元数据编辑 ──────────────────────────────────────────────────────────

export const patchEntity = (id: number, entityData: Record<string, any>) =>
  http.patch<any, { success: boolean; message?: string }>(`/review/entities/${id}`, { entity_data: entityData })

export const patchRelation = (id: number, relationProps: Record<string, any>) =>
  http.patch<any, { success: boolean; message?: string }>(`/review/relations/${id}`, { relation_props: relationProps })
