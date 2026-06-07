import http from './http'

export interface SourceDistribution {
  site: string
  count: number
}

export interface DashboardData {
  total_documents: number
  today_new: number
  source_distribution: SourceDistribution[]
  entity_count: number
  relation_count: number
  avg_entity_confidence: number
  review_pass_rate: number
  neo4j_pending_entities: number
  neo4j_pending_relations: number
  neo4j_failed_total: number
  weknora_pending: number
  weknora_synced: number
  weknora_failed: number
  extraction_pending: number
  extraction_done: number
}

export async function getDashboard(): Promise<DashboardData> {
  const { data } = await http.get('/stats/dashboard')
  return data
}

export interface TopEntityItem {
  entity_name: string
  entity_type: string
  doc_count: number
  avg_confidence: number
}

export interface TopEntitiesResponse {
  items: TopEntityItem[]
}

export async function getTopEntities(params?: {
  entity_type?: string
  limit?: number
}): Promise<TopEntitiesResponse> {
  const { data } = await http.get('/stats/top-entities', { params })
  return data
}
