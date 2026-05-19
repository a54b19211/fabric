import http from './http'

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
}

export interface RelationItem {
  id: number
  source_doc_uuid: string
  relation_type: string
  from_entity_id: number | null
  to_entity_id: number | null
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
  review_status?: string
}) => http.get<any, { total: number; items: EntityItem[] }>('/review/entities', { params })

export const getEntity = (id: number) =>
  http.get<any, EntityItem>(`/review/entities/${id}`)

export const approveEntity = (id: number, reviewer: string, note = '') =>
  http.post<any, { success: boolean }>(`/review/entities/${id}/approve`, { reviewer, note })

export const rejectEntity = (id: number, reviewer: string, note = '') =>
  http.post<any, { success: boolean }>(`/review/entities/${id}/reject`, { reviewer, note })

export const getRelations = (params: {
  page?: number
  page_size?: number
  source_site?: string
  review_status?: string
}) => http.get<any, { total: number; items: RelationItem[] }>('/review/relations', { params })

export const approveRelation = (id: number, reviewer: string, note = '') =>
  http.post<any, { success: boolean }>(`/review/relations/${id}/approve`, { reviewer, note })

export const rejectRelation = (id: number, reviewer: string, note = '') =>
  http.post<any, { success: boolean }>(`/review/relations/${id}/reject`, { reviewer, note })
