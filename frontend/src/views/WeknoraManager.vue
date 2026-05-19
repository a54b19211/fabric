<template>
  <div v-loading="loading">
    <h2 style="margin-bottom:16px">WeKnora 同步管理</h2>

    <!-- 统计卡片 -->
    <el-row :gutter="16" style="margin-bottom:16px">
      <el-col :span="8" v-for="c in statCards" :key="c.label">
        <el-card shadow="never" style="text-align:center">
          <div style="font-size:28px;font-weight:700" :style="{ color: c.color }">{{ c.value }}</div>
          <div style="font-size:12px;color:#909399;margin-top:4px">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 操作 -->
    <el-card shadow="never" style="margin-bottom:16px">
      <el-button type="primary" @click="batchSync" :loading="batching">
        批量同步（100条）
      </el-button>
      <el-button @click="loadAll">刷新</el-button>
    </el-card>

    <!-- 待同步列表 -->
    <el-card shadow="never">
      <template #header>待同步文档（{{ pending.length }}）</template>
      <el-table :data="pending" size="small">
        <el-table-column prop="title" label="标题" show-overflow-tooltip />
        <el-table-column prop="source_site" label="来源" width="110" />
        <el-table-column prop="publish_date" label="发布日期" width="110" />
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
import { getWeknoraPending, getWeknoraStats, syncWeknora, syncWeknoraBatch } from '@/api/weknora'

const loading = ref(false)
const batching = ref(false)
const stats = ref({ pending: 0, synced: 0, failed: 0 })
const pending = ref<any[]>([])

const statCards = computed(() => [
  { label: '待同步', value: stats.value.pending, color: '#909399' },
  { label: '已同步', value: stats.value.synced, color: '#67c23a' },
  { label: '失败', value: stats.value.failed, color: '#f56c6c' },
])

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
  batching.value = true
  try {
    const res = await syncWeknoraBatch(100)
    ElMessage.success(`已触发 ${res.triggered} 篇文档同步`)
    loadAll()
  } finally {
    batching.value = false
  }
}

onMounted(loadAll)
</script>
