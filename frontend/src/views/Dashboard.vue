<template>
  <div v-loading="loading">
    <h2 style="margin-bottom:16px">数据统计仪表盘</h2>

    <el-row :gutter="16" style="margin-bottom:16px">
      <el-col :span="4" v-for="c in statCards" :key="c.label">
        <el-card shadow="never" class="stat-card">
          <div class="stat-num" :style="{ color: c.color }">{{ c.value }}</div>
          <div class="stat-label">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>来源分布</template>
          <div v-for="s in data.source_distribution" :key="s.site" class="source-row">
            <span class="source-site">{{ s.site || 'unknown' }}</span>
            <el-progress
              :percentage="Math.round(s.count / maxSourceCount * 100)"
              :color="'#409eff'"
              :show-text="false"
              style="flex:1;margin:0 12px"
            />
            <span class="source-count">{{ s.count }}</span>
          </div>
          <el-empty v-if="!data.source_distribution?.length" description="暂无数据" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>流水线状态</template>
          <el-row :gutter="12">
            <el-col :span="12" style="text-align:center;margin-bottom:16px">
              <div class="pipeline-num" style="color:#909399">{{ data.extraction_pending }}</div>
              <div class="pipeline-label">待抽取</div>
            </el-col>
            <el-col :span="12" style="text-align:center;margin-bottom:16px">
              <div class="pipeline-num" style="color:#67c23a">{{ data.extraction_done }}</div>
              <div class="pipeline-label">抽取完成</div>
            </el-col>
            <el-col :span="8" style="text-align:center">
              <div class="pipeline-num" style="color:#909399">{{ data.weknora_pending }}</div>
              <div class="pipeline-label">WeKnora 待同步</div>
            </el-col>
            <el-col :span="8" style="text-align:center">
              <div class="pipeline-num" style="color:#67c23a">{{ data.weknora_synced }}</div>
              <div class="pipeline-label">WeKnora 已同步</div>
            </el-col>
            <el-col :span="8" style="text-align:center">
              <div class="pipeline-num" style="color:#f56c6c">{{ data.weknora_failed }}</div>
              <div class="pipeline-label">WeKnora 失败</div>
            </el-col>
          </el-row>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>审核 & 图谱</template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="审核通过率">
              <span :style="{ color: data.review_pass_rate >= 0.7 ? '#67c23a' : '#f56c6c' }">
                {{ (data.review_pass_rate * 100).toFixed(1) }}%
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="平均实体置信度">
              {{ (data.avg_entity_confidence * 100).toFixed(1) }}%
            </el-descriptions-item>
            <el-descriptions-item label="实体总数">{{ data.entity_count }}</el-descriptions-item>
            <el-descriptions-item label="关系总数">{{ data.relation_count }}</el-descriptions-item>
            <el-descriptions-item label="Neo4j 待推送实体">{{ data.neo4j_pending_entities }}</el-descriptions-item>
            <el-descriptions-item label="Neo4j 待推送关系">{{ data.neo4j_pending_relations }}</el-descriptions-item>
            <el-descriptions-item label="Neo4j 推送失败总数">
              <span :style="{ color: data.neo4j_failed_total > 0 ? '#f56c6c' : '#67c23a' }">
                {{ data.neo4j_failed_total }}
              </span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { getDashboard, type DashboardData } from '@/api/stats'

const loading = ref(false)
const data = ref<DashboardData>({
  total_documents: 0, today_new: 0, source_distribution: [],
  entity_count: 0, relation_count: 0, avg_entity_confidence: 0,
  review_pass_rate: 0, neo4j_pending_entities: 0, neo4j_pending_relations: 0,
  neo4j_failed_total: 0, weknora_pending: 0, weknora_synced: 0, weknora_failed: 0,
  extraction_pending: 0, extraction_done: 0,
})

const maxSourceCount = computed(() => {
  if (!data.value.source_distribution.length) return 1
  return Math.max(...data.value.source_distribution.map(s => s.count))
})

const statCards = computed(() => [
  { label: '文档总数', value: data.value.total_documents, color: '#409eff' },
  { label: '今日新增', value: data.value.today_new, color: '#67c23a' },
  { label: '实体总数', value: data.value.entity_count, color: '#e6a23c' },
  { label: '关系总数', value: data.value.relation_count, color: '#e6a23c' },
  { label: '待抽取', value: data.value.extraction_pending, color: '#909399' },
  { label: '通过率', value: (data.value.review_pass_rate * 100).toFixed(1) + '%', color: '#67c23a' },
])

onMounted(async () => {
  loading.value = true
  try {
    data.value = await getDashboard()
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.stat-card { text-align: center; padding: 8px 0; }
.stat-num { font-size: 28px; font-weight: 700; }
.stat-label { font-size: 12px; color: #909399; margin-top: 4px; }
.source-row { display: flex; align-items: center; padding: 4px 0; }
.source-site { width: 110px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.source-count { width: 50px; text-align: right; font-size: 13px; color: #909399; }
.pipeline-num { font-size: 24px; font-weight: 700; }
.pipeline-label { font-size: 11px; color: #909399; margin-top: 2px; }
</style>
