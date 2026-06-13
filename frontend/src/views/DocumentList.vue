<template>
  <div>
    <!-- 抽取状态统计卡片 -->
    <el-row :gutter="16" class="stats-row">
      <el-col :span="6" v-for="s in statCards" :key="s.label">
        <el-card shadow="never" class="stat-card clickable-card" :class="{ 'active-card': filters.extraction_status === s.filterValue }" @click="filterByCard(s.filterValue)">
          <div class="stat-num" :style="{ color: s.color }">{{ s.value }}</div>
          <div class="stat-label">{{ s.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 筛选栏 -->
    <el-card shadow="never" class="filter-card">
      <el-form inline>
        <el-form-item label="来源站点">
          <el-select v-model="filters.source_site" clearable placeholder="全部" style="width:140px">
            <el-option v-for="s in SITES" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="文档状态">
          <el-select v-model="filters.doc_status" clearable placeholder="全部" style="width:160px">
            <el-option label="AUTO_PROCESSED" value="AUTO_PROCESSED" />
            <el-option label="PENDING_REVIEW" value="PENDING_REVIEW" />
            <el-option label="REVIEWED" value="REVIEWED" />
          </el-select>
        </el-form-item>
        <el-form-item label="抽取状态">
          <el-select v-model="filters.extraction_status" clearable placeholder="全部" style="width:130px">
            <el-option label="pending" value="pending" />
            <el-option label="queued" value="queued" />
            <el-option label="processing" value="processing" />
            <el-option label="done" value="done" />
            <el-option label="failed" value="failed" />
          </el-select>
        </el-form-item>
        <el-form-item label="发布日期">
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            value-format="YYYY-MM-DD"
            style="width:240px"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="loadData">
            <el-icon><Search /></el-icon>查询
          </el-button>
          <el-button @click="resetFilters">
            <el-icon><Refresh /></el-icon>重置
          </el-button>
          <el-button type="warning" @click="batchTrigger" :loading="triggering" :disabled="pendingSelectedCount === 0">
            批量触发抽取 ({{ pendingSelectedCount }})
          </el-button>
          <el-button type="warning" @click="batchRecrawl" :loading="batchRecrawling" :disabled="selectedUuids.length === 0">
            批量重爬 ({{ selectedUuids.length }})
          </el-button>
          <el-button type="success" @click="batchReview" :disabled="doneSelectedCount === 0">
            批量审核 ({{ doneSelectedCount }})
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 数据表格 -->
    <el-card shadow="never">
      <el-table ref="tableRef" :data="rows" v-loading="loading" @selection-change="handleSelectionChange" row-key="uuid">
        <el-table-column type="selection" width="45" reserve-selection />
        <el-table-column prop="title" label="标题" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">
            <router-link :to="{ path: `/documents/${row.uuid}`, query: $route.query }" class="title-link">
              {{ row.title || '（无标题）' }}
            </router-link>
          </template>
        </el-table-column>
        <el-table-column prop="source_site" label="来源" width="110" />
        <el-table-column prop="publish_date" label="发布日期" width="110" />
        <el-table-column label="置信度" width="90">
          <template #default="{ row }">
            <span :style="{ color: row.confidence_score < 0.8 ? 'var(--dp-danger-dark)' : 'var(--dp-success-dark)' }">
              {{ row.confidence_score.toFixed(2) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="文档状态" width="140">
          <template #default="{ row }">
            <el-tag :type="docStatusType(row.doc_status)" size="small">
              {{ row.doc_status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="抽取状态" width="110">
          <template #default="{ row }">
            <el-tag :type="extractionStatusType(row.extraction_status)" size="small">
              {{ row.extraction_status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作时间" width="80">
          <template #default="{ row }">
            <span class="relative-time">{{ formatTime(row.updated_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button size="small" plain @click="$router.push({ path: `/documents/${row.uuid}`, query: $route.query })">查看</el-button>
              <el-button
                v-if="row.extraction_status === 'pending'"
                size="small"
                type="primary"
                @click="triggerOne(row.uuid)"
              >触发抽取</el-button>
              <el-button
                v-if="row.extraction_status === 'done'"
                size="small"
                style="background-color:var(--dp-success-dark);border-color:var(--dp-success-dark);color:#fff"
                @click="$router.push({ path: `/review/${row.uuid}`, query: $route.query })"
              >审核</el-button>
              <el-button
                size="small"
                style="color:var(--dp-warning-dark);border-color:var(--dp-warning-dark)"
                :loading="recrawlingUuids.has(row.uuid)"
                @click="recrawlOne(row.uuid)"
              >重爬</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        class="pagination"
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        @change="loadData"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Refresh } from '@element-plus/icons-vue'
import { getDocuments, recrawlDocument, batchRecrawlDocuments, type DocumentItem } from '@/api/documents'
import { triggerExtraction, getExtractionStats, type ExtractionStats } from '@/api/extraction'
import { relativeTime } from '@/utils/time'

const router = useRouter()
const route = useRoute()

const SITES = ['nmpa','cde','fda_press','fda_drug_safety',
  'fda_api_drug_label','fda_api_enforcement','chictr','clinicaltrials',
  'pubmed','drugbank','hkex','fiercepharma','phirda_policy','phirda_industry',
  'phirda_rd','phirda_healthcare_reform','roche']

const rows = ref<DocumentItem[]>([])
const total = ref(0)
const page = ref(Number(route.query.page) || 1)
const pageSize = ref(Number(route.query.pageSize) || 20)
const loading = ref(false)
const triggering = ref(false)
const recrawlingUuids = ref<Set<string>>(new Set())
const dateRange = ref<[string, string] | null>(
  route.query.date_from && route.query.date_to
    ? [route.query.date_from as string, route.query.date_to as string]
    : null
)
const stats = ref<ExtractionStats>({ pending: 0, queued: 0, processing: 0, done: 0, failed: 0 })

const selectedUuids = ref<string[]>([])
const batchRecrawling = ref(false)
const tableRef = ref<any>(null)

const filters = ref({
  source_site: (route.query.source_site as string) || '',
  doc_status: (route.query.doc_status as string) || '',
  extraction_status: (route.query.extraction_status as string) || '',
})

const pendingSelectedCount = computed(() =>
  rows.value.filter(r => selectedUuids.value.includes(r.uuid) && r.extraction_status === 'pending').length
)

const doneSelectedCount = computed(() => {
  const doneUuids = new Set(
    rows.value.filter(r => r.extraction_status === 'done').map(r => r.uuid)
  )
  return selectedUuids.value.filter(u => doneUuids.has(u)).length
})

const statCards = computed(() => [
  { label: '待抽取', value: stats.value.pending, color: 'var(--dp-primary-light)', filterValue: 'pending' },
  { label: '排队中', value: stats.value.queued, color: '#8b7cf6', filterValue: 'queued' },
  { label: '抽取中', value: stats.value.processing, color: 'var(--dp-warning)', filterValue: 'processing' },
  { label: '已完成', value: stats.value.done, color: 'var(--dp-primary)', filterValue: 'done' },
  { label: '失败', value: stats.value.failed, color: 'var(--dp-danger)', filterValue: 'failed' },
])

function syncToUrl() {
  const query: Record<string, string> = {}
  if (filters.value.source_site) query.source_site = filters.value.source_site
  if (filters.value.doc_status) query.doc_status = filters.value.doc_status
  if (filters.value.extraction_status) query.extraction_status = filters.value.extraction_status
  if (dateRange.value?.[0]) query.date_from = dateRange.value[0]
  if (dateRange.value?.[1]) query.date_to = dateRange.value[1]
  if (page.value > 1) query.page = String(page.value)
  if (pageSize.value !== 20) query.pageSize = String(pageSize.value)
  router.replace({ query })
}

function filterByCard(status: string) {
  filters.value.extraction_status = status
  page.value = 1
  syncToUrl()
  loadData()
}

let isRefreshing = false  // 标记是否为轮询刷新（非用户操作）

async function loadData(refresh = false) {
  if (!refresh) syncToUrl()
  if (refresh) isRefreshing = true
  loading.value = true
  try {
    const res = await getDocuments({
      page: page.value,
      page_size: pageSize.value,
      ...filters.value,
      date_from: dateRange.value?.[0] ?? '',
      date_to: dateRange.value?.[1] ?? '',
    })
    rows.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
    if (refresh) isRefreshing = false
  }
}

async function loadStats() {
  stats.value = await getExtractionStats()
}

async function triggerOne(uuid: string) {
  await triggerExtraction([uuid])
  ElMessage.success('已触发抽取')
  loadData()
  loadStats()
}

function clearSelection() {
  tableRef.value?.clearSelection()
  selectedUuids.value = []
}

function handleSelectionChange(selection: DocumentItem[]) {
  // 轮询刷新时不更新选中状态，避免 reserve-selection 的时序问题
  if (isRefreshing) return
  selectedUuids.value = selection.map(row => row.uuid)
}

async function batchTrigger() {
  if (selectedUuids.value.length === 0) {
    ElMessage.warning('请先选择需要触发抽取的文档')
    return
  }
  triggering.value = true
  try {
    const res = await triggerExtraction(selectedUuids.value)
    ElMessage.success(res.message)
    clearSelection()
    loadData()
    loadStats()
  } finally {
    triggering.value = false
  }
}

async function batchRecrawl() {
  if (selectedUuids.value.length === 0) {
    ElMessage.warning('请先选择需要重爬的文档')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认批量重爬选中的 ${selectedUuids.value.length} 个文档？`,
      '确认批量重爬',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return // 用户点击取消
  }
  batchRecrawling.value = true
  try {
    const res = await batchRecrawlDocuments(selectedUuids.value)
    ElMessage.success(res.message)
    clearSelection()
    setTimeout(() => { loadData(); loadStats() }, 1000)
  } finally {
    batchRecrawling.value = false
  }
}

function batchReview() {
  if (selectedUuids.value.length === 0) {
    ElMessage.warning('请先选择需要审核的文档')
    return
  }
  router.push({ path: '/batch-review', query: { uuids: selectedUuids.value.join(',') } })
}

async function recrawlOne(uuid: string) {
  try {
    await ElMessageBox.confirm(
      '确认重新爬取该文档？',
      '确认重爬',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return // 用户点击取消
  }
  recrawlingUuids.value = new Set([...recrawlingUuids.value, uuid])
  try {
    const res = await recrawlDocument(uuid)
    ElMessage.success(res.message || '重爬任务已提交')
    // 延迟 1s 刷新，让后台有时间更新状态
    setTimeout(() => { loadData(); loadStats() }, 1000)
  } finally {
    recrawlingUuids.value = new Set([...recrawlingUuids.value].filter(id => id !== uuid))
  }
}

function resetFilters() {
  filters.value = { source_site: '', doc_status: '', extraction_status: '' }
  dateRange.value = null
  page.value = 1
  syncToUrl()
  loadData()
}

const docStatusType = (s: string) =>
  ({ AUTO_PROCESSED: 'success', PENDING_REVIEW: 'warning', REVIEWED: 'info' }[s] ?? '')

const extractionStatusType = (s: string) =>
  ({ pending: 'info', queued: '', processing: 'warning', done: 'success', failed: 'danger' }[s] ?? '')

const formatTime = (iso: string | null) => {
  timeTick.value
  return relativeTime(iso)
}

// 相对时间每 10 秒刷新一次
const timeTick = ref(0)
let timeTimer: ReturnType<typeof setInterval> | null = null

// 有 processing 时每 5 秒轮询
let timer: ReturnType<typeof setInterval> | null = null
watch(() => stats.value.queued + stats.value.processing, (v) => {
  if (v > 0 && !timer) {
    timer = setInterval(() => { loadData(true); loadStats() }, 5000)
  } else if (v === 0 && timer) {
    clearInterval(timer); timer = null
  }
})

onMounted(() => {
  loadData(); loadStats()
  timeTimer = setInterval(() => timeTick.value++, 10000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (timeTimer) clearInterval(timeTimer)
})
</script>

<style scoped>
.stats-row { margin-bottom: 16px; }
.stat-card {
  text-align: center;
  padding: 8px 0;
  border-radius: var(--dp-radius-xl);
}
.clickable-card {
  cursor: pointer;
  transition: all 0.2s;
}
.clickable-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--dp-shadow-md);
}
.clickable-card.active-card {
  border: 2px solid var(--dp-primary) !important;
}
.stat-num { font-size: 28px; font-weight: 700; }
.stat-label { font-size: 12px; color: var(--dp-text-secondary); margin-top: 4px; }
.filter-card {
  margin-bottom: 16px;
  border-radius: var(--dp-radius-xl);
}
.title-link { color: var(--dp-link); text-decoration: none; }
.title-link:hover { text-decoration: underline; }
.pagination { margin-top: 16px; justify-content: flex-end; }
.relative-time { font-family: monospace; font-size: 11px; color: var(--dp-text-secondary); white-space: nowrap; }

/* 表格卡片圆角 */
:deep(.el-card) {
  border-radius: var(--dp-radius-xl);
}

/* 操作列：强制一行，按钮紧凑排列 */
.action-btns {
  display: flex;
  flex-wrap: nowrap;
  gap: 4px;
  align-items: center;
}
.action-btns .el-button {
  margin: 0;
  padding: 4px 8px;
  font-size: 12px;
}
</style>
