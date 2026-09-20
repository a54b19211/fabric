<template>
  <div class="overview-page" v-loading="loading">
    <el-empty v-if="!loading && !stages.length" description="暂无流程数据" :image-size="80" />

    <div v-else class="stage-grid">
      <div v-for="stage in stages" :key="stage.key" class="stage-card">
        <div class="stage-header">
          <span class="stage-name">{{ stage.label }}</span>
          <span class="stage-total">{{ formatNum(stage.total) }}</span>
        </div>
        <div class="stage-bars">
          <div v-for="(count, status) in stage.by_status" :key="status" class="bar-row">
            <span class="bar-label">{{ statusLabel(status) }}</span>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: barWidth(count, stage.total), background: statusColor(status) }" />
            </div>
            <span class="bar-count">{{ formatNum(count) }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getPipelineOverview, type PipelineStage } from '@/api/agent'

const stages = ref<PipelineStage[]>([])
const loading = ref(false)

const STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  queued: '排队中',
  processing: '处理中',
  done: '已完成',
  failed: '失败',
  no_entity: '无实体',
  auto_approved: '自动通过',
  approved: '已通过',
  rejected: '已拒绝',
  escalated: '转人工',
  linked: '已链接',
  unlinked: '未链接',
  proposal_pending: '提案待审',
  proposal_approved: '提案通过',
  proposal_rejected: '提案拒绝',
  conflict_pending: '冲突待处理',
  weknora_pending: '待同步',
  weknora_synced: '已同步',
  weknora_failed: '同步失败',
  graph_pending: '待推送',
  graph_pushed: '已推送',
  graph_failed: '推送失败',
}

function statusLabel(key: string) {
  return STATUS_LABELS[key] ?? key
}

function statusColor(key: string) {
  if (key.includes('failed') || key === 'rejected' || key === 'no_entity') return 'var(--dp-danger)'
  if (key.includes('done') || key.includes('approved') || key.includes('synced') || key.includes('pushed') || key === 'linked' || key === 'auto_approved') return 'var(--dp-success, #67c23a)'
  if (key === 'processing' || key === 'queued') return 'var(--dp-warning)'
  return 'var(--dp-primary)'
}

function barWidth(count: number, total: number) {
  if (!total) return '0%'
  return Math.max((count / total) * 100, 1) + '%'
}

function formatNum(n: number) {
  return n.toLocaleString()
}

async function load() {
  loading.value = true
  try {
    const res = await getPipelineOverview()
    stages.value = res.stages
  } catch {
    stages.value = []
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.overview-page {
  min-height: 100%;
}

.stage-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 16px;
}

.stage-card {
  background: var(--dp-card);
  border: 1px solid var(--dp-border);
  border-radius: var(--dp-radius-lg);
  padding: 16px 18px;
}

.stage-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;
}

.stage-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--dp-text-regular);
}

.stage-total {
  font-size: 22px;
  font-weight: 700;
  color: var(--dp-primary);
}

.stage-bars {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.bar-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.bar-label {
  width: 72px;
  font-size: 12px;
  color: var(--dp-text-secondary);
  text-align: right;
  flex-shrink: 0;
}

.bar-track {
  flex: 1;
  height: 14px;
  background: var(--dp-bg);
  border-radius: 7px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  border-radius: 7px;
  transition: width 0.3s ease;
}

.bar-count {
  width: 60px;
  font-size: 12px;
  color: var(--dp-text-regular);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
</style>
