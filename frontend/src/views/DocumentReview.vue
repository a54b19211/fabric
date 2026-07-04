<template>
  <div class="doc-review-page">
    <!-- ① 文档列表 -->
    <el-card class="top-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">文档审核</span>
          <div class="card-actions">
            <el-button
              type="primary"
              :disabled="selectedDocs.length === 0"
              :loading="batchRunning"
              @click="startBatchReview"
            >
              批量审核 ({{ selectedDocs.length }})
            </el-button>
            <el-button text :loading="docsLoading" @click="loadPendingDocuments">
              <el-icon><Refresh /></el-icon>
              刷新
            </el-button>
          </div>
        </div>
      </template>

      <el-tabs v-model="filterStatus" class="doc-tabs" @tab-change="onTabChange">
        <el-tab-pane label="全部" name="" />
        <el-tab-pane name="pending">
          <template #label>
            待审核 <el-badge :value="statusCounts.pending" :hidden="!statusCounts.pending" class="tab-badge" />
          </template>
        </el-tab-pane>
        <el-tab-pane name="reviewing">
          <template #label>
            审核中 <el-badge :value="statusCounts.reviewing" :hidden="!statusCounts.reviewing" class="tab-badge" />
          </template>
        </el-tab-pane>
        <el-tab-pane name="reviewed">
          <template #label>
            已审核 <el-badge :value="statusCounts.reviewed" :hidden="!statusCounts.reviewed" class="tab-badge" />
          </template>
        </el-tab-pane>
      </el-tabs>

      <el-table
        ref="tableRef"
        :data="displayedDocuments"
        stripe
        size="small"
        v-loading="docsLoading"
        @selection-change="onSelectionChange"
      >
        <template #empty>
          <div class="table-empty">
            <el-icon :size="48" color="var(--el-text-color-placeholder)"><Document /></el-icon>
            <p>暂无待审核文档</p>
            <p class="table-empty-hint">所有文档中的实体和关系都已审核完成</p>
          </div>
        </template>
        <el-table-column type="selection" width="40" :selectable="isSelectable" />
        <el-table-column prop="document_title" label="文档标题" min-width="220" show-overflow-tooltip />
        <el-table-column prop="source_site" label="来源" width="120">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ row.source_site || '-' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="待审实体" width="90" align="center">
          <template #default="{ row }">
            <el-badge :value="row.pending_count" :type="row.pending_count > 0 ? 'warning' : 'info'" />
          </template>
        </el-table-column>
        <el-table-column label="待审关系" width="90" align="center">
          <template #default="{ row }">
            <el-badge :value="row.pending_relation_count" :type="row.pending_relation_count > 0 ? 'warning' : 'info'" />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(effectiveStatus(row))">
              {{ statusLabel(effectiveStatus(row)) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="effectiveStatus(row) === 'reviewed'"
              size="small"
              @click.stop="focusDocument(row.document_uuid)"
            >
              查看详情
            </el-button>
            <el-button
              v-else
              size="small"
              :type="effectiveStatus(row) === 'reviewing' ? 'warning' : 'primary'"
              :disabled="effectiveStatus(row) === 'reviewing'"
              @click.stop="startDocumentReview(row)"
            >
              {{ effectiveStatus(row) === 'reviewing' ? '审核中' : '审核' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="doc-pagination">
        <el-pagination
          v-model:current-page="docsPage"
          :page-size="docsPageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="docsTotal"
          layout="total, sizes, prev, pager, next"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </el-card>

    <!-- ② 活跃审核进度 -->
    <el-card v-if="activeCount > 0" class="batch-summary-card">
      <div class="batch-summary">
        <span class="batch-label">审核中：{{ activeCount }} 篇文档</span>
        <div class="batch-doc-chips">
          <el-tag
            v-for="(state, uuid) in activeReviews"
            :key="uuid"
            :type="focusedDocUuid === uuid ? 'primary' : 'info'"
            class="batch-chip"
            @click="focusDocument(uuid)"
          >
            {{ state.title || uuid }}
            <span class="batch-chip-count">
              实体 {{ entityDoneCount(state) }}/{{ entityTotalCount(state) }}
              <template v-if="relationTotalCount(state) > 0">
                 · 关系 {{ relationDoneCount(state) }}/{{ relationTotalCount(state) }}
              </template>
            </span>
          </el-tag>
        </div>
      </div>
    </el-card>

    <!-- 未选择文档时的空状态 -->
    <div v-if="!focusedDocUuid && !docsLoading && activeCount === 0" class="empty-state">
      <el-empty description="选择一篇已审核的文档，查看实体和关系的审核结果" :image-size="80">
        <template #image>
          <el-icon :size="64" color="var(--el-text-color-placeholder)"><Files /></el-icon>
        </template>
      </el-empty>
    </div>

    <!-- ③ 审核结果区域 -->
    <div v-if="focusedDocUuid && focusedState && focusedState.runs.length > 0" class="results-area">
      <!-- 结果摘要头 -->
      <div class="results-header">
        <div class="results-title-row">
          <span class="results-title">{{ focusedState.title }}</span>
          <el-tag size="small" :type="focusedState.reviewing ? 'warning' : 'success'">
            {{ focusedState.reviewing ? '审核中' : '已完成' }}
          </el-tag>
        </div>
        <div class="results-summary">
          <span class="summary-item passed">
            <el-icon><CircleCheck /></el-icon> 实体通过 {{ focusedStats.approved }}
          </span>
          <span class="summary-item failed">
            <el-icon><CircleClose /></el-icon> 实体驳回 {{ focusedStats.rejected }}
          </span>
          <template v-if="relationTotalCount(focusedState) > 0">
            <span class="summary-divider">|</span>
            <span class="summary-item passed">
              <el-icon><CircleCheck /></el-icon> 关系通过 {{ relationStats.approved }}
            </span>
            <span class="summary-item failed">
              <el-icon><CircleClose /></el-icon> 关系驳回 {{ relationStats.rejected }}
            </span>
          </template>
        </div>
      </div>

      <!-- Tab: 实体审核 / 关系审核 -->
      <el-tabs v-model="reviewTab" class="review-tabs">
        <el-tab-pane label="实体审核" name="entity">
          <div v-if="focusedSortedEntities.length === 0" class="tab-empty">
            暂无实体审核记录
          </div>
          <div v-else class="step-cards">
            <div
              v-for="step in entityStepCards"
              :key="step.stepId"
              class="step-card"
              :class="stepCardClass(step)"
            >
              <div class="step-card-header" @click="toggleStep(step.stepId)">
                <div class="step-card-left">
                  <el-icon v-if="step.overall === 'passed'" class="step-icon passed"><CircleCheck /></el-icon>
                  <el-icon v-else-if="step.overall === 'failed'" class="step-icon failed"><CircleClose /></el-icon>
                  <el-icon v-else class="step-icon skipped"><Remove /></el-icon>
                  <span class="step-card-title">{{ step.title }}</span>
                  <span class="step-card-desc">{{ step.description }}</span>
                </div>
                <div class="step-card-right">
                  <span class="step-card-stat">
                    通过 {{ step.passedCount }} / 共 {{ step.totalCount }}
                  </span>
                  <el-icon class="expand-icon" :class="{ expanded: isExpanded(step.stepId) }"><ArrowDown /></el-icon>
                </div>
              </div>
              <div v-show="isExpanded(step.stepId)" class="step-card-body">
                <div
                  v-for="item in step.items"
                  :key="item.name"
                  class="step-item"
                  :class="item.status"
                  @click="toggleItem(item.source)"
                >
                  <el-icon v-if="item.status === 'passed'" class="step-item-icon passed"><CircleCheck /></el-icon>
                  <el-icon v-else-if="item.status === 'failed'" class="step-item-icon failed"><CircleClose /></el-icon>
                  <el-icon v-else class="step-item-icon skipped"><Remove /></el-icon>
                  <div class="step-item-info">
                    <span class="step-item-name">{{ item.name }}</span>
                    <span class="step-item-type">{{ item.type }}</span>
                  </div>
                  <div class="step-item-reason">{{ item.reason }}</div>
                  <span class="step-item-confidence">{{ item.confidence }}%</span>
                </div>
              </div>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="关系审核" name="relation">
          <div v-if="focusedRelationRuns.length === 0" class="tab-empty">
            暂无关系审核记录
          </div>
          <div v-else class="step-cards">
            <div
              v-for="step in relationStepCards"
              :key="step.stepId"
              class="step-card"
              :class="stepCardClass(step)"
            >
              <div class="step-card-header" @click="toggleStep(step.stepId)">
                <div class="step-card-left">
                  <el-icon v-if="step.overall === 'passed'" class="step-icon passed"><CircleCheck /></el-icon>
                  <el-icon v-else-if="step.overall === 'failed'" class="step-icon failed"><CircleClose /></el-icon>
                  <el-icon v-else class="step-icon skipped"><Remove /></el-icon>
                  <span class="step-card-title">{{ step.title }}</span>
                  <span class="step-card-desc">{{ step.description }}</span>
                </div>
                <div class="step-card-right">
                  <span class="step-card-stat">
                    通过 {{ step.passedCount }} / 共 {{ step.totalCount }}
                  </span>
                  <el-icon class="expand-icon" :class="{ expanded: isExpanded(step.stepId) }"><ArrowDown /></el-icon>
                </div>
              </div>
              <div v-show="isExpanded(step.stepId)" class="step-card-body">
                <div
                  v-for="item in step.items"
                  :key="item.name"
                  class="step-item"
                  :class="item.status"
                  @click="toggleItem(item.source)"
                >
                  <el-icon v-if="item.status === 'passed'" class="step-item-icon passed"><CircleCheck /></el-icon>
                  <el-icon v-else-if="item.status === 'failed'" class="step-item-icon failed"><CircleClose /></el-icon>
                  <el-icon v-else class="step-item-icon skipped"><Remove /></el-icon>
                  <div class="step-item-info">
                    <span class="step-item-name">{{ item.name }}</span>
                    <span class="step-item-type">{{ item.type }}</span>
                  </div>
                  <div class="step-item-reason">{{ item.reason }}</div>
                  <span class="step-item-confidence">{{ item.confidence }}%</span>
                </div>
              </div>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="图谱" name="graph">
          <KnowledgeGraph
            :nodes="graphNodes"
            :edges="graphEdges"
          />
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- 审核详情弹出 -->
    <el-card v-if="selectedItem" class="detail-card">
      <template #header>
        <span>{{ detailCardTitle }} — 审核详情</span>
        <el-button text style="float: right" @click="selectedItem = null">
          <el-icon><Close /></el-icon>
        </el-button>
      </template>
      <ReasoningTimeline
        :steps="selectedItem.steps || []"
        :decision="selectedItem.decision"
        :confidence="selectedItem.confidence"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onUnmounted, onMounted } from 'vue'
import { CircleCheck, CircleClose, Remove, Close, Refresh, Document, Files, ArrowDown } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import {
  getPendingDocuments,
  getDocumentRuns,
  triggerDocumentReviewAsync,
  type AgentRunRecord,
  type PendingDocument,
  type PipelineStep,
} from '@/api/agent'
import ReasoningTimeline from '@/components/ReasoningTimeline.vue'
import KnowledgeGraph from '@/components/KnowledgeGraph.vue'

interface EntityRow {
  entity_name: string
  entity_type_: string
  decision: string
  confidence: number
  steps: PipelineStep[]
  run: AgentRunRecord
}

interface RelationRow {
  relation_name: string
  decision: string
  confidence: number
  steps: PipelineStep[]
  run: AgentRunRecord
}

interface ReviewState {
  title: string
  runs: AgentRunRecord[]
  pending_count: number
  reviewing: boolean
  reviewProgress: number
}

interface StepCardItem {
  name: string
  type: string
  status: string
  reason: string
  confidence: string
  source: EntityRow | RelationRow
}

interface StepCard {
  stepId: string
  title: string
  description: string
  overall: string
  passedCount: number
  totalCount: number
  items: StepCardItem[]
}

const STEP_DESCRIPTIONS: Record<string, { title: string; desc: string }> = {
  validity: { title: '① 有效性检验', desc: '判断实体名称是否为有效的知识实体，排除通用描述词、纯数字等噪音' },
  type_check: { title: '② 类型正确性', desc: '判断实体的类型标注（Drug / Company / Target 等）是否正确' },
  source_evidence: { title: '③ 原文依据', desc: '逐字段验证实体属性数据在原文中是否有对应依据' },
  endpoint_check: { title: '① 端点存在性', desc: '检查关系两端实体是否存在于数据库中' },
  type_validity: { title: '② 类型合法性', desc: '判断关系类型对该实体类型对是否在合法白名单中' },
}

// ── 文档列表状态 ──────────────────────────────────────────

const docsLoading = ref(false)
const pendingDocuments = ref<PendingDocument[]>([])
const docsTotal = ref(0)
const docsPage = ref(1)
const docsPageSize = ref(20)
const selectedDocs = ref<PendingDocument[]>([])
const batchRunning = ref(false)
const filterStatus = ref<string>('')

const displayedDocuments = computed(() => {
  if (filterStatus.value === 'reviewing') {
    return pendingDocuments.value.filter(d => effectiveStatus(d) === 'reviewing')
  }
  return pendingDocuments.value
})

function onTabChange() {
  docsPage.value = 1
  loadPendingDocuments()
}

function onPageChange() {
  loadPendingDocuments()
}

function onSizeChange(size: number) {
  docsPageSize.value = size
  docsPage.value = 1
  loadPendingDocuments()
}

const statusCounts = computed(() => {
  let pending = 0, reviewing = 0, reviewed = 0
  for (const d of pendingDocuments.value) {
    const s = effectiveStatus(d)
    if (s === 'pending') pending++
    else if (s === 'reviewing') reviewing++
    else if (s === 'reviewed') reviewed++
  }
  return { pending, reviewing, reviewed }
})

function effectiveStatus(doc: PendingDocument): string {
  const state = activeReviews.value[doc.document_uuid]
  if (state?.reviewing) return 'reviewing'
  return doc.review_status
}

function statusTagType(status: string) {
  if (status === 'pending') return 'warning'
  if (status === 'reviewing') return 'primary'
  if (status === 'reviewed') return 'success'
  return 'info'
}

function statusLabel(status: string) {
  const map: Record<string, string> = { pending: '待审核', reviewing: '审核中', reviewed: '已审核' }
  return map[status] || status
}

function isSelectable(row: PendingDocument): boolean {
  return effectiveStatus(row) !== 'reviewed'
}

// ── 多文档审核状态 ─────────────────────────────────────────

const activeReviews = ref<Record<string, ReviewState>>({})
const focusedDocUuid = ref('')
const selectedItem = ref<EntityRow | RelationRow | null>(null)

const detailCardTitle = computed(() => {
  if (!selectedItem.value) return ''
  const item = selectedItem.value as any
  if (item.entity_name) return item.entity_name
  // 关系：用端点信息构造标题
  const s1 = item.steps?.find((s: any) => s.step_id === 'endpoint_check')
  const input = s1?.input || {}
  const s2 = item.steps?.find((s: any) => s.step_id === 'type_validity')
  const relType = s2?.input?.relation_type || ''
  return `${input.from_entity || '?'} → ${relType || '?'} → ${input.to_entity || '?'}`
})
const reviewTab = ref<string>('entity')

let pollTimer: ReturnType<typeof setInterval> | null = null

const activeCount = computed(() => Object.keys(activeReviews.value).length)

// ── 聚焦文档的派生状态 ──────────────────────────────────────

const focusedState = computed(() => {
  if (!focusedDocUuid.value) return null
  return activeReviews.value[focusedDocUuid.value] || null
})

function isPipelineRun(run: AgentRunRecord): boolean {
  const steps = (run.steps || []) as any[]
  if (steps.length === 0) return false
  return steps.some(s => 'step_id' in s)
}

function computeEntities(runs: AgentRunRecord[]): EntityRow[] {
  return runs
    .filter(r => r.agent_type === 'entity_review' && isPipelineRun(r))
    .map(r => {
      const steps = (r.steps || []) as PipelineStep[]
      const s1 = steps.find(s => s.step_id === 'validity')
      const input = (s1?.input || {}) as Record<string, any>
      return {
        entity_name: r.entity_name || `ID:${r.target_id}`,
        entity_type_: input?.entity_type || '-',
        decision: r.decision,
        confidence: r.confidence,
        steps,
        run: r,
      }
    })
    .sort((a, b) => {
      const order: Record<string, number> = { approved: 0, rejected: 1, escalated: 2, error: 3 }
      return (order[a.decision] ?? 4) - (order[b.decision] ?? 4)
    })
}

const focusedSortedEntities = computed<EntityRow[]>(() => {
  if (!focusedState.value) return []
  return computeEntities(focusedState.value.runs)
})

function computeRelations(runs: AgentRunRecord[]): RelationRow[] {
  return runs
    .filter(r => r.agent_type === 'relation_review' && isPipelineRun(r))
    .map(r => ({
      relation_name: r.entity_name || `R:${r.target_id}`,
      decision: r.decision,
      confidence: r.confidence,
      steps: (r.steps || []) as PipelineStep[],
      run: r,
    }))
    .sort((a, b) => {
      const order: Record<string, number> = { approved: 0, rejected: 1, escalated: 2, error: 3 }
      return (order[a.decision] ?? 4) - (order[b.decision] ?? 4)
    })
}

const focusedRelationRuns = computed<RelationRow[]>(() => {
  if (!focusedState.value) return []
  return computeRelations(focusedState.value.runs)
})

const focusedStats = computed(() => {
  let approved = 0, rejected = 0, escalated = 0, error = 0
  for (const e of focusedSortedEntities.value) {
    if (e.decision === 'approved') approved++
    else if (e.decision === 'rejected') rejected++
    else if (e.decision === 'escalated') escalated++
    else if (e.decision === 'error') error++
  }
  return { approved, rejected, escalated, error }
})

const relationStats = computed(() => {
  let approved = 0, rejected = 0, error = 0
  for (const r of focusedRelationRuns.value) {
    if (r.decision === 'approved') approved++
    else if (r.decision === 'rejected') rejected++
    else if (r.decision === 'error') error++
  }
  return { approved, rejected, error }
})

// ── 图谱数据 ────────────────────────────────────────────────

const graphNodes = computed(() => {
  if (!focusedState.value) return []
  return computeEntities(focusedState.value.runs).map(e => ({
    id: e.entity_name,
    label: e.entity_name,
    type: e.entity_type_,
    decision: e.decision,
  }))
})

const graphEdges = computed(() => {
  if (!focusedState.value) return []
  return computeRelations(focusedState.value.runs).map(r => {
    const s1 = r.steps.find(s => s.step_id === 'endpoint_check')
    const input = (s1?.input || {}) as Record<string, any>
    const s2 = r.steps.find(s => s.step_id === 'type_validity')
    const relType = (s2?.input as any)?.relation_type || ''
    return {
      source: input?.from_entity || '',
      target: input?.to_entity || '',
      label: relType,
      decision: r.decision,
    }
  }).filter(e => e.source && e.target)
})

// ── 进度计数 ────────────────────────────────────────────────

function entityDoneCount(state: ReviewState): number {
  return computeEntities(state.runs).length
}

function entityTotalCount(state: ReviewState): number {
  return state.pending_count + entityDoneCount(state)
}

function relationDoneCount(state: ReviewState): number {
  return computeRelations(state.runs).length
}

function relationTotalCount(state: ReviewState): number {
  // pending_relation_count not available in ReviewState directly, estimate from runs
  // The document's pending_relation_count from the list gives the total
  const doc = pendingDocuments.value.find(d => d.document_uuid === focusedDocUuid.value)
  if (doc) return (doc.pending_relation_count || 0) + relationDoneCount(state)
  return relationDoneCount(state)
}

// ── 步骤卡片数据 (实体) ─────────────────────────────────────

function getStepStatus(item: EntityRow | RelationRow, stepId: string): string {
  const step = item.steps.find(s => s.step_id === stepId)
  if (!step) return 'pending'
  return step.status
}

function getStepReason(item: EntityRow | RelationRow, stepId: string): string {
  const step = item.steps.find(s => s.step_id === stepId)
  if (!step) return ''
  if (stepId === 'source_evidence') return getEvidenceText(item)
  const output = (step.output || {}) as Record<string, any>
  return output?.reason || ''
}

function getEvidenceText(item: EntityRow | RelationRow): string {
  const step = item.steps.find(s => s.step_id === 'source_evidence')
  if (!step) return ''
  const output = step.output as any
  const input = step.input as any
  if (!output) return ''
  // 关系：单一 evidence_span，为空时回退到 source_text
  if (output.evidence_span) return output.evidence_span
  // 实体：fields 数组，取第一个有证据的 span
  if (output.fields && Array.isArray(output.fields)) {
    const withEvidence = output.fields.filter((f: any) => f.has_evidence && f.evidence_span)
    if (withEvidence.length > 0) return withEvidence[0].evidence_span
    const withoutEvidence = output.fields.find((f: any) => !f.has_evidence)
    if (withoutEvidence) return `[无依据] ${withoutEvidence.name}`
  }
  // 关系 evidence_span 为空时显示原文片段
  if (input?.source_text) return `原文: ${(input.source_text as string).slice(0, 120)}`
  return output.reason || ''
}

function getEvidenceLabel(item: EntityRow | RelationRow): string {
  const step = item.steps.find(s => s.step_id === 'source_evidence')
  if (!step) return ''
  const output = step.output as any
  if (output?.overall === 'full') return '全部有据'
  if (output?.overall === 'partial') return '部分有据'
  if (output?.overall === 'none') return '无依据'
  // 关系：没有 overall，用 has_evidence
  if (output?.has_evidence === true) return '有依据'
  if (output?.has_evidence === false) return '无依据'
  return ''
}

function buildStepCard(
  stepId: string,
  items: (EntityRow | RelationRow)[],
  getName: (i: any) => string,
  getType: (i: any) => string,
): StepCard {
  const meta = STEP_DESCRIPTIONS[stepId] || { title: stepId, desc: '' }
  let passedCount = 0, totalCount = 0
  const cardItems: StepCardItem[] = []

  for (const item of items) {
    const status = getStepStatus(item, stepId)
    if (status !== 'pending') totalCount++
    if (status === 'passed') passedCount++

    cardItems.push({
      name: getName(item),
      type: getType(item),
      status,
      reason: (stepId === 'source_evidence'
        ? [getEvidenceLabel(item), getStepReason(item, stepId)].filter(Boolean).join(': ')
        : getStepReason(item, stepId)
      ) || (status === 'skipped' ? '前置步骤未通过，已跳过' : ''),
      confidence: ((item.confidence || 0) * 100).toFixed(0),
      source: item,
    })
  }

  let overall: string
  if (totalCount === 0) overall = 'pending'
  else if (passedCount === totalCount) overall = 'passed'
  else if (passedCount === 0) overall = 'failed'
  else overall = 'partial'

  return {
    stepId,
    title: meta.title,
    description: meta.desc,
    overall,
    passedCount,
    totalCount,
    items: cardItems,
  }
}

const expandedSteps = ref<Set<string>>(new Set())

function isExpanded(stepId: string): boolean {
  return expandedSteps.value.has(stepId)
}

function toggleStep(stepId: string) {
  const s = new Set(expandedSteps.value)
  if (s.has(stepId)) s.delete(stepId)
  else s.add(stepId)
  expandedSteps.value = s
}

const entityStepCards = computed<StepCard[]>(() => {
  const entities = focusedSortedEntities.value
  if (entities.length === 0) return []
  return ['validity', 'type_check', 'source_evidence'].map(id =>
    buildStepCard(
      id,
      entities,
      (e: EntityRow) => e.entity_name,
      (e: EntityRow) => e.entity_type_,
    ),
  )
})

const relationStepCards = computed<StepCard[]>(() => {
  const relations = focusedRelationRuns.value
  if (relations.length === 0) return []
  return ['endpoint_check', 'type_validity', 'source_evidence'].map(id =>
    buildStepCard(
      id,
      relations,
      (r: RelationRow) => r.relation_name,
      (r: RelationRow) => '',
    ),
  )
})

function stepCardClass(card: StepCard) {
  return {
    'sc-passed': card.overall === 'passed',
    'sc-failed': card.overall === 'failed',
    'sc-partial': card.overall === 'partial',
    'sc-pending': card.overall === 'pending',
  }
}

// ── 加载文档列表 ──────────────────────────────────────────

async function loadPendingDocuments() {
  docsLoading.value = true
  try {
    const status = filterStatus.value === 'reviewing' ? 'pending' : filterStatus.value
    const data = await getPendingDocuments({ page: docsPage.value, page_size: docsPageSize.value, status })
    pendingDocuments.value = data.items || []
    docsTotal.value = data.total || 0
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    docsLoading.value = false
  }
}

onMounted(() => loadPendingDocuments())

// ── 选择 ──────────────────────────────────────────────────

function onSelectionChange(docs: PendingDocument[]) {
  selectedDocs.value = docs
}

// ── 单个文档审核 ──────────────────────────────────────────

async function startDocumentReview(doc: PendingDocument) {
  const uuid = doc.document_uuid
  if (!uuid) return
  await triggerOne(uuid, doc.document_title, doc.pending_count)
}

async function startBatchReview() {
  if (selectedDocs.value.length === 0) return
  batchRunning.value = true
  try {
    const docs = [...selectedDocs.value]
    for (const doc of docs) {
      await triggerOne(doc.document_uuid, doc.document_title, doc.pending_count)
    }
    ElMessage.success(`已触发 ${docs.length} 篇文档审核`)
    ensurePolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '批量触发失败')
  } finally {
    batchRunning.value = false
  }
}

async function triggerOne(uuid: string, title: string, pendingCount: number) {
  if (activeReviews.value[uuid]) return
  try {
    await triggerDocumentReviewAsync(uuid)
    activeReviews.value[uuid] = {
      title: title || uuid,
      runs: [],
      pending_count: pendingCount,
      reviewing: true,
      reviewProgress: 0,
    }
    if (!focusedDocUuid.value) {
      focusedDocUuid.value = uuid
    }
  } catch (e: any) {
    ElMessage.error(`「${title}」触发失败: ${e?.response?.data?.detail || e}`)
  }
}

async function focusDocument(uuid: string) {
  focusedDocUuid.value = uuid
  if (activeReviews.value[uuid]) return

  try {
    const data = await getDocumentRuns(uuid)
    const doc = pendingDocuments.value.find(d => d.document_uuid === uuid)
    if (!activeReviews.value[uuid]) {
      activeReviews.value[uuid] = {
        title: doc?.document_title || uuid,
        runs: data.runs || [],
        pending_count: data.pending_count,
        reviewing: false,
        reviewProgress: data.pending_count === 0 ? 100 : 0,
      }
    }
  } catch {
    // ignore
  }
}

// ── 轮询 ──────────────────────────────────────────────────

function ensurePolling() {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    const uuids = Object.keys(activeReviews.value).filter(
      k => activeReviews.value[k].reviewing
    )
    if (uuids.length === 0) {
      stopPolling()
      return
    }

    let changed = false
    for (const uuid of uuids) {
      try {
        const data = await getDocumentRuns(uuid)
        const state = activeReviews.value[uuid]
        if (!state) continue
        state.runs = data.runs || []
        state.pending_count = data.pending_count
        const total = state.pending_count + state.runs.length
        state.reviewProgress = total === 0 ? 100 : Math.round((state.runs.length / total) * 100)
        if (data.pending_count === 0) {
          state.reviewing = false
          changed = true
        }
      } catch {
        // ignore
      }
    }

    if (changed) {
      loadPendingDocuments()
    }
  }, 3000)
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

onUnmounted(() => stopPolling())

// ── 工具函数 ──────────────────────────────────────────────

function decisionLabel(d: string) {
  const map: Record<string, string> = { approved: '通过', rejected: '驳回', escalated: '转人工', error: '错误' }
  return map[d] || d
}

function decisionTagType(d: string) {
  if (d === 'approved') return 'success'
  if (d === 'rejected') return 'danger'
  if (d === 'escalated') return 'warning'
  return 'info'
}

function toggleItem(source: EntityRow | RelationRow) {
  selectedItem.value = selectedItem.value === source ? null : source
}
</script>

<style scoped>
.doc-review-page {
  padding: 16px;
  max-width: 1400px;
  margin: 0 auto;
}

.top-card { margin-bottom: 16px; }

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-weight: 600;
  font-size: 15px;
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ── 表格空状态 ── */

.table-empty {
  padding: 32px 0;
  text-align: center;
}

.table-empty-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* ── 批量审核进度条 ── */

.batch-summary-card { margin-bottom: 16px; }

.batch-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.batch-label {
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
}

.batch-doc-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.batch-chip { cursor: pointer; }

.batch-chip-count {
  margin-left: 6px;
  font-size: 11px;
  opacity: 0.8;
}

/* ── 空状态 ── */

.empty-state {
  padding: 80px 0;
}

/* ── 审核结果区域 ── */

.results-area { margin-bottom: 16px; }

.results-header {
  padding: 16px 20px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px 8px 0 0;
}

.results-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.results-title {
  font-weight: 600;
  font-size: 16px;
}

.results-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  flex-wrap: wrap;
}

.summary-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.summary-item.passed { color: var(--el-color-success); }
.summary-item.failed { color: var(--el-color-danger); }

.summary-divider {
  color: var(--el-border-color);
  margin: 0 4px;
}

/* ── Tab ── */

.review-tabs {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-top: none;
  border-radius: 0 0 8px 8px;
  padding: 0 20px 16px;
}

.tab-empty {
  padding: 40px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

/* ── 步骤卡片 ── */

.step-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.step-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  overflow: hidden;
  transition: border-color 0.2s;
}

.step-card.sc-passed { border-left: 3px solid var(--el-color-success); }
.step-card.sc-failed { border-left: 3px solid var(--el-color-danger); }
.step-card.sc-partial { border-left: 3px solid var(--el-color-warning); }
.step-card.sc-pending { border-left: 3px solid var(--el-border-color); opacity: 0.7; }

.step-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s;
}

.step-card-header:hover {
  background: var(--el-fill-color-lighter);
}

.step-card-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
}

.step-icon { font-size: 18px; flex-shrink: 0; }
.step-icon.passed { color: var(--el-color-success); }
.step-icon.failed { color: var(--el-color-danger); }
.step-icon.skipped { color: var(--el-text-color-placeholder); }

.step-card-title {
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
}

.step-card-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.step-card-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  margin-left: 12px;
}

.step-card-stat {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.expand-icon {
  font-size: 14px;
  color: var(--el-text-color-placeholder);
  transition: transform 0.2s;
}

.expand-icon.expanded { transform: rotate(180deg); }

/* ── 步骤卡片展开内容 ── */

.step-card-body {
  border-top: 1px solid var(--el-border-color-lighter);
}

.step-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px 8px 44px;
  border-bottom: 1px solid var(--el-border-color-extra-light);
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s;
}

.step-item:last-child { border-bottom: none; }
.step-item:hover { background: var(--el-fill-color-lighter); }
.step-item.passed { border-left: 3px solid var(--el-color-success); }
.step-item.failed { border-left: 3px solid var(--el-color-danger); }
.step-item.skipped { border-left: 3px solid var(--el-border-color); opacity: 0.6; }

.step-item-icon { font-size: 14px; flex-shrink: 0; }
.step-item-icon.passed { color: var(--el-color-success); }
.step-item-icon.failed { color: var(--el-color-danger); }
.step-item-icon.skipped { color: var(--el-text-color-placeholder); }

.step-item-info {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}

.step-item-name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-item-type {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  padding: 0 6px;
  border-radius: 3px;
  flex-shrink: 0;
}

.step-item-reason {
  flex: 2;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-item-confidence {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
  flex-shrink: 0;
  width: 36px;
  text-align: right;
}

/* ── 分页 ── */

.doc-pagination {
  display: flex;
  justify-content: flex-end;
  padding: 12px 0 0;
}

/* ── 详情卡片 ── */

.detail-card { margin-top: 16px; }
</style>
