<template>
  <div class="index-page">
    <el-alert type="info" :closable="false" show-icon class="intro">
      <template #title>正文进 WeKnora 只有这一条路径：index_publications 作业 + 桥接服务</template>
      <div class="intro-body">
        发表循环里的 <code>index_tick</code> 自动扫描已提交原件、排队、发送并核对回执；失败会自动退避重试（状态“待重试”）。
        连续失败 6 次的作业变为“受阻”，不再自动重发；修好原因后在下表点“重试”，作业回到“待重试”，下一轮由 <code>index_tick</code>
        处理，处理时仍先向桥接服务核对已发请求，不会重复上传。对应接口：
        <code>GET /api/history/index/status</code>、<code>GET /api/history/index/counts</code>、
        <code>POST /api/history/index/{id}/retry</code>（均需 History 管理员凭据）。
      </div>
    </el-alert>

    <!-- 凭据 -->
    <el-card shadow="never" class="token-card">
      <div v-if="!hasToken" class="token-row">
        <span class="token-label">History 管理员凭据</span>
        <el-input
          v-model="tokenInput"
          type="password"
          show-password
          placeholder="DATAFABRIC_HISTORY_ADMIN_TOKEN"
          style="width: 360px"
          @keyup.enter="saveToken"
        />
        <el-button type="primary" :disabled="!tokenInput.trim()" @click="saveToken">使用</el-button>
        <span class="token-hint">只保存在本标签页（sessionStorage），关闭即失效</span>
      </div>
      <div v-else class="token-row">
        <el-tag type="success" size="small">已填写管理员凭据（仅本标签页）</el-tag>
        <el-button size="small" @click="clearToken">清除凭据</el-button>
      </div>
    </el-card>

    <template v-if="hasToken">
      <!-- 状态计数 -->
      <div class="count-grid" v-loading="countsLoading">
        <div
          class="count-tile"
          :class="{ active: !filters.state }"
          @click="pickState(undefined)"
        >
          <div class="count-num">{{ total }}</div>
          <div class="count-label">全部</div>
        </div>
        <div
          v-for="s in INDEX_STATES"
          :key="s"
          class="count-tile"
          :class="{ active: filters.state === s, danger: s === 'blocked' && (counts[s] || 0) > 0 }"
          @click="pickState(s)"
        >
          <div class="count-num">{{ counts[s] || 0 }}</div>
          <div class="count-label">{{ STATE_META[s].label }}</div>
        </div>
      </div>
      <div v-if="countsError" class="error-line">计数读取失败：{{ countsError }}</div>

      <!-- 过滤 -->
      <el-card shadow="never" class="filter-card">
        <el-form inline>
          <el-form-item label="状态">
            <el-select v-model="filters.state" clearable placeholder="全部" style="width: 160px" @change="reload">
              <el-option v-for="s in INDEX_STATES" :key="s" :label="STATE_META[s].label + '（' + s + '）'" :value="s" />
            </el-select>
          </el-form-item>
          <el-form-item label="来源">
            <el-select
              v-model="filters.source_id"
              clearable
              filterable
              placeholder="全部"
              style="width: 260px"
              :loading="catalogLoading"
              @change="reload"
            >
              <el-option v-for="s in catalogSources" :key="s.source_id" :label="s.source_id + ' · ' + s.name" :value="s.source_id" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button :loading="loading" @click="refreshAll">刷新</el-button>
            <el-button
              type="danger"
              plain
              :disabled="!blockedInList.length"
              :loading="bulkRetrying"
              @click="retryAllBlocked"
            >
              重试列表中全部受阻（{{ blockedInList.length }}）
            </el-button>
          </el-form-item>
        </el-form>
        <div v-if="catalogError" class="error-line">来源目录读取失败：{{ catalogError }}</div>
      </el-card>

      <!-- 列表 -->
      <el-card shadow="never">
        <div v-if="listError" class="error-line">列表读取失败：{{ listError }}</div>
        <el-table :data="rows" v-loading="loading" size="small" row-key="id" empty-text="没有符合条件的索引作业">
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="STATE_META[row.state as IndexState]?.tag">
                {{ STATE_META[row.state as IndexState]?.label || row.state }}
              </el-tag>
              <div v-if="row.desired_state !== 'active'" class="sub">目标：{{ row.desired_state }}</div>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="170">
            <template #default="{ row }">
              <div>{{ row.source.source_id }}</div>
              <div class="sub">
                <el-tag v-if="row.source.is_demo" size="small" type="info">演示</el-tag>
                知识库 {{ row.source.knowledge_base_id }}
              </div>
              <el-tag v-if="!row.read_permitted" size="small" type="warning">来源已撤权</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="原件 / 版本" min-width="220">
            <template #default="{ row }">
              <div class="title" :title="row.title || ''">{{ row.title || row.source_record_id }}</div>
              <div class="sub mono">{{ row.source_record_id }}</div>
              <div class="sub mono" :title="row.version_id">版本 {{ short(row.version_id) }}</div>
              <div v-if="row.artifact_key" class="sub mono" :title="row.artifact_key">附件产物 {{ row.artifact_key }}</div>
            </template>
          </el-table-column>
          <el-table-column label="remote_id / 作业" width="170">
            <template #default="{ row }">
              <div class="mono" :title="row.remote_id">{{ short(row.remote_id) }}</div>
              <div class="sub mono" :title="row.id">作业 {{ short(row.id) }}</div>
              <div v-if="row.knowledge_id" class="sub mono" :title="row.knowledge_id">知识 {{ short(row.knowledge_id) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="错误" min-width="220">
            <template #default="{ row }">
              <template v-if="row.error">
                <div class="err-code">{{ row.error.code }}</div>
                <div class="err-msg">{{ row.error.message }}</div>
              </template>
              <span v-else class="sub">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="attempts" label="尝试" width="64" align="center" />
          <el-table-column label="更新 / 下次检查" width="170">
            <template #default="{ row }">
              <div :title="row.updated_at">{{ fmt(row.updated_at) }}</div>
              <div class="sub" :title="row.next_check_at || ''">下次 {{ fmt(row.next_check_at) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="row.state === 'blocked'"
                size="small"
                type="danger"
                :loading="retrying[row.id]"
                @click="retryOne(row)"
              >
                重试
              </el-button>
              <span v-else class="sub">—</span>
            </template>
          </el-table-column>
        </el-table>
        <div class="more-row">
          <span class="sub">已显示 {{ rows.length }} 条</span>
          <el-button v-if="nextCursor" size="small" :loading="loadingMore" @click="loadMore">加载更多</el-button>
        </div>
      </el-card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  INDEX_STATES,
  getCollectionCatalog,
  getHistoryToken,
  getIndexCounts,
  getIndexStatus,
  historyError,
  retryIndexPublication,
  setHistoryToken,
  type CatalogSource,
  type IndexPublication,
  type IndexRetryResult,
  type IndexState,
} from '@/api/history'
import { relativeTime } from '@/utils/time'

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

const STATE_META: Record<IndexState, { label: string; tag: TagType }> = {
  queued: { label: '待发送', tag: 'info' },
  sending: { label: '发送中', tag: 'primary' },
  indexing: { label: '索引中', tag: 'primary' },
  ready: { label: '已就绪', tag: 'success' },
  waiting: { label: '待重试', tag: 'warning' },
  blocked: { label: '受阻', tag: 'danger' },
  disabling: { label: '停用中', tag: 'warning' },
  disabled: { label: '已停用', tag: 'info' },
}

const PAGE_SIZE = 50

const hasToken = ref(!!getHistoryToken())
const tokenInput = ref('')

const counts = ref<Record<string, number>>({})
const total = ref(0)
const countsLoading = ref(false)
const countsError = ref('')

const catalogSources = ref<CatalogSource[]>([])
const catalogLoading = ref(false)
const catalogError = ref('')

const filters = reactive<{ state?: IndexState; source_id?: string }>({})
const rows = ref<IndexPublication[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
const listError = ref('')

const retrying = reactive<Record<string, boolean>>({})
const bulkRetrying = ref(false)

const blockedInList = computed(() => rows.value.filter((r) => r.state === 'blocked'))

function short(value: string | null | undefined) {
  if (!value) return '—'
  return value.length > 16 ? `${value.slice(0, 12)}…` : value
}

function fmt(iso: string | null | undefined) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${d.toLocaleString('zh-CN', { hour12: false })}（${relativeTime(iso)}）`
}

function saveToken() {
  const token = tokenInput.value.trim()
  if (!token) return
  setHistoryToken(token)
  tokenInput.value = ''
  hasToken.value = true
  loadCatalog()
  refreshAll()
}

function clearToken() {
  setHistoryToken('')
  hasToken.value = false
  rows.value = []
  counts.value = {}
  total.value = 0
  catalogSources.value = []
}

/** 403/401 说明凭据不对：清掉，让操作者重新填写。 */
function handleAuth(err: unknown) {
  const status = (err as { response?: { status?: number } })?.response?.status
  if (status === 401 || status === 403) {
    clearToken()
    ElMessage.error('凭据无效或不是 History 管理员凭据，请重新填写')
  }
}

async function loadCatalog() {
  catalogLoading.value = true
  catalogError.value = ''
  try {
    const catalog = await getCollectionCatalog()
    catalogSources.value = catalog.sources
  } catch (err) {
    catalogError.value = historyError(err)
  } finally {
    catalogLoading.value = false
  }
}

async function loadCounts() {
  countsLoading.value = true
  countsError.value = ''
  try {
    const res = await getIndexCounts()
    counts.value = res.states
    total.value = res.total
  } catch (err) {
    countsError.value = historyError(err)
    handleAuth(err)
  } finally {
    countsLoading.value = false
  }
}

function params(cursor?: string) {
  return {
    limit: PAGE_SIZE,
    state: filters.state || undefined,
    source_id: filters.source_id || undefined,
    cursor,
  }
}

async function reload() {
  loading.value = true
  listError.value = ''
  try {
    const page = await getIndexStatus(params())
    rows.value = page.items
    nextCursor.value = page.next_cursor
  } catch (err) {
    rows.value = []
    nextCursor.value = null
    listError.value = historyError(err)
    handleAuth(err)
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (!nextCursor.value) return
  loadingMore.value = true
  try {
    const page = await getIndexStatus(params(nextCursor.value))
    const seen = new Set(rows.value.map((r) => r.id))
    rows.value = rows.value.concat(page.items.filter((r) => !seen.has(r.id)))
    nextCursor.value = page.next_cursor
  } catch (err) {
    listError.value = historyError(err)
    handleAuth(err)
  } finally {
    loadingMore.value = false
  }
}

function refreshAll() {
  loadCounts()
  reload()
}

function pickState(state: IndexState | undefined) {
  filters.state = state
  reload()
}

function describe(result: IndexRetryResult) {
  if (result.scheduled) return '已放回“待重试”，发表循环的下一轮会处理'
  if (result.reason === 'leased') return '作业正被处理中，稍后再试'
  return `作业当前是“${STATE_META[result.state as IndexState]?.label || result.state}”，不是受阻，未改动`
}

async function retryOne(row: IndexPublication) {
  retrying[row.id] = true
  try {
    const result = await retryIndexPublication(row.id)
    if (result.scheduled) ElMessage.success(describe(result))
    else ElMessage.warning(describe(result))
    refreshAll()
  } catch (err) {
    ElMessage.error(`重试失败：${historyError(err)}`)
    handleAuth(err)
  } finally {
    retrying[row.id] = false
  }
}

async function retryAllBlocked() {
  const targets = blockedInList.value.slice()
  if (!targets.length) return
  try {
    await ElMessageBox.confirm(
      `把当前列表中的 ${targets.length} 条受阻作业放回“待重试”？请先确认失败原因已修好。`,
      '批量重试',
      { type: 'warning', confirmButtonText: '重试', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  bulkRetrying.value = true
  let scheduled = 0
  const skipped: string[] = []
  try {
    for (const row of targets) {
      try {
        const result = await retryIndexPublication(row.id)
        if (result.scheduled) scheduled += 1
        else skipped.push(`${short(row.id)}：${describe(result)}`)
      } catch (err) {
        skipped.push(`${short(row.id)}：${historyError(err)}`)
        handleAuth(err)
        if (!hasToken.value) break
      }
    }
  } finally {
    bulkRetrying.value = false
  }
  if (skipped.length) ElMessage.warning(`已放回 ${scheduled} 条；${skipped.length} 条未改动：${skipped.slice(0, 3).join('；')}`)
  else ElMessage.success(`已放回 ${scheduled} 条，发表循环的下一轮会处理`)
  refreshAll()
}

onMounted(() => {
  if (hasToken.value) {
    loadCatalog()
    refreshAll()
  }
})
</script>

<style scoped>
.index-page { display: flex; flex-direction: column; gap: 12px; }
.intro-body { line-height: 1.7; font-size: 13px; }
.intro-body code { background: var(--el-fill-color-light); padding: 0 4px; border-radius: 3px; }
.token-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.token-label { font-weight: 600; }
.token-hint { color: var(--el-text-color-secondary); font-size: 12px; }
.count-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; }
.count-tile {
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  padding: 10px 12px;
  cursor: pointer;
  background: var(--el-bg-color);
}
.count-tile.active { border-color: var(--el-color-primary); box-shadow: 0 0 0 1px var(--el-color-primary) inset; }
.count-tile.danger .count-num { color: var(--el-color-danger); }
.count-num { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.count-label { font-size: 12px; color: var(--el-text-color-secondary); }
.filter-card :deep(.el-form-item) { margin-bottom: 0; }
.error-line { color: var(--el-color-danger); font-size: 13px; margin: 6px 0; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.err-code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--el-color-danger); }
.err-msg { font-size: 12px; }
.more-row { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; }
</style>
