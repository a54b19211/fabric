import axios from 'axios'

// ── History 服务（/api/history）：正文索引发布管理、来源目录 ─────────────────
// 这些接口用 Bearer 凭据（DATAFABRIC_HISTORY_ADMIN_TOKEN / READ_TOKEN），不走 /api 的无凭据封装。
// 凭据只放在本标签页的 sessionStorage，关闭标签页即失效；不写进 URL、不写进 localStorage。

const TOKEN_KEY = 'datafabric.history.token'
// 存储不可用（隐私窗口等）时退回内存，刷新后需重新填写
let memoryToken = ''

export function getHistoryToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || memoryToken
  } catch {
    return memoryToken
  }
}

export function setHistoryToken(token: string) {
  memoryToken = token
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // 只留在内存
  }
}

const historyHttp = axios.create({ baseURL: '/api/history', timeout: 30000 })

historyHttp.interceptors.request.use((config) => {
  const token = getHistoryToken()
  if (!token) return Promise.reject(new Error('需要历史服务凭据'))
  config.headers.Authorization = `Bearer ${token}`
  return config
})

historyHttp.interceptors.response.use((res) => res.data)

/** 把 History 接口的错误（detail: {code, message}）转成一行可读文字。 */
export function historyError(err: unknown): string {
  const e = err as { response?: { status?: number; data?: { detail?: unknown } }; message?: string }
  const detail = e?.response?.data?.detail
  if (Array.isArray(detail)) return '请求参数无效'
  if (detail && typeof detail === 'object') {
    const d = detail as { code?: string; message?: string }
    if (d.message) return d.code ? `${d.message}（${d.code}）` : d.message
  }
  if (e?.response?.status) return `请求失败（HTTP ${e.response.status}）`
  return e?.message || '请求失败'
}

// ── 来源目录（collection/catalog 唯一来源列表）──────────────────────────────
export interface CatalogSource {
  source_id: string
  name: string
  status: 'active' | 'declared' | 'blocked'
  plugin: string | null
  fetchable: boolean
  watchable: boolean
  verified: boolean
  blocked_reason: string | null
}

export interface CollectionCatalog {
  schema_version: string
  sources: CatalogSource[]
  gaps: unknown[]
}

export const getCollectionCatalog = () =>
  historyHttp.get<any, CollectionCatalog>('/collection/catalog')

// ── 正文索引发布（index_publications，正文进 WeKnora 的唯一路径）────────────
export const INDEX_STATES = [
  'queued', 'sending', 'indexing', 'ready', 'waiting', 'blocked', 'disabling', 'disabled',
] as const
export type IndexState = (typeof INDEX_STATES)[number]

export interface IndexPublication {
  id: string
  target_id: string
  version_id: string
  source_record_id: string
  remote_id: string
  state: IndexState
  desired_state: string
  knowledge_id: string | null
  receipt: Record<string, unknown> | null
  error: { code?: string; message?: string } | null
  attempts: number
  first_send_at: string | null
  next_check_at: string | null
  updated_at: string
  artifact_key: string
  title: string | null
  source: {
    source_id: string
    is_demo: boolean
    knowledge_base_id: string
    source_scope: string
    source_instance: string
  }
  read_permitted: boolean
}

export interface IndexStatusPage {
  items: IndexPublication[]
  next_cursor: string | null
}

export interface IndexStatusParams {
  limit?: number
  state?: IndexState
  source_id?: string
  cursor?: string
}

export const getIndexStatus = (params: IndexStatusParams) =>
  historyHttp.get<any, IndexStatusPage>('/index/status', { params })

export const getIndexCounts = () =>
  historyHttp.get<any, { states: Record<string, number>; total: number }>('/index/counts')

export interface IndexRetryResult {
  id: string
  scheduled: boolean
  state: string
  reason?: 'not_blocked' | 'leased'
}

export const retryIndexPublication = (id: string) =>
  historyHttp.post<any, IndexRetryResult>(`/index/${encodeURIComponent(id)}/retry`)
