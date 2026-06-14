import http from './http'

export interface ExtractionStats {
  pending: number
  queued: number
  processing: number
  done: number
  failed: number
}

export const triggerExtraction = (params: {
  uuids?: string[]
  source_site?: string
  doc_type?: string
  limit?: number
} = {}) =>
  http.post<any, { triggered: number; message: string }>('/extraction/trigger', {
    uuids: params.uuids ?? [],
    source_site: params.source_site ?? null,
    doc_type: params.doc_type ?? null,
    limit: params.limit ?? 10,
  })

export const getExtractionStats = () =>
  http.get<any, ExtractionStats>('/extraction/stats')

export const getExtractionStatus = (uuid: string) =>
  http.get<any, {
    uuid: string
    extraction_status: string
    extraction_model: string
    extracted_at: string | null
    extraction_error: string
  }>(`/extraction/status/${uuid}`)

export const reExtract = (uuid: string) =>
  http.post<any, { success: boolean; uuid: string; message: string }>(`/extraction/re-extract/${uuid}`)

export const triggerPendingExtraction = (params: {
  count: number
  source_site?: string
  doc_type?: string
}) =>
  http.post<any, { triggered: number; message: string }>('/extraction/trigger/pending', {
    count: params.count,
    source_site: params.source_site ?? null,
    doc_type: params.doc_type ?? null,
  })
