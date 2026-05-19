<template>
  <div>
    <!-- 抽取状态统计卡片 -->
    <el-row :gutter="16" class="stats-row">
      <el-col :span="6" v-for="s in statCards" :key="s.label">
        <el-card shadow="never" class="stat-card">
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
          <el-button type="primary" @click="loadData">查询</el-button>
          <el-button @click="resetFilters">重置</el-button>
          <el-button type="warning" @click="batchTrigger" :loading="triggering">
            批量触发抽取
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 数据表格 -->
    <el-card shadow="never">
      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column prop="title" label="标题" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">
            <router-link :to="`/documents/${row.uuid}`" class="title-link">
              {{ row.title || '（无标题）' }}
            </router-link>
          </template>
        </el-table-column>
        <el-table-column prop="source_site" label="来源" width="110" />
        <el-table-column prop="publish_date" label="发布日期" width="110" />
        <el-table-column label="置信度" width="90">
          <template #default="{ row }">
            <span :style="{ color: row.confidence_score < 0.8 ? '#f56c6c' : '#67c23a' }">
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
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="$router.push(`/documents/${row.uuid}`)">查看</el-button>
            <el-button
              v-if="row.extraction_status === 'pending'"
              size="small"
              type="primary"
              @click="triggerOne(row.uuid)"
            >触发抽取</el-button>
            <el-button
              v-if="row.extraction_status === 'done'"
              size="small"
              type="success"
              @click="$router.push(`/review/${row.uuid}`)"
            >审核</el-button>
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
import { ElMessage } from 'element-plus'
import { getDocuments, type DocumentItem } from '@/api/documents'
import { triggerExtraction, getExtractionStats, type ExtractionStats } from '@/api/extraction'

const SITES = ['nmpa','cde','fda_press','fda_drug_safety','fda_drug_recalls',
  'fda_api_drug_label','fda_api_enforcement','chictr','clinicaltrials',
  'pubmed','drugbank','hkex','fiercepharma','phirda_policy','phirda_industry',
  'phirda_rd','phirda_healthcare_reform','roche']

const rows = ref<DocumentItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const triggering = ref(false)
const dateRange = ref<[string, string] | null>(null)
const stats = ref<ExtractionStats>({ pending: 0, processing: 0, done: 0, failed: 0 })

const filters = ref({
  source_site: '',
  doc_status: '',
  extraction_status: '',
})

const statCards = computed(() => [
  { label: '待抽取', value: stats.value.pending, color: '#909399' },
  { label: '抽取中', value: stats.value.processing, color: '#e6a23c' },
  { label: '已完成', value: stats.value.done, color: '#67c23a' },
  { label: '失败', value: stats.value.failed, color: '#f56c6c' },
])

async function loadData() {
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

async function batchTrigger() {
  triggering.value = true
  try {
    const res = await triggerExtraction([], 100)
    ElMessage.success(res.message)
    loadData()
    loadStats()
  } finally {
    triggering.value = false
  }
}

function resetFilters() {
  filters.value = { source_site: '', doc_status: '', extraction_status: '' }
  dateRange.value = null
  page.value = 1
  loadData()
}

const docStatusType = (s: string) =>
  ({ AUTO_PROCESSED: 'success', PENDING_REVIEW: 'warning', REVIEWED: 'info' }[s] ?? '')

const extractionStatusType = (s: string) =>
  ({ pending: 'info', processing: 'warning', done: 'success', failed: 'danger' }[s] ?? '')

// 有 processing 时每 5 秒轮询
let timer: ReturnType<typeof setInterval> | null = null
watch(() => stats.value.processing, (v) => {
  if (v > 0 && !timer) {
    timer = setInterval(() => { loadData(); loadStats() }, 5000)
  } else if (v === 0 && timer) {
    clearInterval(timer); timer = null
  }
})

onMounted(() => { loadData(); loadStats() })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.stats-row { margin-bottom: 16px; }
.stat-card { text-align: center; padding: 8px 0; }
.stat-num { font-size: 28px; font-weight: 700; }
.stat-label { font-size: 12px; color: #909399; margin-top: 4px; }
.filter-card { margin-bottom: 16px; }
.title-link { color: #409eff; text-decoration: none; }
.title-link:hover { text-decoration: underline; }
.pagination { margin-top: 16px; justify-content: flex-end; }
</style>
