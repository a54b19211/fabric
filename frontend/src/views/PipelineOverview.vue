<template>
  <div class="overview-page" v-loading="loading">
    <el-empty v-if="!loading && !stages.length" description="暂无流程数据" :image-size="80" />

    <div v-else class="stage-grid">
      <div v-for="stage in stages" :key="stage.key" class="stage-card">
        <div class="stage-header">
          <div class="stage-title">
            <span class="stage-name">{{ stage.label }}</span>
            <span v-if="stageDesc(stage.key)" class="stage-desc">{{ stageDesc(stage.key) }}</span>
          </div>
          <div class="stage-header-right">
            <span class="stage-total">{{ formatNum(stage.total) }}</span>
          </div>
        </div>

        <div class="stage-bars">
          <div v-for="(count, status) in stage.by_status" :key="status" class="bar-row">
            <span class="bar-label">{{ statusLabel(status) }}</span>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: barWidth(count, stage.total), background: statusColor(status) }" />
            </div>
            <span class="bar-count">{{ formatNum(count) }}</span>
            <el-icon
              v-if="stage.by_site && Object.keys(stage.by_site).length && status === 'pending'"
              class="toggle-arrow"
              :class="{ expanded: expandedSite === stage.key }"
              @click="toggleSite(stage.key)"
            >
              <ArrowDown />
            </el-icon>
          </div>
        </div>

        <div v-if="expandedSite === stage.key && stage.by_site" class="site-panel">
          <div class="pie" :style="{ background: conicGradient(stage.by_site) }" />
          <div class="site-legend">
            <div v-for="(entry, idx) in siteEntries(stage.by_site)" :key="entry[0]" class="legend-row">
              <span class="legend-swatch" :style="{ background: siteColor(idx) }" />
              <span class="legend-label">{{ entry[0] }}</span>
              <span class="legend-count">{{ formatNum(entry[1]) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ArrowDown } from '@element-plus/icons-vue'
import { getPipelineOverview, type PipelineStage } from '@/api/agent'

const stages = ref<PipelineStage[]>([])
const loading = ref(false)
const expandedSite = ref<string | null>(null)

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

const SITE_COLORS = [
  '#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de',
  '#3ba272', '#fc8452', '#9a60b4', '#ea7ccc', '#66b1ff',
]

const STAGE_DESCRIPTIONS: Record<string, string> = {
  disambiguation: 'dict_id 命中率 = 初步消歧（6step，抽取时）+ 精细消歧（Agent D，审核后）两段累积结果',
}

function stageDesc(key: string) {
  return STAGE_DESCRIPTIONS[key] ?? ''
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

function siteColor(index: number) {
  return SITE_COLORS[index % SITE_COLORS.length]
}

function siteEntries(bySite: Record<string, number>) {
  return Object.entries(bySite).sort((a, b) => b[1] - a[1])
}

function conicGradient(bySite: Record<string, number>) {
  const entries = siteEntries(bySite)
  const total = entries.reduce((s, [, v]) => s + v, 0)
  if (!total) return 'none'
  let acc = 0
  const stops = entries.map(([, count], i) => {
    const start = (acc / total) * 360
    acc += count
    const end = (acc / total) * 360
    return `${siteColor(i)} ${start.toFixed(2)}deg ${end.toFixed(2)}deg`
  })
  return `conic-gradient(${stops.join(', ')})`
}

function toggleSite(key: string) {
  expandedSite.value = expandedSite.value === key ? null : key
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
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.stage-title {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.stage-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--dp-text-regular);
}

.stage-desc {
  font-size: 12px;
  color: var(--dp-text-secondary);
  line-height: 1.4;
}

.stage-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
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

.toggle-arrow {
  cursor: pointer;
  color: var(--dp-text-secondary);
  font-size: 12px;
  flex-shrink: 0;
  transition: transform 0.2s ease;
}
.toggle-arrow:hover {
  color: var(--dp-primary);
}
.toggle-arrow.expanded {
  transform: rotate(180deg);
}

.site-panel {
  display: flex;
  align-items: center;
  gap: 24px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px dashed var(--dp-border);
}

.pie {
  width: 140px;
  height: 140px;
  border-radius: 50%;
  flex-shrink: 0;
}

.site-legend {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 180px;
  overflow-y: auto;
}

.legend-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.legend-swatch {
  width: 12px;
  height: 12px;
  border-radius: 3px;
  flex-shrink: 0;
}

.legend-label {
  flex: 1;
  font-size: 13px;
  color: var(--dp-text-regular);
}

.legend-count {
  font-size: 13px;
  color: var(--dp-text-secondary);
  font-variant-numeric: tabular-nums;
}
</style>
