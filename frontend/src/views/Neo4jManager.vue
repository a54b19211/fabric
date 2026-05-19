<template>
  <div v-loading="loading">
    <h2 style="margin-bottom:16px">Neo4j 推送管理</h2>

    <!-- 统计卡片 -->
    <el-row :gutter="16" style="margin-bottom:16px">
      <el-col :span="4" v-for="c in statCards" :key="c.label">
        <el-card shadow="never" style="text-align:center">
          <div style="font-size:24px;font-weight:700" :style="{ color: c.color }">{{ c.value }}</div>
          <div style="font-size:12px;color:#909399;margin-top:4px">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 操作按钮 -->
    <el-card shadow="never" style="margin-bottom:16px">
      <el-button type="primary" @click="doSyncEntities" :loading="syncingEntities">
        批量推送实体
      </el-button>
      <el-button type="primary" @click="doSyncRelations" :loading="syncingRelations">
        批量推送关系
      </el-button>
      <el-button @click="loadAll">刷新</el-button>
    </el-card>

    <!-- 失败记录 -->
    <el-card shadow="never">
      <template #header>推送失败记录</template>
      <el-tabs v-model="failedTab">
        <el-tab-pane :label="`实体失败（${failed.entities.length}）`" name="entities">
          <el-table :data="failed.entities" size="small">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="entity_type" label="类型" width="100" />
            <el-table-column prop="entity_name" label="名称" />
            <el-table-column prop="neo4j_error" label="错误原因" show-overflow-tooltip />
            <el-table-column label="操作" width="80">
              <template #default="{ row }">
                <el-button size="small" @click="retry('entity', row.id)">重试</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="`关系失败（${failed.relations.length}）`" name="relations">
          <el-table :data="failed.relations" size="small">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="relation_type" label="关系类型" width="140" />
            <el-table-column prop="neo4j_error" label="错误原因" show-overflow-tooltip />
            <el-table-column label="操作" width="80">
              <template #default="{ row }">
                <el-button size="small" @click="retry('relation', row.id)">重试</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getNeo4jStats, getNeo4jFailed, syncEntities, syncRelations, retryNeo4j } from '@/api/neo4j'

const loading = ref(false)
const syncingEntities = ref(false)
const syncingRelations = ref(false)
const failedTab = ref('entities')

const stats = ref({ entities: { pending: 0, pushed: 0, failed: 0 }, relations: { pending: 0, pushed: 0, failed: 0 } })
const failed = ref<{ entities: any[]; relations: any[] }>({ entities: [], relations: [] })

const statCards = computed(() => [
  { label: '实体待推送', value: stats.value.entities.pending, color: '#909399' },
  { label: '实体已推送', value: stats.value.entities.pushed, color: '#67c23a' },
  { label: '实体失败', value: stats.value.entities.failed, color: '#f56c6c' },
  { label: '关系待推送', value: stats.value.relations.pending, color: '#909399' },
  { label: '关系已推送', value: stats.value.relations.pushed, color: '#67c23a' },
  { label: '关系失败', value: stats.value.relations.failed, color: '#f56c6c' },
])

async function loadAll() {
  loading.value = true
  try {
    const [s, f] = await Promise.all([getNeo4jStats(), getNeo4jFailed()])
    stats.value = s
    failed.value = f
  } finally {
    loading.value = false
  }
}

async function doSyncEntities() {
  syncingEntities.value = true
  try {
    const res = await syncEntities(100)
    ElMessage.success(`推送完成：成功 ${res.pushed}，失败 ${res.failed}`)
    loadAll()
  } finally {
    syncingEntities.value = false
  }
}

async function doSyncRelations() {
  syncingRelations.value = true
  try {
    const res = await syncRelations(100)
    ElMessage.success(`推送完成：成功 ${res.pushed}，失败 ${res.failed}`)
    loadAll()
  } finally {
    syncingRelations.value = false
  }
}

async function retry(type: 'entity' | 'relation', id: number) {
  const res = await retryNeo4j(type, id)
  if (res.success) {
    ElMessage.success('重试成功')
    loadAll()
  } else {
    ElMessage.error(`重试失败：${res.error}`)
  }
}

onMounted(loadAll)
</script>
