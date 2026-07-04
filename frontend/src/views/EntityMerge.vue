<template>
  <div class="entity-merge-page">
    <!-- 顶部统计 -->
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total }}</div>
        <div class="stat-label">可消歧实体</div>
      </div>
      <div class="stat-card" v-for="(count, type) in statsTopTypes" :key="type">
        <div class="stat-value">{{ count }}</div>
        <div class="stat-label">{{ type }}</div>
      </div>
      <div class="stat-card queue-stat-card" :class="{ running: queueStatus?.running }">
        <div class="stat-value">
          <span v-if="queueStatus">{{ queueStatus.pending }}</span>
          <span v-else>—</span>
        </div>
        <div class="stat-label">
          {{ queueStatus?.running ? '队列运行中' : '队列空闲' }}
        </div>
        <div class="queue-detail" v-if="queueStatus">
          完成 {{ queueStatus.completed }} / 失败 {{ queueStatus.failed }}
        </div>
      </div>
    </div>

    <!-- 来源分布卡片 -->
    <div class="source-stats-row">
      <span class="source-label">按来源：</span>
      <el-tag
        v-for="(count, site) in stats.by_source"
        :key="site"
        size="small"
        type="info"
        effect="plain"
        style="cursor:pointer"
        :class="{ 'source-active': filters.source_site === site }"
        @click="toggleSourceFilter(site)"
      >
        {{ site }} {{ count }}
      </el-tag>
    </div>

    <!-- 筛选栏 -->
    <div class="filter-bar">
      <el-input
        v-model="filters.search"
        placeholder="搜索实体名称..."
        clearable
        size="small"
        style="width:200px"
        @keyup.enter="applyFilters"
        @clear="applyFilters"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>

      <el-select v-model="filters.entity_type" placeholder="实体类型" clearable size="small" style="width:140px" @change="applyFilters">
        <el-option v-for="t in typeOptions" :key="t" :label="t" :value="t" />
      </el-select>

      <div class="confidence-filter">
        <span class="filter-label">置信度</span>
        <el-input-number
          v-model="filters.conf_min"
          :min="0" :max="1" :step="0.1" :precision="1"
          size="small" style="width:80px"
          @change="applyFilters"
        />
        <span>—</span>
        <el-input-number
          v-model="filters.conf_max"
          :min="0" :max="1" :step="0.1" :precision="1"
          size="small" style="width:80px"
          @change="applyFilters"
        />
      </div>

      <el-select v-model="filters.has_dict" placeholder="字典匹配" clearable size="small" style="width:120px" @change="applyFilters">
        <el-option label="有匹配" :value="true" />
        <el-option label="无匹配" :value="false" />
      </el-select>

      <el-button size="small" @click="resetFilters">重置</el-button>
      <el-button size="small" type="primary" @click="applyFilters" :loading="loading">查询</el-button>
    </div>

    <!-- 批量操作 + 队列状态 -->
    <div class="batch-bar">
      <div class="batch-left">
        <span class="batch-label">批量消歧</span>
        <el-input-number v-model="mergeCount" :min="1" :max="200" size="small" style="width:120px" />
        <span class="batch-count">条</span>
        <el-button size="small" type="warning" @click="doBatchMerge" :loading="batchMerging">
          入队
        </el-button>
      </div>
      <div class="batch-right">
        <el-button size="small" :disabled="selectedIds.length === 0" @click="doMergeSelected" :loading="mergingSelected">
          消歧选中 ({{ selectedIds.length }})
        </el-button>
        <el-button size="small" @click="loadCandidates" :loading="loading">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- 实体列表 -->
    <div class="entity-table-wrapper" v-loading="loading">
      <el-empty v-if="!entities.length && !loading" description="暂无已审核实体需要消歧" :image-size="80" />

      <template v-else>
        <el-table
          ref="tableRef"
          :data="entities"
          stripe
          size="small"
          style="width:100%"
          @selection-change="onSelectionChange"
        >
          <el-table-column type="selection" width="36" />
          <el-table-column prop="id" label="ID" width="55" />
          <el-table-column prop="entity_name" label="实体名称" min-width="150">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="toggleExpand(row)">
                {{ row.entity_name }}
              </el-button>
            </template>
          </el-table-column>
          <el-table-column prop="entity_type" label="类型" width="100">
            <template #default="{ row }">
              <el-tag size="small">{{ row.entity_type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="source_site" label="来源" width="100" />
          <el-table-column label="文档" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">
              <router-link
                v-if="row.source_doc_uuid"
                :to="`/review/${row.source_doc_uuid}`"
                class="doc-link"
              >
                {{ row.document_title || row.source_doc_uuid.slice(0, 12) + '...' }}
              </router-link>
              <span v-else class="text-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="属性摘要" min-width="140">
            <template #default="{ row }">
              <div class="attr-preview">
                <span v-for="(val, key) in topFields(row.entity_data)" :key="key" class="attr-chip">
                  <em>{{ key }}</em>{{ formatVal(val) }}
                </span>
                <span v-if="!row.entity_data || !Object.keys(row.entity_data).length" class="text-muted">—</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="confidence_score" label="置信度" width="85" align="center">
            <template #default="{ row }">
              <span :class="confidenceClass(row.confidence_score)">
                {{ (row.confidence_score * 100).toFixed(0) }}%
              </span>
            </template>
          </el-table-column>
          <el-table-column label="字典匹配" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.dict_id" size="small" type="success" effect="plain">
                {{ row.dict_name || '#' + row.dict_id }}
              </el-tag>
              <el-tag v-else size="small" type="info" effect="plain">新实体</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" align="center" fixed="right">
            <template #default="{ row }">
              <el-button
                size="small"
                type="primary"
                @click="doMergeEntity(row.id, row.entity_name)"
                :loading="streamingRunId === row.id"
              >
                消歧
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="pagination-wrapper">
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[10, 20, 50, 100]"
            layout="total, sizes, prev, pager, next, jumper"
            small
            @current-change="loadCandidates"
            @size-change="onPageSizeChange"
          />
        </div>
      </template>
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawerVisible" :title="drawerEntity?.entity_name" size="620px">
      <template v-if="drawerEntity">
        <div class="drawer-section">
          <h4>基本信息</h4>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="ID">{{ drawerEntity.id }}</el-descriptions-item>
            <el-descriptions-item label="类型">
              <el-tag size="small">{{ drawerEntity.entity_type }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="来源站点">{{ drawerEntity.source_site }}</el-descriptions-item>
            <el-descriptions-item label="置信度">
              <span :class="confidenceClass(drawerEntity.confidence_score)">
                {{ (drawerEntity.confidence_score * 100).toFixed(0) }}%
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="来源文档" :span="2">
              <router-link
                v-if="drawerEntity.source_doc_uuid"
                :to="`/review/${drawerEntity.source_doc_uuid}`"
              >
                {{ drawerEntity.document_title || drawerEntity.source_doc_uuid }}
              </router-link>
              <span v-else class="text-muted">—</span>
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <div class="drawer-section" v-if="drawerEntity.entity_data && Object.keys(drawerEntity.entity_data).length">
          <h4>完整属性</h4>
          <el-table :data="entityDataRows" size="small" stripe max-height="260">
            <el-table-column prop="key" label="字段" width="160" />
            <el-table-column prop="value" label="值">
              <template #default="{ row }">
                <span v-if="row.key === 'source_text'" class="source-text-preview">{{ formatVal(row.value) }}</span>
                <span v-else>{{ formatVal(row.value) }}</span>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div class="drawer-section" v-if="drawerEntity.dict_id">
          <h4>字典匹配</h4>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="字典 ID">{{ drawerEntity.dict_id }}</el-descriptions-item>
            <el-descriptions-item label="字典名称">{{ drawerEntity.dict_name }}</el-descriptions-item>
          </el-descriptions>
        </div>

        <!-- 消歧历史 -->
        <div class="drawer-section">
          <h4>
            消歧记录
            <span v-if="mergeHistory.length" class="history-count">{{ mergeHistory.length }} 次</span>
          </h4>

          <!-- 历史记录列表 -->
          <div v-if="mergeHistory.length" class="run-history-list">
            <div
              v-for="run in mergeHistory"
              :key="run.run_id"
              class="run-card"
              :class="run.decision"
            >
              <div class="run-card-header" @click="toggleRunExpanded(run)">
                <div class="run-summary">
                  <el-tag
                    :type="run.decision === 'linked' ? 'success' : 'warning'"
                    size="small"
                  >
                    {{ run.decision === 'linked' ? '已链接' : '转人工' }}
                  </el-tag>
                  <span class="run-confidence">{{ (run.confidence * 100).toFixed(0) }}%</span>
                </div>
                <div class="run-meta">
                  <span class="run-time">{{ formatTime(run.created_at) }}</span>
                  <el-icon><ArrowDown v-if="!expandedRuns[run.run_id]" /><ArrowUp v-else /></el-icon>
                </div>
              </div>
              <div v-if="expandedRuns[run.run_id]" class="run-steps">
                <div class="step-reason">{{ run.reason }}</div>
                <div
                  v-for="step in asAgentSteps(run.steps)"
                  :key="step.step"
                  class="step-item"
                  :class="step.type === 'ai' ? 'agent' : 'tools'"
                >
                  <div class="step-header">
                    <span class="step-no">#{{ step.step }}</span>
                    <el-tag v-if="step.type === 'ai'" size="small" type="primary">AI 推理</el-tag>
                    <el-tag v-else size="small" type="warning">{{ step.tool_name || '工具' }}</el-tag>
                  </div>
                  <div v-if="isSearchTool(step.tool_name) && step.tool_input?.name" class="search-name">
                    搜: <strong>{{ step.tool_input.name }}</strong>
                  </div>
                  <div v-if="step.tool_input && Object.keys(step.tool_input).length" class="step-input">
                    <code>{{ JSON.stringify(step.tool_input) }}</code>
                  </div>
                  <div class="step-content">{{ step.content }}</div>
                </div>
              </div>
            </div>
          </div>

          <!-- 实时流式推理 -->
          <div v-if="streamingRunId === drawerEntity.id" class="run-card live">
            <div class="run-card-header">
              <div class="run-summary">
                <span class="status-dot" :class="streamingDone ? 'done' : 'running'" />
                <span>{{ streamingDone ? '推理完成' : '推理中' }} ({{ liveStreamSteps.length }} 步)</span>
              </div>
            </div>
            <div class="run-steps">
              <!-- System / Human Prompt -->
              <details v-if="liveStreamPrompt" class="prompt-block">
                <summary>System Prompt ({{ liveStreamPrompt.system_prompt.length }} 字符)</summary>
                <pre class="prompt-text">{{ liveStreamPrompt.system_prompt }}</pre>
              </details>
              <details v-if="liveStreamPrompt" class="prompt-block">
                <summary>Human Prompt ({{ liveStreamPrompt.human_prompt.length }} 字符)</summary>
                <pre class="prompt-text">{{ liveStreamPrompt.human_prompt }}</pre>
              </details>

              <!-- 统一事件流：llm_input → agent 回复 → tool 结果 → llm_input → ... -->
              <template v-for="(evt, ei) in liveEvents" :key="`evt-${ei}`">
                <!-- llm_input: 发给 LLM 的完整上下文 -->
                <details v-if="evt.type === 'llm_input' && evt.messages" class="llm-input-block">
                  <summary>发给 LLM — {{ evt.messages.length }} 条消息</summary>
                  <div class="msg-list">
                    <div
                      v-for="(m, mi) in evt.messages"
                      :key="`m-${mi}`"
                      class="msg-item"
                      :class="m.role.toLowerCase().replace('message', '')"
                    >
                      <span class="msg-role">{{ m.role }}</span>
                      <div v-if="m.content" class="msg-content">{{ m.content }}</div>
                      <div v-if="m.tool_calls?.length" class="msg-toolcalls">
                        调用: {{ m.tool_calls.map(tc => `${tc.name}(${JSON.stringify(tc.args)})`).join(', ') }}
                      </div>
                      <div v-if="m.name" class="msg-tool-name">工具名: {{ m.name }}</div>
                    </div>
                  </div>
                </details>

                <!-- step: agent / tools / remind -->
                <div v-if="evt.type === 'step'" class="step-item" :class="evt.node">
                  <div class="step-header">
                    <span class="step-no">#{{ evt.step }}</span>
                    <el-tag v-if="evt.node === 'agent'" size="small" type="primary">LLM 回复</el-tag>
                    <el-tag v-else-if="evt.node === 'tools'" size="small" type="warning">{{ evt.tool_name || '工具结果' }}</el-tag>
                    <el-tag v-else-if="evt.node === 'remind'" size="small" type="danger">系统提醒</el-tag>
                    <el-tag v-else size="small" type="info">{{ evt.node }}</el-tag>
                    <span v-if="evt.tool_calls?.length" class="tool-call-names">
                      调用: {{ evt.tool_calls.join(', ') }}
                    </span>
                  </div>
                  <div v-if="isSearchTool(evt.tool_name) && evt.tool_input?.name" class="search-name">
                    搜: <strong>{{ evt.tool_input.name }}</strong>
                  </div>
                  <div v-if="evt.tool_input && Object.keys(evt.tool_input).length && !(isSearchTool(evt.tool_name) && Object.keys(evt.tool_input).length === 3 && evt.tool_input.name && evt.tool_input.entity_type && evt.tool_input.exclude_dict_id !== undefined)" class="step-input">
                    <details>
                      <summary>入参</summary>
                      <pre class="prompt-text">{{ JSON.stringify(evt.tool_input, null, 2) }}</pre>
                    </details>
                  </div>
                  <div class="step-content live-content" v-if="evt.content">{{ evt.content }}</div>
                </div>
              </template>

              <div v-if="liveStreamLoading && liveEvents.length === 0" class="step-item loading">
                <span class="dot-typing">…</span>
              </div>
            </div>
            <!-- 实时结果 -->
            <div v-if="liveStreamResult" class="run-result" :class="liveStreamResult.decision">
              <div class="result-header">
                <el-tag
                  :type="liveStreamResult.decision === 'linked' ? 'success' : 'warning'"
                  size="small"
                >
                  {{ liveStreamResult.decision === 'linked' ? '已链接' : '转人工' }}
                </el-tag>
                <span v-if="liveStreamResult.dict_id" class="result-dict">
                  目标: dict #{{ liveStreamResult.dict_id }}
                </span>
                <span class="result-conf">置信度 {{ ((liveStreamResult.confidence ?? 0) * 100).toFixed(0) }}%</span>
              </div>
              <div class="result-reason" v-if="liveStreamResult.reason">{{ liveStreamResult.reason }}</div>
              <div v-if="liveStreamResult.merge_queue_id" class="result-proposal">
                <el-tag type="primary" effect="dark" size="small">已创建合并提案 #{{ liveStreamResult.merge_queue_id }}</el-tag>
                <el-button size="small" text type="primary" @click="$router.push('/merge-proposals')">
                  查看提案 →
                </el-button>
              </div>
            </div>
          </div>
        </div>

        <div class="drawer-actions">
          <el-button
            type="primary"
            @click="doMergeEntity(drawerEntity.id, drawerEntity.entity_name)"
            :loading="streamingRunId === drawerEntity.id"
          >
            执行消歧
          </el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Search, Refresh, ArrowDown, ArrowUp } from '@element-plus/icons-vue'
import {
  getMergeCandidates,
  getMergeCandidatesStats,
  triggerEntityMerge,
  triggerBatchMerge,
  getMergeQueueStatus,
  getEntityMergeRuns,
  streamEntityMerge,
  type MergeCandidate,
  type MergeCandidatesParams,
  type MergeCandidateStats,
  type MergeQueueStatus,
  type MergeStreamEvent,
  type LLMMessage,
  type AgentRunRecord,
  type AgentStep,
} from '@/api/agent'

// ── 状态 ──────────────────────────────────────────────────────

const entities = ref<MergeCandidate[]>([])
const total = ref(0)
const loading = ref(false)
const mergingSelected = ref(false)

const page = ref(1)
const pageSize = ref(20)

const selectedIds = ref<number[]>([])

const filters = reactive<MergeCandidatesParams>({
  search: '',
  entity_type: '',
  source_site: '',
  conf_min: 0,
  conf_max: 1,
  has_dict: null,
})

const stats = ref<MergeCandidateStats>({ total: 0, by_type: {}, by_source: {} })

const mergeCount = ref(10)
const batchMerging = ref(false)
const queueStatus = ref<MergeQueueStatus | null>(null)
let mergeStatusTimer: ReturnType<typeof setInterval> | null = null

// 详情抽屉
const drawerVisible = ref(false)
const drawerEntity = ref<MergeCandidate | null>(null)

// 消歧历史
const mergeHistory = ref<AgentRunRecord[]>([])
const expandedRuns = reactive<Record<string, boolean>>({})

// 实时流式推理（在抽屉内）
const streamingRunId = ref(0)
const streamingDone = ref(false)  // 流式完成后保持展示
const liveStreamSteps = ref<MergeStreamEvent[]>([])
const liveStreamLoading = ref(false)
const liveStreamResult = ref<MergeStreamEvent | null>(null)
const liveStreamPrompt = ref<{ system_prompt: string; human_prompt: string } | null>(null)
const liveEvents = ref<MergeStreamEvent[]>([])  // 统一事件流：prompt/llm_input/step 按时间顺序
let mergeStreamCancel: (() => void) | null = null

// ── 计算属性 ──────────────────────────────────────────────────

const statsTopTypes = computed(() => {
  const entries = Object.entries(stats.value.by_type)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
  return Object.fromEntries(entries)
})

const typeOptions = computed(() => Object.keys(stats.value.by_type))

const entityDataRows = computed(() => {
  if (!drawerEntity.value?.entity_data) return []
  return Object.entries(drawerEntity.value.entity_data).map(([key, value]) => ({ key, value }))
})

// ── 方法 ──────────────────────────────────────────────────────

function buildParams(): MergeCandidatesParams {
  const p: MergeCandidatesParams = {
    page: page.value,
    page_size: pageSize.value,
  }
  if (filters.search) p.search = filters.search
  if (filters.entity_type) p.entity_type = filters.entity_type
  if (filters.source_site) p.source_site = filters.source_site
  if ((filters.conf_min ?? 0) > 0) p.conf_min = filters.conf_min
  if ((filters.conf_max ?? 1) < 1) p.conf_max = filters.conf_max
  if (filters.has_dict != null) p.has_dict = filters.has_dict
  return p
}

async function loadCandidates() {
  loading.value = true
  try {
    const res = await getMergeCandidates(buildParams())
    entities.value = res.items
    total.value = res.total
  } catch (e: any) {
    ElMessage.error('加载失败: ' + (e.message || ''))
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try {
    stats.value = await getMergeCandidatesStats()
  } catch { /* ignore */ }
}

function applyFilters() {
  page.value = 1
  loadCandidates()
}

function resetFilters() {
  filters.search = ''
  filters.entity_type = ''
  filters.source_site = ''
  filters.conf_min = 0
  filters.conf_max = 1
  filters.has_dict = null
  page.value = 1
  loadCandidates()
}

function toggleSourceFilter(site: string) {
  filters.source_site = filters.source_site === site ? '' : site
  applyFilters()
}

function onPageSizeChange() {
  page.value = 1
  loadCandidates()
}

function onSelectionChange(rows: MergeCandidate[]) {
  selectedIds.value = rows.map(r => r.id)
}

async function toggleExpand(entity: MergeCandidate) {
  drawerEntity.value = entity
  drawerVisible.value = true
  // 加载消歧历史
  mergeHistory.value = []
  Object.keys(expandedRuns).forEach(k => delete expandedRuns[k])
  try {
    const res = await getEntityMergeRuns(entity.id)
    mergeHistory.value = res.items
  } catch { /* ignore */ }
}

function toggleRunExpanded(run: AgentRunRecord) {
  expandedRuns[run.run_id] = !expandedRuns[run.run_id]
}

// ── 消歧操作 ────────────────────────────────────────────────

function doMergeEntity(entityId: number, _entityName?: string) {
  // 在抽屉内开始 SSE 流式推理
  streamingRunId.value = entityId
  streamingDone.value = false
  liveStreamSteps.value = []
  liveStreamResult.value = null
  liveStreamPrompt.value = null
  liveEvents.value = []
  liveStreamLoading.value = true

  let resultHandled = false

  mergeStreamCancel = streamEntityMerge(
    entityId,
    (event) => {
      if (event.type === 'prompt') {
        liveStreamPrompt.value = {
          system_prompt: event.system_prompt || '',
          human_prompt: event.human_prompt || '',
        }
        liveEvents.value = [...liveEvents.value, event]
      } else if (event.type === 'llm_input') {
        liveEvents.value = [...liveEvents.value, event]
      } else if (event.type === 'status') {
        liveStreamLoading.value = event.status === 'running'
      } else if (event.type === 'step') {
        liveStreamSteps.value = [...liveStreamSteps.value, event]
        liveEvents.value = [...liveEvents.value, event]
        liveStreamLoading.value = false
      } else if (event.type === 'result') {
        liveStreamResult.value = event
        liveStreamLoading.value = false
        resultHandled = true
        // 重新加载历史和候选列表
        if (drawerEntity.value) {
          getEntityMergeRuns(drawerEntity.value.id).then(res => {
            mergeHistory.value = res.items
          }).catch(() => {})
        }
        loadCandidates()
        loadStats()
      } else if (event.type === 'error') {
        liveStreamResult.value = { type: 'result', decision: 'error', message: event.message }
        liveStreamLoading.value = false
        if (!resultHandled) {
          resultHandled = true
        }
      } else if (event.type === 'done') {
        streamingDone.value = true
        liveStreamLoading.value = false
      }
    },
    () => {
      if (!resultHandled) {
        liveStreamResult.value = { type: 'result', decision: 'error', message: 'SSE 连接异常' }
        liveStreamLoading.value = false
        streamingRunId.value = 0
      }
    },
  )
}

function asAgentSteps(steps: AgentRunRecord['steps']): AgentStep[] {
  return steps as AgentStep[]
}

function formatTime(isoStr: string): string {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function isSearchTool(name?: string): boolean {
  return name === 'exact_search' || name === 'fuzzy_search'
}

async function doMergeSelected() {
  if (selectedIds.value.length === 0) return
  mergingSelected.value = true
  try {
    for (const id of selectedIds.value) {
      await triggerEntityMerge(id)
    }
    ElMessage.success(`已完成 ${selectedIds.value.length} 个实体的消歧`)
    selectedIds.value = []
    await loadCandidates()
    await loadStats()
  } catch (e: any) {
    ElMessage.error(e.message || '消歧选中失败')
  } finally {
    mergingSelected.value = false
  }
}

async function doBatchMerge() {
  batchMerging.value = true
  try {
    const status = await triggerBatchMerge(mergeCount.value)
    queueStatus.value = status
    ElMessage.success(`已入队 ${mergeCount.value} 个消歧任务`)
    startMergeStatusPolling()
    await loadCandidates()
    await loadStats()
  } catch (e: any) {
    ElMessage.error(e.message || '批量消歧失败')
  } finally {
    batchMerging.value = false
  }
}

function startMergeStatusPolling() {
  if (mergeStatusTimer) clearInterval(mergeStatusTimer)
  mergeStatusTimer = setInterval(async () => {
    try {
      const s = await getMergeQueueStatus()
      queueStatus.value = s
      if (s.pending === 0 && !s.running) {
        if (mergeStatusTimer) clearInterval(mergeStatusTimer)
        mergeStatusTimer = null
        loadCandidates()
        loadStats()
      }
    } catch { /* ignore */ }
  }, 3000)
}

// ── 工具函数 ────────────────────────────────────────────────

function topFields(data: Record<string, any> | null): Record<string, any> {
  if (!data) return {}
  const keys = Object.keys(data).filter(k => k !== 'source_text').slice(0, 3)
  return Object.fromEntries(keys.map(k => [k, data[k]]))
}

function formatVal(val: any): string {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'object') return JSON.stringify(val, null, 0).slice(0, 80)
  const s = String(val)
  return s.length > 60 ? s.slice(0, 60) + '...' : s
}

function confidenceClass(score: number) {
  if (score >= 0.8) return 'conf-high'
  if (score >= 0.5) return 'conf-mid'
  return 'conf-low'
}

// ── 生命周期 ─────────────────────────────────────────────────

onMounted(() => {
  loadCandidates()
  loadStats()
  getMergeQueueStatus().then(s => { queueStatus.value = s }).catch(() => {})
})

onUnmounted(() => {
  if (mergeStatusTimer) clearInterval(mergeStatusTimer)
  if (mergeStreamCancel) mergeStreamCancel()
})
</script>

<style scoped>
.entity-merge-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
}

/* 统计卡片 */
.stats-row {
  display: flex;
  gap: 10px;
  flex-shrink: 0;
}
.stat-card {
  background: var(--dp-card);
  border: 1px solid var(--dp-border);
  border-radius: var(--dp-radius-md);
  padding: 10px 16px;
  min-width: 100px;
  text-align: center;
}
.stat-value { font-size: 22px; font-weight: 700; color: var(--dp-text-regular); }
.stat-label { font-size: 12px; color: var(--dp-text-secondary); margin-top: 2px; }
.queue-detail { font-size: 11px; color: var(--dp-text-secondary); margin-top: 2px; }
.queue-stat-card.running { border-color: var(--dp-warning); }
.queue-stat-card.running .stat-value { color: var(--dp-warning); }

/* 来源分布 */
.source-stats-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  flex-shrink: 0;
}
.source-label { font-size: 13px; color: var(--dp-text-secondary); margin-right: 4px; }
.source-active { border-color: var(--dp-primary) !important; color: var(--dp-primary) !important; }

/* 筛选栏 */
.filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  flex-wrap: wrap;
}
.confidence-filter {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: var(--dp-text-secondary);
}

/* 批量操作栏 */
.batch-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: var(--dp-card);
  border: 1px solid var(--dp-border);
  border-radius: var(--dp-radius-md);
  flex-shrink: 0;
}
.batch-left, .batch-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.batch-label { font-size: 13px; color: var(--dp-text-secondary); }
.batch-count { font-size: 13px; color: var(--dp-text-secondary); }

/* 表格容器 */
.entity-table-wrapper {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--dp-card);
  border: 1px solid var(--dp-border);
  border-radius: var(--dp-radius-md);
  overflow: hidden;
  min-height: 0;
}

.doc-link {
  color: var(--dp-primary);
  text-decoration: none;
  font-size: 13px;
}
.doc-link:hover { text-decoration: underline; }

.attr-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
}
.attr-chip {
  background: var(--dp-muted-bg);
  border-radius: var(--dp-radius-xs);
  padding: 1px 6px;
  font-size: 11px;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attr-chip em {
  font-style: normal;
  color: var(--dp-text-secondary);
  margin-right: 3px;
}

.text-muted { color: var(--dp-text-placeholder); font-size: 13px; }

.conf-high { color: var(--dp-success); font-weight: 600; }
.conf-mid { color: var(--dp-warning); font-weight: 500; }
.conf-low { color: var(--dp-danger); }

.pagination-wrapper {
  display: flex;
  justify-content: center;
  padding: 10px 0;
  margin-top: auto;
  border-top: 1px solid var(--dp-border);
}

/* 抽屉 */
.drawer-section {
  margin-bottom: 20px;
}
.drawer-section h4 {
  margin: 0 0 8px 0;
  font-size: 14px;
  color: var(--dp-text-regular);
}
.source-text-preview {
  display: block;
  max-height: 200px;
  overflow-y: auto;
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.5;
}
.drawer-actions {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
  border-top: 1px solid var(--dp-border);
}

/* 消歧历史 + 实时推理 */
.history-count {
  font-size: 12px;
  color: var(--dp-text-placeholder);
  font-weight: 400;
  margin-left: 6px;
}
.run-history-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.run-card {
  border: 1px solid var(--dp-border);
  border-radius: var(--dp-radius-sm);
  overflow: hidden;
}
.run-card.live {
  border-color: var(--dp-warning);
  background: var(--dp-warning-light, #fff8e1);
}
.run-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s;
}
.run-card-header:hover {
  background: var(--dp-muted-bg);
}
.run-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.run-dict {
  font-size: 12px;
  color: var(--dp-text-secondary);
  font-family: monospace;
}
.run-confidence {
  font-size: 12px;
  color: var(--dp-text-placeholder);
}
.run-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--dp-text-placeholder);
}
.run-steps {
  border-top: 1px solid var(--dp-border);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--dp-muted-bg);
}
.step-reason {
  font-size: 13px;
  color: var(--dp-text-regular);
  line-height: 1.6;
  white-space: pre-wrap;
  padding-bottom: 6px;
  border-bottom: 1px dashed var(--dp-border);
  margin-bottom: 4px;
}
.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}
.status-dot.running {
  background: var(--dp-warning);
  animation: pulse 1s infinite;
}
.status-dot.done {
  background: var(--dp-success);
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
.step-item {
  border-left: 3px solid var(--dp-border);
  padding: 6px 10px;
  border-radius: 0 var(--dp-radius-xs) var(--dp-radius-xs) 0;
}
.step-item.agent { border-left-color: var(--dp-primary); }
.step-item.tools { border-left-color: var(--dp-warning); }
.step-item.loading { border-left-color: var(--dp-text-placeholder); border-left-style: dashed; }
.step-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 3px;
}
.step-no {
  font-size: 12px;
  color: var(--dp-text-placeholder);
  font-weight: 500;
}
.step-input {
  margin-bottom: 3px;
}
.search-name {
  font-size: 14px;
  color: var(--dp-text-regular);
  margin-bottom: 4px;
  padding: 3px 0;
}
.search-name strong {
  color: var(--dp-primary);
  font-weight: 700;
  background: var(--dp-primary-light, #e3f2fd);
  padding: 1px 6px;
  border-radius: var(--dp-radius-xs);
}
.step-input code {
  font-size: 12px;
  color: var(--dp-text-secondary);
  background: var(--dp-muted-bg);
  padding: 2px 6px;
  border-radius: var(--dp-radius-xs);
  word-break: break-all;
}
.step-content {
  font-size: 13px;
  color: var(--dp-text-regular);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 100px;
  overflow-y: auto;
}
.step-content.live-content {
  max-height: none;
  overflow-y: visible;
}
.prompt-block {
  margin-bottom: 6px;
}
.prompt-block summary {
  font-size: 12px;
  color: var(--dp-text-secondary);
  cursor: pointer;
  padding: 4px 0;
  user-select: none;
}
.llm-input-block {
  margin-bottom: 8px;
  border: 1px dashed var(--dp-border);
  border-radius: var(--dp-radius-xs);
  padding: 4px 8px;
}
.llm-input-block summary {
  font-size: 12px;
  color: var(--dp-primary);
  cursor: pointer;
  padding: 4px 0;
  user-select: none;
}
.msg-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 6px;
  max-height: 400px;
  overflow-y: auto;
}
.msg-item {
  border-left: 3px solid var(--dp-border);
  padding: 4px 8px;
  font-size: 12px;
  line-height: 1.4;
}
.msg-item.systemmessage { border-left-color: #9c27b0; }
.msg-item.humanmessage { border-left-color: #2196f3; }
.msg-item.aimessage { border-left-color: #4caf50; }
.msg-item.toolmessage { border-left-color: #ff9800; }
.msg-role {
  font-weight: 600;
  font-size: 11px;
  color: var(--dp-text-secondary);
  text-transform: uppercase;
  margin-bottom: 2px;
  display: block;
}
.msg-content {
  color: var(--dp-text-regular);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 120px;
  overflow-y: auto;
}
.msg-toolcalls {
  font-size: 11px;
  color: var(--dp-primary);
  font-family: monospace;
}
.msg-tool-name {
  font-size: 11px;
  color: var(--dp-warning);
  font-family: monospace;
}
.prompt-text {
  font-size: 12px;
  line-height: 1.5;
  color: var(--dp-text-regular);
  background: var(--dp-card);
  border: 1px solid var(--dp-border);
  border-radius: var(--dp-radius-xs);
  padding: 8px 10px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 300px;
  overflow-y: auto;
  margin: 4px 0 0 0;
}
.tool-call-names {
  font-size: 11px;
  color: var(--dp-text-placeholder);
  font-family: monospace;
}
.dot-typing {
  color: var(--dp-text-placeholder);
  font-size: 20px;
  letter-spacing: 2px;
}
.run-result {
  border-top: 1px solid var(--dp-border);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
}
.run-result.linked { background: var(--dp-success-light, #e8f5e9); }
.run-result.escalated { background: var(--dp-warning-light, #fff8e1); }
.run-result.error { background: var(--dp-danger-light, #fce4ec); }
.result-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.result-dict {
  font-size: 13px;
  color: var(--dp-text-regular);
  font-family: monospace;
}
.result-conf {
  font-size: 12px;
  color: var(--dp-text-secondary);
  margin-left: auto;
}
.result-reason {
  font-size: 13px;
  color: var(--dp-text-regular);
  line-height: 1.5;
  white-space: pre-wrap;
  max-height: 120px;
  overflow-y: auto;
  padding: 6px 8px;
  background: var(--dp-card);
  border-radius: var(--dp-radius-xs);
  border: 1px solid var(--dp-border-light);
}
.result-proposal {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
