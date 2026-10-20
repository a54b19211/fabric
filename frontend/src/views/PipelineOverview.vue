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
              v-if="siteData(stage, status) && Object.keys(siteData(stage, status)!).length"
              class="toggle-arrow"
              :class="{ expanded: isExpanded(stage.key, status) }"
              @click="toggleSite(stage.key, status)"
            >
              <ArrowDown />
            </el-icon>
          </div>
        </div>

        <div v-if="stage.by_match_step && Object.keys(stage.by_match_step).length" class="match-step-panel">
          <div class="match-step-title">消歧匹配分布</div>
          <template v-for="row in matchStepRows(stage.by_match_step)" :key="row.step">
            <!-- 可折叠集合（step3 类型规则） -->
            <div
              v-if="row.children"
              class="bar-row bar-group"
              @click="toggleMatchGroup(row.step)"
            >
              <span class="bar-label group-toggle">
                <el-icon class="caret" :class="{ expanded: isMatchGroupExpanded(row.step) }"><ArrowDown /></el-icon>
                {{ row.label }}
              </span>
              <div class="bar-track">
                <div class="bar-fill" :style="{ width: barWidth(row.count, stage.total), background: matchStepColor(row.step) }" />
              </div>
              <span class="bar-count">{{ formatNum(row.count) }}</span>
            </div>
            <!-- 展开后的具体实体类型 -->
            <div
              v-for="child in (row.children || [])"
              v-show="row.children && isMatchGroupExpanded(row.step)"
              :key="child.step"
              class="bar-row bar-child"
            >
              <span class="bar-label">{{ child.label }}</span>
              <div class="bar-track">
                <div class="bar-fill" :style="{ width: barWidth(child.count, stage.total), background: matchStepColor(child.step) }" />
              </div>
              <span class="bar-count">{{ formatNum(child.count) }}</span>
            </div>
            <!-- 普通类型行 -->
            <div v-if="!row.children" class="bar-row">
              <span class="bar-label">{{ row.label }}</span>
              <div class="bar-track">
                <div class="bar-fill" :style="{ width: barWidth(row.count, stage.total), background: matchStepColor(row.step) }" />
              </div>
              <span class="bar-count">{{ formatNum(row.count) }}</span>
            </div>
          </template>
        </div>

        <div v-if="stage.by_dict && Object.keys(stage.by_dict).length" class="match-step-panel">
          <div class="match-step-title">字典现状</div>
          <div v-for="(count, key) in stage.by_dict" :key="key" class="bar-row">
            <span class="bar-label">
              {{ dictLabel(key) }}
              <el-tooltip v-if="key === 'dict_duplicate_groups'" trigger="click" placement="top" :content="DUPLICATE_EXPLAIN" :show-after="0">
                <el-icon class="info-icon"><WarningFilled /></el-icon>
              </el-tooltip>
            </span>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: barWidth(count, dictTotal(stage.by_dict)), background: dictColor(key) }" />
            </div>
            <span class="bar-count">{{ formatNum(count) }}</span>
          </div>
        </div>

        <div v-if="expandedSite && expandedSite.key === stage.key && siteData(stage, expandedSite.status)" class="site-panel">
          <div class="pie" :style="{ background: conicGradient(siteData(stage, expandedSite.status)!) }" />
          <div class="site-legend">
            <div v-for="(entry, idx) in siteEntries(siteData(stage, expandedSite.status)!)" :key="entry[0]" class="legend-row">
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
import { ArrowDown, WarningFilled } from '@element-plus/icons-vue'
import { getPipelineOverview, type PipelineStage } from '@/api/agent'

const stages = ref<PipelineStage[]>([])
const loading = ref(false)
const expandedSite = ref<{ key: string; status: string } | null>(null)

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
  weknora_pending: '文档待同步',
  weknora_synced: '文档已同步',
  weknora_failed: '文档同步失败',
  graph_entity_pending: '实体待推送',
  graph_entity_pushed: '实体已推送',
  graph_entity_failed: '实体推送失败',
  graph_relation_pending: '关系待推送',
  graph_relation_pushed: '关系已推送',
  graph_relation_failed: '关系推送失败',
}

const SITE_COLORS = [
  '#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de',
  '#3ba272', '#fc8452', '#9a60b4', '#ea7ccc', '#66b1ff',
]

const STAGE_DESCRIPTIONS: Record<string, string> = {
  disambiguation: 'dict_id 命中率 = 初步消歧（强ID匹配 / 精确别名 / 类型规则 / 向量召回+Reranker 精排，抽取时）+ 精细消歧（Agent D，审核后）两段累积结果；统计已排除 PrimeKG 本体镜像行（其不参与消歧，避免污染"未消歧"指标）',
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

const MATCH_STEP_LABELS: Record<string, string> = {
  step1_strong_id: '强ID匹配',
  step2_exact_alias: '精确匹配',
  step3_company_rule: '类型规则',
  step3_drug_cas: '类型规则',
  step3_drug_id: '类型规则',
  step3_target_rule: '类型规则',
  step5_reranker: 'Reranker精排',
  step5_reranker_error: '精排失败',
  step6_llm: 'LLM兜底',
  step6_llm_error: 'LLM兜底失败',
  step_none: '无匹配·新实体',
  none: '未消歧',
}

// step3 类型规则合并为可折叠集合：展开后展示具体匹配的实体类型
const STEP3_CHILDREN: { key: string; label: string }[] = [
  { key: 'step3_company_rule', label: '公司 Company' },
  { key: 'step3_drug_cas', label: '药物 CAS 号' },
  { key: 'step3_drug_id', label: '药物 ID' },
  { key: 'step3_target_rule', label: '靶点基因 Target' },
]

// 按消歧管线阶段排序（step1 → step2 → step3集合 → step5 → 兜底），
// 即使后端无某类数据也占位显示（数量 0），避免展示错乱、误导阅读。
// 注：step6_llm（LLM兜底）已按课题设计禁用并降级为分级路由（第6章），不再作为活跃阶段展示，故不在此固定顺序中。
const MATCH_STEP_ORDER: string[] = [
  'step1_strong_id',
  'step2_exact_alias',
  'step3_group',
  'step5_reranker',
  'step5_reranker_error',
  'step_none',
  'none',
]

function matchStepLabel(key: string) {
  return MATCH_STEP_LABELS[key] ?? key
}

type MatchStepRow = {
  step: string
  label: string
  count: number
  children?: { step: string; label: string; count: number }[]
}

// 将 by_match_step（后端无序对象）转为按管线阶段有序的数组；
// step3_* 聚合为一个可折叠「类型规则」组（展开列出具体实体类型），其余类型即使数量 0 也占位
function matchStepRows(
  byMatchStep: Record<string, number> | undefined,
): MatchStepRow[] {
  const src = byMatchStep ?? {}
  const rows: MatchStepRow[] = []
  for (const key of MATCH_STEP_ORDER) {
    if (key === 'step3_group') {
      const children = STEP3_CHILDREN.map((c) => ({
        step: c.key,
        label: `${c.key} · ${c.label}`,
        count: src[c.key] ?? 0,
      }))
      rows.push({
        step: 'step3',
        label: 'step3 · 类型规则',
        count: children.reduce((s, c) => s + c.count, 0),
        children,
      })
    } else {
      rows.push({
        step: key,
        label: `${key} · ${matchStepLabel(key)}`,
        count: src[key] ?? 0,
      })
    }
  }
  // 后端返回但不在预定义顺序中的新键追加到末尾
  const known = new Set([
    ...MATCH_STEP_ORDER.filter((k) => k !== 'step3_group'),
    ...STEP3_CHILDREN.map((c) => c.key),
  ])
  for (const k of Object.keys(src)) {
    if (!known.has(k)) {
      rows.push({ step: k, label: `${k} · ${matchStepLabel(k)}`, count: src[k] })
    }
  }
  return rows
}

// step3 类型规则集合的展开/折叠状态（同一时刻仅展开一个）
const expandedMatchGroup = ref<string | null>(null)
function isMatchGroupExpanded(step: string) {
  return expandedMatchGroup.value === step
}
function toggleMatchGroup(step: string) {
  expandedMatchGroup.value = expandedMatchGroup.value === step ? null : step
}

function matchStepColor(key: string) {
  if (key === 'step1_strong_id' || key === 'step2_exact_alias') return 'var(--dp-success, #67c23a)'
  if (key.startsWith('step3_')) return 'var(--dp-warning)'
  if (key === 'step5_reranker' || key === 'step6_llm') return 'var(--dp-primary)'
  if (key === 'step5_reranker_error' || key === 'step6_llm_error') return 'var(--dp-danger)'
  return 'var(--dp-text-secondary, #909399)'
}

const DICT_LABELS: Record<string, string> = {
  dict_verified: '字典实体(PrimeKG)',
  dict_unverified: '字典实体(抽取)',
  dict_duplicate_groups: '强ID重复组',
}

function dictLabel(key: string) {
  return DICT_LABELS[key] ?? key
}

function dictColor(key: string) {
  if (key === 'dict_duplicate_groups') return 'var(--dp-danger)'
  if (key === 'dict_verified') return 'var(--dp-success, #67c23a)'
  return 'var(--dp-primary)'
}

function dictTotal(byDict: Record<string, number>) {
  return (byDict.dict_verified || 0) + (byDict.dict_unverified || 0)
}

const DUPLICATE_EXPLAIN = '强ID重复组：同一个权威唯一标识（如 NCT 注册号、CAS 号、股票代码、批准文号）在字典中被拆成了多个实体。说明消歧时本应链接到同一字典实体的记录被误判成了新实体，导致字典层出现重复，需要归并处理。'

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

function siteData(stage: PipelineStage, status: string): Record<string, number> | null {
  return stage.by_site?.[status] ?? null
}

function isExpanded(key: string, status: string) {
  return expandedSite.value?.key === key && expandedSite.value?.status === status
}

function toggleSite(key: string, status: string) {
  if (expandedSite.value?.key === key && expandedSite.value?.status === status) {
    expandedSite.value = null
  } else {
    expandedSite.value = { key, status }
  }
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
  width: 110px;
  font-size: 12px;
  color: var(--dp-text-secondary);
  text-align: right;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 3px;
}

.bar-group {
  cursor: pointer;
  user-select: none;
}
.bar-group:hover .bar-label {
  color: var(--dp-text-primary, #303133);
}
.group-toggle {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.caret {
  transition: transform 0.2s ease;
  transform: rotate(-90deg);
  color: var(--dp-text-secondary);
}
.caret.expanded {
  transform: rotate(0deg);
}
.bar-child {
  padding-left: 20px;
}
.bar-child .bar-label {
  font-size: 11px;
  opacity: 0.85;
}

.info-icon {
  font-size: 13px;
  color: var(--dp-text-secondary);
  cursor: pointer;
  flex-shrink: 0;
}
.info-icon:hover {
  color: var(--dp-warning);
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

.match-step-panel {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--dp-border);
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.match-step-title {
  font-size: 12px;
  color: var(--dp-text-secondary);
  font-weight: 600;
}

/* 匹配分布标签带 match_step 前缀，需更宽以免截断 */
.match-step-panel .bar-label {
  width: 185px;
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
