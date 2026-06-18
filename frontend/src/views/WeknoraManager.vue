<template>
  <div v-loading="loading">
    <h2 style="margin-bottom:16px">WeKnora 同步管理</h2>

    <!-- 统计卡片 -->
    <el-row :gutter="16" class="stats-row">
      <el-col :span="8" v-for="c in statCards" :key="c.label">
        <el-card shadow="never" class="stat-card clickable-card" :class="{ 'active-card': c.key === activeCard }" @click="filterByCard(c.key)">
          <div class="stat-num" :style="{ color: c.color }">{{ c.value }}</div>
          <div class="stat-label">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 操作 -->
    <el-card shadow="never" class="filter-card">
      <el-row :gutter="12" align="middle">
        <el-col :span="5">
          <el-input
            v-model="kbId"
            placeholder="知识库 ID（留空用 .env 配置）"
            clearable
            size="default"
          />
        </el-col>
        <el-col :span="4">
          <el-select v-model="sourceSite" placeholder="来源站点" clearable size="default" style="width:100%">
            <el-option v-for="s in SITES" :key="s" :label="s" :value="s" />
          </el-select>
        </el-col>
        <el-col :span="15">
          <el-space>
            <el-button type="primary" @click="batchSyncBySelect" :loading="batching" :disabled="selectedUuids.length === 0">
              选中同步 ({{ selectedUuids.length }})
            </el-button>
            <el-input-number v-model="batchCount" :min="1" :max="5000" :step="100" style="width:150px" />
            <el-button type="success" plain @click="batchSyncByCount" :loading="batchingByCount">
              批量同步
            </el-button>
            <el-button @click="loadData">
              <el-icon><Refresh /></el-icon>刷新
            </el-button>
          </el-space>
        </el-col>
      </el-row>
    </el-card>

    <!-- 待同步列表 -->
    <el-card shadow="never">
      <template #header>待同步文档（{{ total }}）</template>
      <el-table ref="tableRef" :data="pending" v-loading="loading" size="small" row-key="uuid" @selection-change="handleSelectionChange">
        <el-table-column type="selection" width="45" reserve-selection />
        <el-table-column prop="title" label="标题" show-overflow-tooltip />
        <el-table-column prop="source_site" label="来源" width="110" />
        <el-table-column prop="publish_date" label="发布日期" width="111" />
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button size="small" type="primary" @click="syncOne(row.uuid)">同步</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div style="margin-top:12px;display:flex;justify-content:flex-end">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[20, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          small
          @current-change="loadData"
          @size-change="loadData"
        />
      </div>
    </el-card>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  getWeknoraPending, getWeknoraStats, syncWeknora, syncWeknoraBatch,
} from '@/api/weknora'

const SITES = ['nmpa','cde','cde_zdyz','cde_xxgk',
  'fda_press','fda_drug_safety','fda_api_drug_label','fda_api_enforcement',
  'chictr','clinicaltrials','pubmed','drugbank','hkex','fiercepharma',
  'phirda_policy','phirda_industry','phirda_rd','phirda_healthcare_reform','roche']

const loading = ref(false)
const batching = ref(false)
const batchingByCount = ref(false)
const kbId = ref('')
const sourceSite = ref('')
const batchCount = ref(100)
const stats = ref({ pending: 0, synced: 0, failed: 0 })
const pending = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const tableRef = ref<any>(null)
const selectedUuids = ref<string[]>([])

const activeCard = ref('pending')

const statCards = computed(() => [
  { label: '待同步', value: stats.value.pending, color: 'var(--dp-info)', key: 'pending' },
  { label: '已同步', value: stats.value.synced,  color: 'var(--dp-success-dark)', key: 'synced' },
  { label: '失败',   value: stats.value.failed,  color: 'var(--dp-danger-dark)', key: 'failed' },
])

function filterByCard(key: string) {
  activeCard.value = key
  if (key !== 'pending') {
    ElMessage.info(`${key === 'synced' ? '已同步' : '失败'}文档列表请在文档管理页面查看`)
  }
}

async function loadData() {
  loading.value = true
  try {
    const offset = (page.value - 1) * pageSize.value
    const [s, p] = await Promise.all([
      getWeknoraStats(),
      getWeknoraPending(pageSize.value, offset, sourceSite.value),
    ])
    stats.value = s
    pending.value = p.items
    total.value = p.total
  } finally {
    loading.value = false
  }
}

function handleSelectionChange(selection: any[]) {
  selectedUuids.value = selection.map(row => row.uuid)
}

async function syncOne(uuid: string) {
  const res = await syncWeknora(uuid, kbId.value)
  if (res.success) {
    ElMessage.success('同步成功')
    loadData()
  } else {
    ElMessage.error(`同步失败：${res.message}`)
  }
}

async function batchSyncBySelect() {
  if (selectedUuids.value.length === 0) {
    ElMessage.warning('请先选择需要同步的文档')
    return
  }
  batching.value = true
  try {
    let ok = 0
    for (const uuid of selectedUuids.value) {
      const res = await syncWeknora(uuid, kbId.value)
      if (res.success) ok++
    }
    ElMessage.success(`同步完成：${ok}/${selectedUuids.value.length}`)
    tableRef.value?.clearSelection()
    selectedUuids.value = []
    loadData()
  } finally {
    batching.value = false
  }
}

async function batchSyncByCount() {
  const source = sourceSite.value || '全部'
  try {
    await ElMessageBox.confirm(
      `确认按数量同步 ${batchCount.value} 篇？\n来源站点: ${source}`,
      '确认批量同步',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  batchingByCount.value = true
  try {
    const res = await syncWeknoraBatch(batchCount.value, kbId.value, sourceSite.value)
    ElMessage.success(`已触发 ${res.triggered} 篇文档同步`)
    loadData()
  } finally {
    batchingByCount.value = false
  }
}

// 切换来源站点时重置页码并刷新
watch(sourceSite, () => {
  page.value = 1
  loadData()
})

onMounted(() => {
  loadData()
})
</script>

<style scoped>
.stats-row { margin-bottom: 16px; }
.stat-card {
  text-align: center;
  padding: 8px 0;
  border-radius: 12px;
}
.clickable-card {
  cursor: pointer;
  transition: all 0.2s;
}
.clickable-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}
.active-card {
  border: 2px solid var(--dp-link);
}
.stat-num { font-size: 28px; font-weight: 700; }
.stat-label { font-size: 12px; color: var(--dp-text-secondary); margin-top: 4px; }
.filter-card {
  margin-bottom: 16px;
  border-radius: 12px;
}
:deep(.el-card) {
  border-radius: 12px;
}
</style>