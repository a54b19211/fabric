import http from './http'

// Pipeline 步骤（实体/关系审核）
export interface PipelineStep {
  step_id: string
  step_name: string
  status: 'passed' | 'failed' | 'skipped'
  input: Record<string, any>
  output: Record<string, any>
  duration_ms: number
}

// ReAct 步骤（冲突裁决/语义匹配）
export interface AgentStep {
  step: number
  type: 'ai' | 'tool'
  content: string
  tool_calls?: string[]
  tool_name?: string
  tool_input?: Record<string, any>
}

export type ReviewStep = PipelineStep | AgentStep

export interface AgentReviewResult {
  run_id: string
  decision: string
  reason: string
  confidence: number
  suggested_dict_id?: number | null
  dict_id?: number | null
  merge_queue_id?: number | null
  resolved_value?: string
  keeper_dict_id?: number | null
  entity_name?: string
  document_title?: string
  steps: ReviewStep[]
}

export interface AgentRunRecord {
  id: number
  run_id: string
  agent_type: string
  target_id: number
  entity_name: string
  document_title: string
  status: string
  decision: string
  reason: string
  confidence: number
  steps: ReviewStep[]
  started_at: string
  completed_at: string | null
  created_at: string
}

export interface AgentStats {
  stats: Record<string, Record<string, number>>
}

// 实体审核
export const triggerEntityReview = (entityId: number) =>
  http.post<any, AgentReviewResult>(`/agent/review/entity/${entityId}`)

export const triggerBatchEntityReview = (params: {
  limit?: number
  entity_type?: string
  source_site?: string
}) =>
  http.post<any, { queued: number; results: AgentReviewResult[]; summary: Record<string, number> }>(
    '/agent/review/entity/batch',
    params,
  )

export const triggerBatchAsync = (params: {
  limit?: number
  entity_type?: string
  source_site?: string
}) =>
  http.post<any, { run_id: string; status: string; message: string }>(
    '/agent/review/entity/batch/async',
    params,
  )

// 文档维度审核
export interface DocumentReviewResponse {
  batch_id: string
  document_uuid: string
  document_title: string
  total: number
  approved: number
  rejected: number
  escalated: number
  error: number
  relation_total: number
  relation_approved: number
  relation_rejected: number
  relation_error: number
  results: AgentReviewResult[]
  relation_results: AgentReviewResult[]
}

export interface DocumentRunSummary {
  document_uuid: string
  document_title: string
  entity_count: number
  approved_count: number
  rejected_count: number
  escalated_count: number
  error_count: number
  avg_confidence: number
  last_review_at: string
  entities: AgentRunRecord[]
}

export const triggerDocumentReview = (documentUuid: string) =>
  http.post<any, DocumentReviewResponse>(`/agent/review/document/${documentUuid}`)

export const triggerDocumentReviewAsync = (documentUuid: string) =>
  http.post<any, { run_id: string; status: string; message: string }>(
    `/agent/review/document/${documentUuid}/async`,
  )

export const getDocumentReviewRuns = (params: { page?: number; page_size?: number }) =>
  http.get<any, { total: number; items: DocumentRunSummary[] }>('/agent/runs/by-document', { params })

// 文档审核预览 + 运行记录
export interface DocumentPreview {
  document_uuid: string
  document_title: string
  pending_count: number
}

export interface DocumentRuns {
  document_uuid: string
  document_title: string
  pending_count: number
  runs: AgentRunRecord[]
}

export type DocReviewStatus = 'pending' | 'reviewing' | 'reviewed'

export interface PendingDocument {
  document_uuid: string
  document_title: string
  source_site: string
  doc_type: string
  pending_count: number
  total_entity_count: number
  pending_relation_count: number
  total_relation_count: number
  review_status: DocReviewStatus
  created_at: string
}

export const getPendingDocuments = (params?: { page?: number; page_size?: number; status?: string }) =>
  http.get<any, { items: PendingDocument[]; total: number }>('/agent/documents/pending', { params })

export const getDocumentPreview = (documentUuid: string) =>
  http.get<any, DocumentPreview>(`/agent/review/document/${documentUuid}/preview`)

export const getDocumentRuns = (documentUuid: string) =>
  http.get<any, DocumentRuns>(`/agent/review/document/${documentUuid}/runs`)

// 关系审核
export const triggerRelationReview = (relationId: number) =>
  http.post<any, AgentReviewResult>(`/agent/review/relation/${relationId}`)

// 冲突裁决
export const triggerConflictResolution = (params: {
  merge_id: number
  dict_id: number
  entity_type: string
  entity_name: string
  field_name: string
  existing_value: string
  new_value: string
  existing_source_site?: string
  new_source_site?: string
  existing_source_doc?: string
  new_source_doc?: string
}) =>
  http.post<any, AgentReviewResult>('/agent/conflict/resolve', params)

// 语义匹配
export const triggerSemanticMatch = (params: {
  dict_id_a: number
  dict_id_b: number
  entity_a_name?: string
  entity_b_name?: string
  entity_type?: string
}) =>
  http.post<any, AgentReviewResult>('/agent/match/entity-pair', params)

// 运行记录
export const getAgentRun = (runId: string) =>
  http.get<any, AgentRunRecord>(`/agent/run/${runId}`)

export const getAgentRuns = (params: {
  agent_type?: string
  target_id?: number
  page?: number
  page_size?: number
}) =>
  http.get<any, { total: number; items: AgentRunRecord[] }>('/agent/runs', { params })

export const getEntityMergeRuns = (entityId: number) =>
  getAgentRuns({ agent_type: 'entity_merge', target_id: entityId, page_size: 10 })

export const getAgentStats = () =>
  http.get<any, AgentStats>('/agent/stats')

// 实体合并消歧
export const triggerEntityMerge = (entityId: number) =>
  http.post<any, AgentReviewResult>(`/agent/merge/entity/${entityId}`)

export interface MergeCandidate {
  id: number
  entity_name: string
  entity_type: string
  source_site: string
  source_doc_uuid: string
  document_title: string
  confidence_score: number
  entity_data: Record<string, any>
  dict_id: number | null
  dict_name: string
}

export interface MergeCandidatesParams {
  page?: number
  page_size?: number
  entity_type?: string
  source_site?: string
  search?: string
  conf_min?: number
  conf_max?: number
  has_dict?: boolean | null
}

export const getMergeCandidates = (params?: MergeCandidatesParams) =>
  http.get<any, { items: MergeCandidate[]; total: number }>('/agent/merge/candidates', { params })

export interface MergeCandidateStats {
  total: number
  by_type: Record<string, number>
  by_source: Record<string, number>
}

export const getMergeCandidatesStats = () =>
  http.get<any, MergeCandidateStats>('/agent/merge/candidates/stats')

export const triggerBatchMerge = (count: number) =>
  http.post<any, MergeQueueStatus>('/agent/merge/batch', { count })

export interface MergeQueueStatus {
  running: boolean
  pending: number
  completed: number
  failed: number
  total: number
}

export const getMergeQueueStatus = () =>
  http.get<any, MergeQueueStatus>('/agent/merge/queue/status')

// ── SSE 流式消歧 ───────────────────────────────────────────

export interface LLMMessage {
  role: string
  content?: string
  name?: string
  tool_calls?: { name: string; args: Record<string, any> }[]
}

export interface MergeStreamEvent {
  type: 'prompt' | 'llm_input' | 'status' | 'step' | 'result' | 'error' | 'done'
  run_id?: string
  step?: number
  node?: string
  content?: string
  tool_calls?: string[]
  tool_name?: string
  tool_input?: Record<string, any>
  decision?: string
  dict_id?: number | null
  merge_queue_id?: number | null
  reason?: string
  confidence?: number
  steps?: MergeStreamEvent[]
  status?: string
  message?: string
  // prompt event
  system_prompt?: string
  human_prompt?: string
  // llm_input event
  messages?: LLMMessage[]
}

export type MergeStreamCallback = (event: MergeStreamEvent) => void

export function streamEntityMerge(
  entityId: number,
  onEvent: MergeStreamCallback,
  onError?: (err: Event | string) => void,
): () => void {
  const es = new EventSource(`/api/agent/merge/entity/${entityId}/stream`)

  es.addEventListener('prompt', (e: MessageEvent) => {
    onEvent({ type: 'prompt', ...JSON.parse(e.data) })
  })
  es.addEventListener('llm_input', (e: MessageEvent) => {
    onEvent({ type: 'llm_input', ...JSON.parse(e.data) })
  })
  es.addEventListener('status', (e: MessageEvent) => {
    onEvent({ type: 'status', ...JSON.parse(e.data) })
  })
  es.addEventListener('step', (e: MessageEvent) => {
    onEvent({ type: 'step', ...JSON.parse(e.data) })
  })
  es.addEventListener('result', (e: MessageEvent) => {
    onEvent({ type: 'result', ...JSON.parse(e.data) })
  })
  es.addEventListener('done', () => {
    onEvent({ type: 'done' })
    es.close()
  })
  es.addEventListener('error', (e: MessageEvent) => {
    try {
      const data = e.data ? JSON.parse(e.data) : null
      onEvent({ type: 'error', message: data?.message || '连接异常' })
    } catch {
      onEvent({ type: 'error', message: '连接异常' })
    }
    if (onError) onError(e)
    es.close()
  })

  return () => es.close()
}

// ── 流程总览 ───────────────────────────────────────────

export interface PipelineStage {
  key: string
  label: string
  total: number
  by_status: Record<string, number>
  by_site?: Record<string, Record<string, number>>
  by_match_step?: Record<string, number>
  by_dict?: Record<string, number>
}

export interface PipelineOverview {
  stages: PipelineStage[]
}

export const getPipelineOverview = () =>
  http.get<any, PipelineOverview>('/agent/pipeline/overview')

export interface EntityReviewStats {
  pending_entities: number
  reviewed_entities: number
}

export const getEntityReviewStats = () =>
  http.get<any, EntityReviewStats>('/agent/entity-review-stats')
