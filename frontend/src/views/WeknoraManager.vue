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
      <el-button type="primary" @click="batchSync" :loading="batching" :disabled="selectedUuids.length === 0">
        批量同步 ({{ selectedUuids.length }})
      </el-button>
      <el-button @click="loadAll">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
    </el-card>

    <!-- 待同步列表 -->
    <el-card shadow="never">
      <template #header>待同步文档（{{ pending.length }}）</template>
      <el-table ref="tableRef" :data="pending" size="small" row-key="uuid" @selection-change="handleSelectionChange">
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
    </el-card>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  getWeknoraPending, getWeknoraStats, syncWeknora, syncWeknoraBatch,
} from '@/api/weknora'

const loading = ref(false)
const batching = ref(false)
const stats = ref({ pending: 0, synced: 0, failed: 0 })
const pending = ref<any[]>([])
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
  // 当前页面仅显示待同步列表，切换到其他状态时显示提示
  if (key !== 'pending') {
    ElMessage.info(`${key === 'synced' ? '已同步' : '失败'}文档列表请在文档管理页面查看`)
  }
}

async function loadAll() {
  loading.value = true
  try {
    const [s, p] = await Promise.all([getWeknoraStats(), getWeknoraPending(100)])
    stats.value = s
    pending.value = p.items
  } finally {
    loading.value = false
  }
}

function handleSelectionChange(selection: any[]) {
  selectedUuids.value = selection.map(row => row.uuid)
}

async function syncOne(uuid: string) {
  const res = await syncWeknora(uuid)
  if (res.success) {
    ElMessage.success('同步成功')
    loadAll()
  } else {
    ElMessage.error(`同步失败：${res.message}`)
  }
}

async function batchSync() {
  if (selectedUuids.value.length === 0) {
    ElMessage.warning('请先选择需要同步的文档')
    return
  }
  batching.value = true
  try {
    const res = await syncWeknoraBatch(100)
    ElMessage.success(`已触发 ${res.triggered} 篇文档同步`)
    tableRef.value?.clearSelection()
    selectedUuids.value = []
    loadAll()
  } finally {
    batching.value = false
  }
}

onMounted(() => {
  loadAll()
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