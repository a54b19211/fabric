<template>
  <div v-loading="loading">
    <h2 style="margin-bottom:16px">Neo4j 推送管理</h2>

    <!-- 统计卡片 -->
    <el-row :gutter="16" class="stats-row">
        <el-col :span="4" v-for="c in statCards" :key="c.label">
        <el-card shadow="never" class="stat-card clickable-card" :class="{ 'active-card': c.tabKey === failedTab }" @click="filterByCard(c)">
          <div class="stat-num" :style="{ color: c.color }">{{ c.value }}</div>
          <div class="stat-label">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 操作按钮 -->
    <el-card shadow="never" class="filter-card">
      <el-button type="primary" @click="doSyncEntities" :loading="syncingEntities" :disabled="selectedEntityIds.length === 0">
        批量推送实体 ({{ selectedEntityIds.length }})
      </el-button>
      <el-button type="primary" @click="doSyncRelations" :loading="syncingRelations" :disabled="selectedRelationIds.length === 0">
        批量推送关系 ({{ selectedRelationIds.length }})
      </el-button>
      <el-button @click="loadAll">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
    </el-card>

    <!-- 失败记录 -->
    <el-card shadow="never">
      <template #header>推送失败记录</template>
      <el-tabs v-model="failedTab">
        <el-tab-pane :label="`实体失败（${failed.entities.length}）`" name="entities">
          <el-table ref="entityTableRef" :data="failed.entities" size="small" row-key="id" @selection-change="handleEntitySelectionChange">
            <el-table-column type="selection" width="45" reserve-selection />
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
          <el-table ref="relationTableRef" :data="failed.relations" size="small" row-key="id" @selection-change="handleRelationSelectionChange">
            <el-table-column type="selection" width="45" reserve-selection />
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
import { Refresh } from '@element-plus/icons-vue'
import { getNeo4jStats, getNeo4jFailed, syncEntities, syncRelations, retryNeo4j } from '@/api/neo4j'

const loading = ref(false)
const syncingEntities = ref(false)
const syncingRelations = ref(false)
const failedTab = ref('entities')

const entityTableRef = ref<any>(null)
const relationTableRef = ref<any>(null)
const selectedEntityIds = ref<number[]>([])
const selectedRelationIds = ref<number[]>([])

const stats = ref({ entities: { pending: 0, pushed: 0, failed: 0 }, relations: { pending: 0, pushed: 0, failed: 0 } })
const failed = ref<{ entities: any[]; relations: any[] }>({ entities: [], relations: [] })

const statCards = computed(() => [
  { label: '实体待推送', value: stats.value.entities.pending, color: '#4a7fa5', tabKey: 'entities' },
  { label: '实体已推送', value: stats.value.entities.pushed, color: '#0e9e8e', tabKey: 'entities' },
  { label: '实体失败',   value: stats.value.entities.failed, color: '#c0392b', tabKey: 'entities' },
  { label: '关系待推送', value: stats.value.relations.pending, color: '#4a7fa5', tabKey: 'relations' },
  { label: '关系已推送', value: stats.value.relations.pushed, color: '#0e9e8e', tabKey: 'relations' },
  { label: '关系失败',   value: stats.value.relations.failed, color: '#c0392b', tabKey: 'relations' },
])

function filterByCard(card: { tabKey: string }) {
  failedTab.value = card.tabKey
}

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

function handleEntitySelectionChange(selection: any[]) {
  selectedEntityIds.value = selection.map(row => row.id)
}

function handleRelationSelectionChange(selection: any[]) {
  selectedRelationIds.value = selection.map(row => row.id)
}

async function doSyncEntities() {
  if (selectedEntityIds.value.length === 0) {
    ElMessage.warning('请先选择需要推送的实体')
    return
  }
  syncingEntities.value = true
  try {
    // 逐个重试选中的实体
    let success = 0, fail = 0
    for (const id of selectedEntityIds.value) {
      const res = await retryNeo4j('entity', id)
      if (res.success) success++
      else fail++
    }
    ElMessage.success(`推送完成：成功 ${success}，失败 ${fail}`)
    entityTableRef.value?.clearSelection()
    selectedEntityIds.value = []
    loadAll()
  } finally {
    syncingEntities.value = false
  }
}

async function doSyncRelations() {
  if (selectedRelationIds.value.length === 0) {
    ElMessage.warning('请先选择需要推送的关系')
    return
  }
  syncingRelations.value = true
  try {
    let success = 0, fail = 0
    for (const id of selectedRelationIds.value) {
      const res = await retryNeo4j('relation', id)
      if (res.success) success++
      else fail++
    }
    ElMessage.success(`推送完成：成功 ${success}，失败 ${fail}`)
    relationTableRef.value?.clearSelection()
    selectedRelationIds.value = []
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
  border: 2px solid #409eff;
}
.stat-num { font-size: 24px; font-weight: 700; }
.stat-label { font-size: 12px; color: #909399; margin-top: 4px; }
.filter-card {
  margin-bottom: 16px;
  border-radius: 12px;
}
:deep(.el-card) {
  border-radius: 12px;
}
</style>