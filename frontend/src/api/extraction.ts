import http from './http'

export interface ExtractionStats {
  pending: number
  queued: number
  processing: number
  done: number
  failed: number
}

export const triggerExtraction = (uuids: string[] = [], limit = 10) =>
  http.post<any, { triggered: number; message: string }>('/extraction/trigger', { uuids, limit })

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
