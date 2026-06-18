import http from './http'

export interface DocumentItem {
  id: number
  uuid: string
  title: string
  source_site: string
  publish_date: string
  doc_status: string
  confidence_score: number
  extraction_status: string
  weknora_sync_status: string
  updated_at: string | null
}

export interface DocumentDetail extends DocumentItem {
  raw_content: string
  parse_method: string
  doc_type: string
  language: string
  extra: Record<string, any>
  attachments: { file_name: string; file_url: string; local_path: string }[]
  extraction_model: string
  extraction_error: string
  extracted_at: string | null
}

export interface DocumentListParams {
  page?: number
  page_size?: number
  source_site?: string
  doc_status?: string
  extraction_status?: string
  date_from?: string
  date_to?: string
}

export const getSourceSites = () =>
  http.get<any, { sites: string[] }>('/documents/sites')

export const getDocuments = (params: DocumentListParams) =>
  http.get<any, { total: number; page: number; page_size: number; items: DocumentItem[] }>(
    '/documents', { params }
  )

export const getDocument = (uuid: string) =>
  http.get<any, DocumentDetail>(`/documents/${uuid}`)

export const recrawlDocument = (uuid: string) =>
  http.post<any, { success: boolean; uuid: string; message: string }>(
    `/documents/${uuid}/recrawl`, {}
  )

export const batchRecrawlDocuments = (uuids: string[]) =>
  http.post<any, { success: boolean; triggered: number; skipped: number; message: string }>(
    '/documents/batch-recrawl', { uuids }
  )

export const uploadDocument = (file: File, source_site?: string) => {
  const form = new FormData()
  form.append('file', file)
  if (source_site) form.append('source_site', source_site)
  return http.post<any, { success: boolean; uuid: string; title: string; message: string }>(
    '/documents/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' }
    }
  )
}

export interface BatchUploadItem {
  filename: string
  uuid: string
  title: string
  success: boolean
  error: string
}

export const batchUploadDocuments = (files: File[], source_site?: string) => {
  const form = new FormData()
  files.forEach(f => form.append('files', f))
  if (source_site) form.append('source_site', source_site)
  return http.post<any, { total: number; succeeded: number; failed: number; items: BatchUploadItem[] }>(
    '/documents/upload/batch', form, {
      headers: { 'Content-Type': 'multipart/form-data' }
    }
  )
}
