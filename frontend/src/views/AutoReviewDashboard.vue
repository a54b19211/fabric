<template>
  <div class="auto-review-dashboard">
    <!-- 实体审核存量 -->
    <el-row :gutter="16" class="stats-row">
      <el-col :span="4">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" style="color:#E6A23C">{{ entityReviewStats.pending_entities.toLocaleString() }}</div>
          <div class="stat-label">待审核实体</div>
          <div class="stat-hint">extracted_entities · review_status=pending</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" style="color:#67C23A">{{ entityReviewStats.reviewed_entities.toLocaleString() }}</div>
          <div class="stat-label">已审核实体</div>
          <div class="stat-hint">approved + auto_approved + rejected</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 统计面板 -->
    <div class="stats-note">
      统计口径：全部 Agent 运行记录（实体审核 / 关系审核 / 冲突裁决 / 语义匹配），单位 = 条（一次运行 = 审核一条实体或关系）。其中「转人工」仅来自冲突裁决 / 语义匹配 Agent，实体审核不产生转人工；实体审核「通过」= 作为新实体通过（不链接已有字典），消歧由 Agent D 后续处理。
    </div>
    <el-row :gutter="16" class="stats-row">
      <el-col :span="4" v-for="stat in statCards" :key="stat.label">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" :style="{ color: stat.color }">{{ stat.value }}</div>
          <div class="stat-label">{{ stat.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 操作工具栏 -->
    <el-card class="toolbar-card">
      <el-row :gutter="12" align="middle">
        <el-col :span="6">
          <el-input
            v-model="documentUuidInput"
            placeholder="输入文档 UUID"
            clearable
            @keyup.enter="triggerDocumentReview"
          />
        </el-col>
        <el-col :span="4">
          <el-button type="success" @click="triggerDocumentReview" :loading="docReviewLoading">
            审核文档
          </el-button>
        </el-col>
        <el-col :span="1">
          <el-divider direction="vertical" style="height: 32px" />
        </el-col>
        <el-col :span="4">
          <el-input-number v-model="batchLimit" :min="1" :max="500" :step="10" />
        </el-col>
        <el-col :span="4">
          <el-select v-model="filterSourceSite" placeholder="来源站点" clearable filterable>
            <el-option v-for="s in SITES" :key="s" :label="s" :value="s" />
          </el-select>
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="triggerBatch" :loading="batchLoading">
            <el-icon><VideoPlay /></el-icon>
            批量审核 {{ batchLimit }} 篇
          </el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- 视图切换 -->
    <el-card class="view-card">
      <template #header>
        <div class="view-header">
          <span>审核记录</span>
          <el-radio-group v-model="viewMode" size="small">
            <el-radio-button value="document">文档视图</el-radio-button>
            <el-radio-button value="entity">实体视图</el-radio-button>
          </el-radio-group>
          <el-button text style="margin-left: auto" @click="refreshCurrentView">刷新</el-button>
        </div>
      </template>

      <!-- 文档视图 -->
      <div v-if="viewMode === 'document'" v-loading="docRunsLoading">
        <div v-if="docRuns.length === 0" class="empty-state">暂无审核记录</div>

        <div v-for="doc in docRuns" :key="doc.document_uuid || 'no-doc'" class="doc-card">
          <div class="doc-card-header" @click="doc._expanded = !doc._expanded">
            <div class="doc-card-title">
              <el-icon class="expand-icon" :class="{ expanded: doc._expanded }">
                <ArrowRight />
              </el-icon>
              <span class="doc-name">{{ doc.document_title || '未关联文档' }}</span>
              <el-tag size="small" type="info" class="doc-badge">{{ doc.entity_count }} 实体</el-tag>
            </div>
            <div class="doc-card-stats">
              <span class="stat-item approved" v-if="doc.approved_count">
                <el-icon><CircleCheck /></el-icon> {{ doc.approved_count }}
              </span>
              <span class="stat-item rejected" v-if="doc.rejected_count">
                <el-icon><CircleClose /></el-icon> {{ doc.rejected_count }}
              </span>
              <span class="stat-item escalated" v-if="doc.escalated_count">
                <el-icon><Warning /></el-icon> {{ doc.escalated_count }}
              </span>
              <span class="stat-item error" v-if="doc.error_count">
                <el-icon><CircleClose /></el-icon> {{ doc.error_count }}
              </span>
              <span class="stat-item confidence">
                均置信度 {{ (doc.avg_confidence * 100).toFixed(0) }}%
              </span>
              <span class="stat-item time">{{ formatTime(doc.last_review_at) }}</span>
            </div>
          </div>

          <!-- 展开的实体列表 -->
          <div v-if="doc._expanded" class="doc-card-body">
            <el-table :data="doc.entities" size="small" stripe>
              <el-table-column prop="entity_name" label="审核对象" min-width="160" show-overflow-tooltip />
              <el-table-column prop="decision" label="决策" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="decisionType(row.decision)">
                    {{ decisionLabel(row.decision) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="reason" label="理由" min-width="200" show-overflow-tooltip />
              <el-table-column prop="confidence" label="置信度" width="90">
                <template #default="{ row }">
                  <el-progress
                    :percentage="Math.round((row.confidence || 0) * 100)"
                    :stroke-width="6" :show-text="true"
                  />
                </template>
              </el-table-column>
              <el-table-column label="操作" width="60">
                <template #default="{ row }">
                  <el-button text type="primary" size="small" @click.stop="showDetail(row)">
                    查看
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <div class="doc-uuid-hint">UUID: {{ doc.document_uuid }}</div>
          </div>
        </div>

        <div class="pagination-wrap">
          <el-pagination
            v-model:current-page="docPage"
            v-model:page-size="docPageSize"
            :total="docTotal"
            :page-sizes="[10, 20, 50]"
            layout="total, sizes, prev, pager, next"
            @change="loadDocRuns"
          />
        </div>
      </div>

      <!-- 实体视图（原有扁平列表） -->
      <div v-else>
        <el-table :data="runs" v-loading="runsLoading" stripe>
          <el-table-column prop="created_at" label="时间" width="160">
            <template #default="{ row }">
              {{ formatTime(row.created_at) }}
            </template>
          </el-table-column>
          <el-table-column prop="agent_type" label="Agent" width="110">
            <template #default="{ row }">
              <el-tag size="small">{{ agentTypeLabel(row.agent_type) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="entity_name" label="审核对象" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.entity_name">{{ row.entity_name }}</span>
              <span v-else class="text-muted">ID: {{ row.target_id }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="document_title" label="来源文档" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.document_title">{{ row.document_title }}</span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column prop="decision" label="决策" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="decisionType(row.decision)">
                {{ decisionLabel(row.decision) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="confidence" label="置信度" width="90">
            <template #default="{ row }">
              <el-progress :percentage="Math.round((row.confidence || 0) * 100)" :stroke-width="6" :show-text="true" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button text type="primary" size="small" @click="showDetail(row)">
                查看
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="pagination-wrap">
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[10, 20, 50]"
            layout="total, sizes, prev, pager, next"
            @change="loadRuns"
          />
        </div>
      </div>
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="推理详情" size="560px">
      <template v-if="detailRun">
        <el-descriptions :column="1" border size="small" class="detail-desc">
          <el-descriptions-item label="Run ID">{{ detailRun.run_id }}</el-descriptions-item>
          <el-descriptions-item label="Agent">{{ agentTypeLabel(detailRun.agent_type) }}</el-descriptions-item>
          <el-descriptions-item label="审核对象" v-if="detailRun.entity_name">{{ detailRun.entity_name }}</el-descriptions-item>
          <el-descriptions-item label="目标 ID" v-else>{{ detailRun.target_id }}</el-descriptions-item>
          <el-descriptions-item label="来源文档" v-if="detailRun.document_title">{{ detailRun.document_title }}</el-descriptions-item>
          <el-descriptions-item label="决策">
            <el-tag :type="decisionType(detailRun.decision)">{{ decisionLabel(detailRun.decision) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="置信度">{{ (detailRun.confidence * 100).toFixed(0) }}%</el-descriptions-item>
          <el-descriptions-item label="理由">{{ detailRun.reason }}</el-descriptions-item>
        </el-descriptions>

        <h4 style="margin-top: 20px">推理过程</h4>
        <ReasoningTimeline :steps="detailRun.steps || []" :decision="detailRun.decision" :confidence="detailRun.confidence" />
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, watch } from 'vue'
import { VideoPlay, ArrowRight, CircleCheck, CircleClose, Warning } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import {
  getAgentRuns, getAgentStats, getDocumentReviewRuns,
  triggerBatchAsync, triggerDocumentReviewAsync,
  getEntityReviewStats,
  type AgentRunRecord, type DocumentRunSummary,
} from '@/api/agent'
import ReasoningTimeline from '@/components/ReasoningTimeline.vue'

const SITES = [
  'nmpa', 'cde', 'cde_zdyz', 'cde_xxgk', 'fda_press', 'fda_drug_safety',
  'fda_api_drug_label', 'fda_api_enforcement', 'chictr', 'clinicaltrials',
  'pubmed', 'drugbank', 'hkex', 'fiercepharma',
  'phirda_policy', 'phirda_industry', 'phirda_rd', 'phirda_healthcare_reform', 'roche',
]

// 工具栏
const batchLimit = ref(50)
const filterSourceSite = ref('')
const batchLoading = ref(false)

// 文档审核
const documentUuidInput = ref('')
const docReviewLoading = ref(false)

// 视图
const viewMode = ref<'document' | 'entity'>('document')

// 实体视图（原有）
const runs = ref<AgentRunRecord[]>([])
const runsLoading = ref(false)
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)

// 文档视图
const docRunsLoading = ref(false)
const docPage = ref(1)
const docPageSize = ref(20)
const docTotal = ref(0)
const docRuns = ref<(DocumentRunSummary & { _expanded?: boolean })[]>([])

// 统计
const stats = ref<Record<string, Record<string, number>>>({})
const entityReviewStats = ref({ pending_entities: 0, reviewed_entities: 0 })
const statCards = reactive([
  { label: '总运行', value: 0, color: '#409EFF', key: 'total' },
  { label: '通过/批准', value: 0, color: '#67C23A', key: 'approved' },
  { label: '拒绝', value: 0, color: '#F56C6C', key: 'rejected' },
  { label: '转人工', value: 0, color: '#E6A23C', key: 'escalated' },
  { label: '错误', value: 0, color: '#909399', key: 'error' },
])

const detailVisible = ref(false)
const detailRun = ref<AgentRunRecord | null>(null)

onMounted(() => {
  loadCurrentView()
  loadStats()
})

watch([viewMode], () => {
  loadCurrentView()
})

function loadCurrentView() {
  if (viewMode.value === 'document') {
    loadDocRuns()
  } else {
    loadRuns()
  }
}

function refreshCurrentView() {
  loadCurrentView()
  loadStats()
}

// ── 文档视图 ──────────────────────────────────────────

async function loadDocRuns() {
  docRunsLoading.value = true
  try {
    const res = await getDocumentReviewRuns({
      page: docPage.value,
      page_size: docPageSize.value,
    })
    docRuns.value = (res.items || []).map(d => ({ ...d, _expanded: false }))
    docTotal.value = res.total
  } catch {
    // ignore
  } finally {
    docRunsLoading.value = false
  }
}

async function triggerDocumentReview() {
  const uuid = documentUuidInput.value.trim()
  if (!uuid) {
    ElMessage.warning('请输入文档 UUID')
    return
  }
  docReviewLoading.value = true
  try {
    const res = await triggerDocumentReviewAsync(uuid)
    ElMessage.success(res.message || `文档 ${uuid} 已入队审核`)
    documentUuidInput.value = ''
    setTimeout(() => { loadDocRuns(); loadStats() }, 3000)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '审核失败')
  } finally {
    docReviewLoading.value = false
  }
}

// ── 实体视图 ──────────────────────────────────────────

async function loadRuns() {
  runsLoading.value = true
  try {
    const res = await getAgentRuns({
      agent_type: 'entity_review',
      page: page.value,
      page_size: pageSize.value,
    })
    runs.value = res.items
    total.value = res.total
  } catch {
    // ignore
  } finally {
    runsLoading.value = false
  }
}

// ── 统计 ──────────────────────────────────────────────

async function loadStats() {
  try {
    const res = await getAgentStats()
    stats.value = res.stats
    updateStatCards()
  } catch {
    // ignore
  }
  try {
    entityReviewStats.value = await getEntityReviewStats()
  } catch {
    // ignore
  }
}

function updateStatCards() {
  let totalRuns = 0, approved = 0, rejected = 0, escalated = 0, error = 0
  for (const decisions of Object.values(stats.value)) {
    for (const [decision, cnt] of Object.entries(decisions)) {
      totalRuns += cnt
      if (['approved', 'keep_existing', 'accept_new'].includes(decision)) approved += cnt
      else if (decision === 'rejected') rejected += cnt
      else if (decision === 'escalated') escalated += cnt
      else if (decision === 'error') error += cnt
    }
  }
  statCards[0].value = totalRuns
  statCards[1].value = approved
  statCards[2].value = rejected
  statCards[3].value = escalated
  statCards[4].value = error
}

// ── 批量审核 ──────────────────────────────────────────

async function triggerBatch() {
  batchLoading.value = true
  try {
    const res = await triggerBatchAsync({
      limit: batchLimit.value,
      source_site: filterSourceSite.value,
    })
    ElMessage.success(res.message || `已入队 ${batchLimit.value} 篇异步审核`)
    setTimeout(() => { loadCurrentView(); loadStats() }, 3000)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '触发失败')
  } finally {
    batchLoading.value = false
  }
}

// ── 详情 ──────────────────────────────────────────────

function showDetail(row: AgentRunRecord) {
  detailRun.value = row
  detailVisible.value = true
}

// ── 工具函数 ──────────────────────────────────────────

function formatTime(ts: string) {
  if (!ts) return '-'
  const d = new Date(ts)
  return d.toLocaleString('zh-CN', { hour12: false })
}

function agentTypeLabel(type: string) {
  const map: Record<string, string> = {
    entity_review: '实体审核',
    relation_review: '关系审核',
    conflict_resolution: '冲突裁决',
    semantic_match: '语义匹配',
  }
  return map[type] || type
}

function decisionLabel(d: string) {
  const map: Record<string, string> = {
    approved: '通过（新建实体）',
    rejected: '拒绝',
    escalated: '转人工',
    keep_existing: '保留现有',
    accept_new: '接受新值',
    custom: '自定义',
    error: '错误',
  }
  return map[d] || d
}

function decisionType(d: string) {
  if (['approved', 'accept_new'].includes(d)) return 'success'
  if (d === 'rejected') return 'danger'
  if (d === 'escalated') return 'warning'
  if (d === 'keep_existing') return 'info'
  if (d === 'error') return 'info'
  return ''
}
</script>

<style scoped>
.auto-review-dashboard {
  padding: 16px;
  max-width: 1400px;
  margin: 0 auto;
}

.stats-note {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 10px;
  line-height: 1.5;
}

.stats-row {
  margin-bottom: 16px;
}

.stat-card {
  text-align: center;
}

.stat-value {
  font-size: 28px;
  font-weight: 700;
}

.stat-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}

.stat-hint {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
  margin-top: 2px;
}

.toolbar-card {
  margin-bottom: 16px;
}

.view-card {
  margin-bottom: 16px;
}

.view-header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.empty-state {
  text-align: center;
  padding: 60px 0;
  color: var(--el-text-color-placeholder);
  font-size: 14px;
}

/* 文档卡片 */
.doc-card {
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  margin-bottom: 12px;
  overflow: hidden;
}

.doc-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  cursor: pointer;
  background: var(--el-fill-color-light);
  transition: background 0.2s;
  flex-wrap: wrap;
  gap: 8px;
}

.doc-card-header:hover {
  background: var(--el-fill-color);
}

.doc-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 15px;
}

.expand-icon {
  transition: transform 0.2s;
  font-size: 12px;
}

.expand-icon.expanded {
  transform: rotate(90deg);
}

.doc-name {
  max-width: 400px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-badge {
  flex-shrink: 0;
}

.doc-card-stats {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.stat-item {
  display: flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}

.stat-item.approved { color: #67C23A; }
.stat-item.rejected { color: #F56C6C; }
.stat-item.escalated { color: #E6A23C; }
.stat-item.error { color: #909399; }
.stat-item.confidence { color: var(--el-text-color-regular); }

.doc-card-body {
  padding: 0 16px 12px;
}

.doc-uuid-hint {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  margin-top: 8px;
  font-family: monospace;
}

.pagination-wrap {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}

.text-muted {
  color: var(--el-text-color-placeholder);
}

.detail-desc {
  margin-bottom: 8px;
}
</style>
