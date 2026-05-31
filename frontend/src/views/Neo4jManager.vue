<template>
  <div v-loading="loading">
    <h2 style="margin-bottom:16px">Neo4j 图数据推送</h2>
    <p style="color:#909399;margin-bottom:16px;font-size:13px">
      将本库抽取的实体/关系推送到 WeKnora 的 Neo4j 拷贝库，供 WeKnora 知识图谱使用。
    </p>

    <!-- 统计卡片 -->
    <el-row :gutter="16" class="stats-row">
      <el-col :span="8">
        <el-card shadow="never" class="stat-card">
          <div class="stat-num" style="color:#4a7fa5">{{ graphStats.entity_dict.pending }}</div>
          <div class="stat-label">entity_dict 待同步</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="stat-card">
          <div class="stat-num" style="color:#0e9e8e">{{ graphStats.entities.pushed }}</div>
          <div class="stat-label">实体 已推送</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="stat-card">
          <div class="stat-num" style="color:#c0392b">{{ graphStats.relations.pending }}</div>
          <div class="stat-label">关系 待推送</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 操作 -->
    <el-card shadow="never" class="filter-card">
      <el-space wrap>
        <el-button type="primary" @click="doSyncEntityDict" :loading="syncingDict">
          同步 entity_dict
        </el-button>
        <el-button type="warning" @click="doInitCopies" :loading="initCopying">
          初始化 PrimeKG 副本
        </el-button>
        <el-button type="success" @click="doSyncEntities" :loading="syncingEnt">
          同步抽取实体 ({{ graphStats.entities.pending }})
        </el-button>
        <el-button type="success" @click="doSyncRelations" :loading="syncingRel">
          同步抽取关系 ({{ graphStats.relations.pending }})
        </el-button>
        <el-button @click="loadGraphStats">
          <el-icon><Refresh /></el-icon>刷新统计
        </el-button>
      </el-space>
    </el-card>

    <!-- 详细统计 -->
    <el-card shadow="never" style="margin-top:16px">
      <template #header>推送详情</template>
      <el-row :gutter="12">
        <el-col :span="8" v-for="item in graphDetailCards" :key="item.title">
          <div style="font-size:13px;font-weight:600;margin-bottom:6px;color:#606266">{{ item.title }}</div>
          <div style="font-size:12px;color:#909399;line-height:1.8">
            <span v-for="s in item.statuses" :key="s.label" style="margin-right:12px">
              {{ s.label }}: <strong :style="{ color: s.color }">{{ s.value }}</strong>
            </span>
          </div>
        </el-col>
      </el-row>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  syncEntityDict, initPrimeKGCopies, syncGraphEntities, syncGraphRelations, getGraphSyncStats,
} from '@/api/neo4j'

const loading = ref(false)

const graphStats = ref({
  entity_dict: { pending: 0, synced: 0, failed: 0 },
  entities: { pending: 0, pushed: 0, failed: 0 },
  relations: { pending: 0, pushed: 0, failed: 0 },
})
const syncingDict = ref(false)
const initCopying = ref(false)
const syncingEnt = ref(false)
const syncingRel = ref(false)

const graphDetailCards = computed(() => [
  {
    title: 'entity_dict 镜像表',
    statuses: [
      { label: '待同步', value: graphStats.value.entity_dict.pending, color: '#4a7fa5' },
      { label: '已同步', value: graphStats.value.entity_dict.synced, color: '#0e9e8e' },
      { label: '失败', value: graphStats.value.entity_dict.failed, color: '#c0392b' },
    ],
  },
  {
    title: '抽取实体',
    statuses: [
      { label: '待推送', value: graphStats.value.entities.pending, color: '#4a7fa5' },
      { label: '已推送', value: graphStats.value.entities.pushed, color: '#0e9e8e' },
      { label: '失败', value: graphStats.value.entities.failed, color: '#c0392b' },
    ],
  },
  {
    title: '抽取关系',
    statuses: [
      { label: '待推送', value: graphStats.value.relations.pending, color: '#4a7fa5' },
      { label: '已推送', value: graphStats.value.relations.pushed, color: '#0e9e8e' },
      { label: '失败', value: graphStats.value.relations.failed, color: '#c0392b' },
    ],
  },
])

async function loadGraphStats() {
  loading.value = true
  try {
    graphStats.value = await getGraphSyncStats()
  } catch {
    // ignore
  } finally {
    loading.value = false
  }
}

async function doSyncEntityDict() {
  syncingDict.value = true
  try {
    const res = await syncEntityDict(500)
    ElMessage.success(`entity_dict 同步完成：推送 ${res.pushed} 条`)
    loadGraphStats()
  } catch {
    ElMessage.error('entity_dict 同步失败')
  } finally {
    syncingDict.value = false
  }
}

async function doInitCopies() {
  initCopying.value = true
  try {
    const res = await initPrimeKGCopies()
    ElMessage.success(`PrimeKG 副本初始化完成：${res.pushed} 条`)
  } catch {
    ElMessage.error('PrimeKG 副本初始化失败')
  } finally {
    initCopying.value = false
  }
}

async function doSyncEntities() {
  syncingEnt.value = true
  try {
    const res = await syncGraphEntities(500)
    ElMessage.success(`实体同步完成：推送 ${res.pushed} 条`)
    loadGraphStats()
  } catch {
    ElMessage.error('实体同步失败')
  } finally {
    syncingEnt.value = false
  }
}

async function doSyncRelations() {
  syncingRel.value = true
  try {
    const res = await syncGraphRelations(500)
    ElMessage.success(`关系同步完成：推送 ${res.pushed} 条`)
    loadGraphStats()
  } catch {
    ElMessage.error('关系同步失败')
  } finally {
    syncingRel.value = false
  }
}

onMounted(() => {
  loadGraphStats()
})
</script>

<style scoped>
.stats-row { margin-bottom: 16px; }
.stat-card {
  text-align: center;
  padding: 8px 0;
  border-radius: 12px;
}
.stat-num { font-size: 28px; font-weight: 700; }
.stat-label { font-size: 12px; color: #909399; margin-top: 4px; }
.filter-card {
  margin-bottom: 16px;
  border-radius: 12px;
}
:deep(.el-card) {
  border-radius: 12px;
}
</style>
