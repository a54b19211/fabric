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

export const getDocuments = (params: DocumentListParams) =>
  http.get<any, { total: number; page: number; page_size: number; items: DocumentItem[] }>(
    '/documents', { params }
  )

export const getDocument = (uuid: string) =>
  http.get<any, DocumentDetail>(`/documents/${uuid}`)
