<template>
  <div class="graph-review" v-loading="loading">
    <div class="graph-toolbar">
      <div class="toolbar-left">
        <el-input v-model="filterName" placeholder="搜索实体名...回车聚焦" clearable size="small" style="width:160px" class="local-filter" @keyup.enter="onSearchEnter" @input="onSearchInput" @clear="onSearchClear" />
        <el-select v-model="filterEntityTypes" multiple collapse-tags placeholder="实体类型" clearable size="small" style="width:180px" @change="debouncedReloadGraph">
          <el-option v-for="t in entityTypeOptions" :key="t" :label="t" :value="t" />
        </el-select>
        <el-select v-model="filterSourceSite" placeholder="来源站点" clearable size="small" style="width:130px" @change="debouncedReloadGraph">
          <el-option v-for="s in siteOptions" :key="s" :label="s" :value="s" />
        </el-select>
        <el-select v-model="filterReviewStatus" multiple collapse-tags placeholder="审核状态" size="small" style="width:170px" @change="debouncedReloadGraph">
          <el-option label="待审核" value="pending" />
          <el-option label="已通过" value="approved" />
          <el-option label="自动通过" value="auto_approved" />
        </el-select>
        <el-input-number v-model="filterLimit" :min="10" :max="2000" :step="100" size="small" style="width:120px" @change="debouncedReloadGraph" />
      </div>
      <div class="toolbar-right">
        <span class="node-count">节点: {{ visibleNodeCount }} / {{ nodes.length }} | 边: {{ visibleEdgeCount }} / {{ edges.length }}</span>
        <template v-if="focusNodeId !== null">
          <el-tag size="small" type="warning" closable @close="focusNodeId = null">邻接: {{ focusNodeName }}</el-tag>
          <el-select v-model="neighborTypeFilter" size="small" placeholder="邻接类型" clearable style="width:130px" multiple collapse-tags>
            <el-option v-for="t in entityTypeOptions" :key="t" :label="t" :value="t" />
          </el-select>
          <el-button size="small" @click="focusNodeId = null">返回全局</el-button>
        </template>
        <el-button size="small" type="success" @click="batchApproveSelected" :disabled="selectedNodeIds.size === 0">&#x2713; 通过 ({{ selectedNodeIds.size }})</el-button>
        <el-button size="small" type="danger" @click="batchRejectSelected" :disabled="selectedNodeIds.size === 0">&#x2717; 拒绝</el-button>
        <el-button size="small" type="warning" plain @click="openMergeQueue">合并 ({{ mergeQueueGroups.length }})</el-button>
        <el-button size="small" type="primary" plain title="自动调整布局，使实体不堆叠" @click="rerunLayout">&#x2699;&#xFE0F; 自动调整</el-button>
      </div>
    </div>

    <div class="toolbar-local">
      <el-select v-model="filterIsolated" size="small" style="width:130px" class="local-filter">
        <el-option label="全部节点" value="all" />
        <el-option label="隐藏孤立点" value="hide" />
        <el-option label="只看孤立点" value="only" />
      </el-select>
      <el-select v-model="filterVerified" size="small" style="width:110px" class="local-filter">
        <el-option label="全部验证" value="all" />
        <el-option label="已验证" value="yes" />
        <el-option label="未验证" value="no" />
      </el-select>
      <div class="conf-slider">
        <span class="slider-label">置信度≥</span>
        <el-slider v-model="filterMinConf" :min="0" :max="1" :step="0.05" size="small" style="width:100px" class="local-filter" :show-tooltip="true" />
      </div>
      <el-select v-model="filterTraversal" size="small" style="width:100px" class="local-filter" @change="debouncedReloadGraph">
        <el-option label="BFS 展开" value="bfs" />
        <el-option label="度数排序" value="degree" />
      </el-select>
      <template v-if="filterTraversal === 'bfs'">
        <span class="slider-label">跳数</span>
        <el-input-number v-model="filterMaxDepth" :min="1" :max="10" size="small" style="width:80px" @change="debouncedReloadGraph" />
      </template>
      <el-select v-model="highlightTypeFilter" size="small" placeholder="高亮类型" clearable style="width:150px" class="local-filter" multiple collapse-tags>
        <el-option v-for="t in entityTypeOptions" :key="t" :label="t" :value="t" />
      </el-select>
    </div>

    <div class="graph-main">
      <div ref="graphContainerRef" class="graph-container" />
      <div class="graph-legend">
        <span class="legend-item"><span class="dot pending"></span>待审核</span>
        <span class="legend-item"><span class="dot approved"></span>已通过</span>
        <span class="legend-item"><span class="dot rejected"></span>已拒绝</span>
        <span class="legend-sep">|</span>
        <span class="legend-item">虚线=待审核 实线=已通过</span>
      </div>
      <div class="graph-tip">点击节点飞入聚焦 | 点击空白返回全局 | Shift+多选 | 滚轮缩放</div>
      <div class="graph-zoom-slider">
        <span class="zoom-label">🔍</span>
        <el-slider
          v-model="sliderZoom"
          :min="0"
          :max="100"
          :step="1"
          size="small"
          style="width:160px"
          @input="onZoomSliderChange"
        />
        <span class="zoom-value">{{ currentZoom.toFixed(2) }}x</span>
      </div>
    </div>

    <el-drawer v-model="showDetailPanel" title="实体详情" size="420px" direction="rtl" @close="detailNode = null">
      <template v-if="detailNode">
        <el-form label-width="80px" size="small">
          <el-form-item label="名称">
            <span class="field-value">{{ detailNode.entity_name }}</span>
          </el-form-item>
          <el-form-item label="类型">
            <el-tag size="small">{{ detailNode.entity_type }}</el-tag>
          </el-form-item>
          <el-form-item label="来源">
            <span class="field-value">{{ detailNode.source_site }}</span>
          </el-form-item>
          <el-form-item label="置信度">
            <el-progress :percentage="Math.round(detailNode.confidence_score * 100)" :stroke-width="8" style="width:120px" />
          </el-form-item>
          <el-form-item label="状态">
            <el-tag size="small" :type="statusTagType(detailNode.review_status)">{{ statusLabel(detailNode.review_status) }}</el-tag>
          </el-form-item>
          <el-form-item label="已验证">
            <el-tag size="small" :type="detailNode.is_verified ? 'success' : 'info'">{{ detailNode.is_verified ? '是' : '否' }}</el-tag>
          </el-form-item>
        </el-form>

        <el-divider content-position="left">元数据</el-divider>
        <el-form label-width="0" size="small">
          <el-form-item v-for="(val, key) in editableData" :key="key" :label="key" label-width="100px">
            <template v-if="typeof val === 'string' || typeof val === 'number'">
              <el-input v-model="editableData[key]" />
            </template>
            <template v-else-if="Array.isArray(val)">
              <el-input v-model="editableData[key]" :placeholder="'逗号分隔:' + val.join(',')" />
            </template>
            <template v-else-if="typeof val === 'boolean'">
              <el-switch v-model="editableData[key]" />
            </template>
            <template v-else>
              <el-input v-model="editableData[key]" type="textarea" :rows="2" />
            </template>
          </el-form-item>
          <el-form-item v-if="Object.keys(editableData).length === 0">
            <span class="empty-hint">暂无元数据</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" size="small" @click="saveEntityData" :loading="savingData">&#x1f4be; 保存元数据</el-button>
          </el-form-item>
        </el-form>

        <el-divider content-position="left">关联关系</el-divider>
        <div class="related-relations">
          <div v-for="rel in relatedEdges" :key="rel.id" class="rel-item" :class="{ 'rel-pending': rel.review_status === 'pending', 'rel-approved': rel.review_status === 'approved' }">
            <div class="rel-type">
              <span class="badge badge-relation-type" style="font-size:11px">{{ rel.relation_type }}</span>
            </div>
            <div class="rel-nodes">
              {{ getNodeName(rel.from_entity_id) }} &#x2192; {{ getNodeName(rel.to_entity_id) }}
            </div>
            <div class="rel-actions" v-if="rel.review_status === 'pending'">
              <el-button size="small" text type="success" @click="doApproveRelation(rel.id)">&#x2713;</el-button>
              <el-button size="small" text type="danger" @click="doRejectRelation(rel.id)">&#x2717;</el-button>
            </div>
          </div>
          <div v-if="relatedEdges.length === 0" class="empty-hint">无关联关系</div>
        </div>

        <template v-if="detailNode.review_status === 'pending'">
          <div class="detail-actions" style="margin-top:16px">
            <el-button type="success" @click="doApproveEntity(detailNode.id)">&#x2713; 审核通过</el-button>
            <el-button type="danger" @click="doRejectEntity(detailNode.id)">&#x2717; 审核拒绝</el-button>
          </div>
        </template>
        <div class="detail-actions" style="margin-top:8px">
          <el-button plain @click="showNeighbors(detailNode.id)">&#x1f50d; 查看邻接</el-button>
          <el-button v-if="detailNode.source_doc_uuid" @click="goToDocument(detailNode.source_doc_uuid)">&#x1f4c4; 查看文档</el-button>
        </div>
      </template>
    </el-drawer>

    <el-drawer v-model="showMergeDrawer" title="合并队列" size="500px" direction="rtl">
      <div class="merge-queue-panel" v-loading="mergeQueueLoading">
        <div v-for="group in mergeQueueGroups" :key="group.dict_id" class="merge-group">
          <div class="merge-group-header">
            <strong>{{ group.entity_name }}</strong>
            <el-tag size="small" type="info">{{ group.entity_type }}</el-tag>
            <span class="merge-group-source">dict_id={{ group.dict_id }}</span>
          </div>
          <div v-for="m in group.conflicts" :key="m.merge_id" class="merge-card">
            <div class="merge-card-header">
              <el-tag size="small" type="danger">{{ m.field_name }}</el-tag>
              <span class="merge-source">entity #{{ m.source_entity_id }} → dict #{{ group.dict_id }}</span>
            </div>
            <div class="merge-row">
              <div class="merge-col">
                <div class="merge-label">现有值</div>
                <pre class="merge-value">{{ formatMergeValue(m.existing_value) }}</pre>
              </div>
              <div class="merge-col">
                <div class="merge-label">新值</div>
                <pre class="merge-value">{{ formatMergeValue(m.new_value) }}</pre>
              </div>
            </div>
            <div class="merge-card-actions">
              <el-button size="small" type="success" @click="doResolveMerge(m.merge_id, 'accept_new')">采用新值</el-button>
              <el-button size="small" type="warning" @click="doResolveMerge(m.merge_id, 'keep_existing')">保留现有</el-button>
            </div>
          </div>
        </div>
        <el-empty v-if="!mergeQueueGroups.length && !mergeQueueLoading" description="暂无待处理的合并冲突" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import G6, { Graph as G6Graph } from '@antv/g6'
import {
  getReviewGraph,
  approveEntity, rejectEntity,
  approveRelation, rejectRelation,
  patchEntity,
  getMergeQueue, resolveMerge,
  type GraphNode, type GraphEdge,
  type MergeQueueGroup,
} from '@/api/review'

const router = useRouter()

let debounceTimer: ReturnType<typeof setTimeout> | null = null
function debouncedReloadGraph(delay = 300) {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => reloadGraph(), delay)
}

let localFilterTimer: ReturnType<typeof setTimeout> | null = null
function debouncedApplyFilters(delay = 120) {
  if (localFilterTimer) clearTimeout(localFilterTimer)
  localFilterTimer = setTimeout(() => applyFiltersToGraph(), delay)
}

let suppressApplyFilter = false
let globalPositionCache: Record<string, { x: number; y: number }> = {}
let focusNodePos: { x: number; y: number } | null = null

// ── 数据状态 ────────────────────────────────────────────────────────────

const loading = ref(false)
const savingData = ref(false)
const mergeQueueLoading = ref(false)
const nodes = ref<GraphNode[]>([])
const edges = ref<GraphEdge[]>([])
const entityTypeOptions = ref<string[]>([])
const siteOptions = ref<string[]>([])

// ── 筛选 ────────────────────────────────────────────────────────────────

const filterEntityTypes = ref<string[]>([])
const filterSourceSite = ref('')
const filterReviewStatus = ref<string[]>(['pending', 'approved'])
const filterDocUuid = ref('')
const filterLimit = ref(500)
const filterTraversal = ref('bfs')
const filterMaxDepth = ref(3)

// ── 本地筛选 ────────────────────────────────────────────────────────────
const filterName = ref('')

function matchSearchNodes(): number[] {
  const q = filterName.value.trim().toLowerCase()
  if (!q) return []
  return nodes.value
    .filter(n => n.entity_name.toLowerCase().includes(q))
    .map(n => n.id)
}

function onSearchInput() {
  if (!graph) return
  const matchIds = new Set(matchSearchNodes().map(String))
  const savedData = graph.save() as any
  for (const nd of (savedData.nodes || [])) {
    const item = graph.findById(nd.id)
    if (!item) continue
    const isMatch = matchIds.has(nd.id)
    if (isMatch) {
      item.setState('searchDim', false)
      item.setState('searchMatch', true)
    } else {
      item.setState('searchMatch', false)
      item.setState('searchDim', true)
    }
  }
}

function onSearchClear() {
  if (!graph) return
  const savedData = graph.save() as any
  for (const nd of (savedData.nodes || [])) {
    const item = graph.findById(nd.id)
    if (!item) continue
    item.setState('searchMatch', false)
    item.setState('searchDim', false)
  }
}

function onSearchEnter() {
  const matchIds = matchSearchNodes()
  if (matchIds.length === 0) {
    ElMessage.warning('未找到匹配实体')
    return
  }
  const match = nodes.value.find(n => n.id === matchIds[0])!
  // 清除搜索高亮
  onSearchClear()
  // 与点击节点效果一致：打开详情 + 聚焦放大
  detailNode.value = match
  showDetailPanel.value = true
  focusNodeId.value = match.id
  filterName.value = ''
}

function applyTypeHighlight() {
  if (!graph) return
  const typeSet = new Set(highlightTypeFilter.value)
  const savedData = graph.save() as any
  for (const nd of (savedData.nodes || [])) {
    const item = graph.findById(nd.id)
    if (!item) continue
    const ndData = nodes.value.find(n => String(n.id) === nd.id)
    const isMatch = ndData && typeSet.has(ndData.entity_type)
    if (typeSet.size === 0) {
      item.setState('typeHighlight', false)
      item.setState('typeDim', false)
    } else if (isMatch) {
      item.setState('typeDim', false)
      item.setState('typeHighlight', true)
    } else {
      item.setState('typeHighlight', false)
      item.setState('typeDim', true)
    }
  }
}

const focusNodeId = ref<number | null>(null)
const neighborTypeFilter = ref<string[]>([])
const highlightTypeFilter = ref<string[]>([])
const filterIsolated = ref('hide')
const filterVerified = ref('all')
const filterMinConf = ref(0)

const focusNodeName = computed(() => {
  if (focusNodeId.value === null) return ''
  return nodes.value.find(n => n.id === focusNodeId.value)?.entity_name || ''
})

const nodeDegrees = computed(() => {
  const deg: Record<number, number> = {}
  for (const n of nodes.value) deg[n.id] = 0
  for (const e of edges.value) {
    if (e.from_entity_id != null) deg[e.from_entity_id] = (deg[e.from_entity_id] || 0) + 1
    if (e.to_entity_id != null) deg[e.to_entity_id] = (deg[e.to_entity_id] || 0) + 1
  }
  return deg
})

const neighborSet = computed(() => {
  if (focusNodeId.value === null) return null
  const s = new Set<number>([focusNodeId.value])
  for (const e of edges.value) {
    if (e.from_entity_id === focusNodeId.value && e.to_entity_id != null) s.add(e.to_entity_id)
    if (e.to_entity_id === focusNodeId.value && e.from_entity_id != null) s.add(e.from_entity_id)
  }
  return s
})

const visibleNodeIds = computed(() => {
  let result = new Set<number>(nodes.value.map(n => n.id))

  if (neighborSet.value !== null) {
    result = new Set([...result].filter(id => neighborSet.value!.has(id)))
    if (neighborTypeFilter.value.length > 0) {
      const typeSet = new Set(neighborTypeFilter.value)
      result = new Set([...result].filter(id => {
        if (id === focusNodeId.value) return true
        const nd = nodes.value.find(n => n.id === id)
        return nd && typeSet.has(nd.entity_type)
      }))
    }
  }

  if (filterIsolated.value === 'hide') {
    result = new Set([...result].filter(id => (nodeDegrees.value[id] || 0) > 0))
  } else if (filterIsolated.value === 'only') {
    result = new Set([...result].filter(id => (nodeDegrees.value[id] || 0) === 0))
  }

  if (filterVerified.value === 'yes') {
    result = new Set([...result].filter(id => nodes.value.find(n => n.id === id)?.is_verified))
  } else if (filterVerified.value === 'no') {
    result = new Set([...result].filter(id => !nodes.value.find(n => n.id === id)?.is_verified))
  }

  if (filterMinConf.value > 0) {
    result = new Set([...result].filter(id => {
      const n = nodes.value.find(nd => nd.id === id)
      return (n?.confidence_score || 0) >= filterMinConf.value
    }))
  }

  return result
})

const visibleNodeCount = computed(() => visibleNodeIds.value.size)

const visibleEdges = computed(() => {
  return edges.value.filter(
    e => visibleNodeIds.value.has(e.from_entity_id!) && visibleNodeIds.value.has(e.to_entity_id!)
  )
})
const visibleEdgeCount = computed(() => visibleEdges.value.length)

// ── 合并队列 ─────────────────────────────────────────────────────────────

const showMergeDrawer = ref(false)
const mergeQueueGroups = ref<MergeQueueGroup[]>([])

async function openMergeQueue() {
  showMergeDrawer.value = true
  await loadMergeQueue()
}

async function loadMergeQueue() {
  mergeQueueLoading.value = true
  try {
    const res = await getMergeQueue(50)
    mergeQueueGroups.value = res.groups
  } catch { /* ignore */ } finally {
    mergeQueueLoading.value = false
  }
}

async function doResolveMerge(mergeId: number, resolution: string) {
  try {
    await resolveMerge(mergeId, '审核员', resolution)
    ElMessage.success(resolution === 'accept_new' ? '已采用新值' : '已保留现有值')
    // 从 groups 中移除已解决的 conflict
    for (const g of mergeQueueGroups.value) {
      g.conflicts = g.conflicts.filter(c => c.merge_id !== mergeId)
    }
    mergeQueueGroups.value = mergeQueueGroups.value.filter(g => g.conflicts.length > 0)
  } catch (e: any) {
    ElMessage.error('操作失败: ' + (e.message || ''))
  }
}

function formatMergeValue(val: any): string {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'string') return val
  return JSON.stringify(val, null, 2)
}

// ── 详情面板 ─────────────────────────────────────────────────────────────

const detailNode = ref<GraphNode | null>(null)
const showDetailPanel = ref(false)
const editableData = reactive<Record<string, any>>({})
const selectedNodeIds = reactive(new Set<number>())

const relatedEdges = computed(() =>
  detailNode.value
    ? edges.value.filter(e => e.from_entity_id === detailNode.value!.id || e.to_entity_id === detailNode.value!.id)
    : []
)

// ── G6 图谱引擎 ──────────────────────────────────────────────────────────

const graphContainerRef = ref<HTMLDivElement | null>(null)
let graph: G6Graph | null = null

const MIN_ZOOM = 0.05
const MAX_ZOOM = 1
const ZOOM_RATIO = MAX_ZOOM / MIN_ZOOM // 20

function sliderToZoom(s: number): number {
  return MIN_ZOOM * Math.pow(ZOOM_RATIO, s / 100)
}
function zoomToSlider(z: number): number {
  return 100 * Math.log(z / MIN_ZOOM) / Math.log(ZOOM_RATIO)
}

const sliderZoom = ref(zoomToSlider(0.25))
const currentZoom = ref(0.25)
let zoomFromSlider = false

function onZoomSliderChange(val: number) {
  if (!graph) return
  const z = sliderToZoom(val)
  currentZoom.value = z
  zoomFromSlider = true
  graph.zoomTo(z)
  setTimeout(() => { zoomFromSlider = false }, 100)
}

function statusColor(status: string): string {
  if (status === 'approved' || status === 'auto_approved') return '#00ff88'
  if (status === 'rejected') return '#ff3366'
  return '#ffaa00'
}

function transformToG6Data(nodes: GraphNode[], edges: GraphEdge[]) {
  const nodeIdSet = new Set(nodes.map(n => n.id))

  return {
    nodes: nodes.map(n => {
      const fill = statusColor(n.review_status)
      return {
        id: String(n.id),
        label: n.entity_name || '',
        type: 'circle',
        size: 55 + (n.confidence_score || 0) * 30,
        style: {
          fill,
          stroke: 'rgba(255,255,255,0.85)',
          lineWidth: 2.5,
          shadowColor: fill,
          shadowBlur: 22,
          shadowOffsetX: 0,
          shadowOffsetY: 0,
          cursor: 'pointer',
        },
        labelCfg: {
          position: 'top',
          offset: 8,
          style: {
            fill: '#e8e8f0',
            fontSize: 13,
            fontWeight: 600,
            shadowColor: 'rgba(0,0,0,0.9)',
            shadowBlur: 6,
          },
        },
        entity_type: n.entity_type,
        entity_name: n.entity_name,
        review_status: n.review_status,
        confidence_score: n.confidence_score,
        is_verified: n.is_verified,
      }
    }),
    edges: edges
      .filter(e => nodeIdSet.has(e.from_entity_id!) && nodeIdSet.has(e.to_entity_id!))
      .map(e => ({
        id: 'E' + e.id,
        source: String(e.from_entity_id),
        target: String(e.to_entity_id),
        style: {
          stroke: e.review_status === 'approved' ? '#00e676' :
                  e.review_status === 'rejected' ? '#ff3366' : '#ffaa00',
          lineWidth: e.review_status === 'rejected' ? 1.2 : 2.2,
          lineDash: e.review_status === 'approved' ? undefined :
                    (e.review_status === 'rejected' ? [6, 4] : [10, 6]),
          endArrow: {
            path: 'M 0 0 L 6 -4 L 6 4 Z',
            fill: e.review_status === 'approved' ? '#00e676' :
                  e.review_status === 'rejected' ? '#ff3366' : '#ffaa00',
          },
          opacity: e.review_status === 'rejected' ? 0.3 : 0.7,
        },
        review_status: e.review_status,
      })),
  }
}

const G6_NODE_BASE_SIZE = 55

const G6_NODE_SIZE = 70

const g6LayoutOptions = {
  type: 'force2' as const,
  animate: false,
  maxIteration: 300,
  nodeStrength: 1800,
  linkDistance: 360,
  edgeStrength: 200,
  gravity: 10,
  damping: 0.9,
  minMovement: 0.5,
  preventOverlap: true,
  nodeSize: G6_NODE_SIZE,
  nodeSpacing: 30,
}

function initGraph(container: HTMLDivElement): G6Graph {
  const graph = new G6Graph({
    container,
    width: container.clientWidth,
    height: container.clientHeight,
    animate: false,
    minZoom: 0.05,
    maxZoom: 100,
    defaultNode: {
      type: 'circle',
      size: 55,
      style: {
        fill: '#ffaa00',
        stroke: 'rgba(255,255,255,0.85)',
        lineWidth: 2.5,
        shadowColor: 'rgba(0, 212, 255, 0.35)',
        shadowBlur: 18,
        shadowOffsetX: 0,
        shadowOffsetY: 0,
      },
      labelCfg: {
        position: 'top',
        offset: 8,
        style: {
          fill: '#e8e8f0',
          fontSize: 13,
          fontWeight: 600,
          shadowColor: 'rgba(0,0,0,0.9)',
          shadowBlur: 6,
        },
      },
    },
    defaultEdge: {
      type: 'cubic',
      style: {
        stroke: '#4a7fc4',
        lineWidth: 2.0,
        lineDash: [8, 4],
        endArrow: {
          path: 'M 0 0 L 8 -5 L 8 5 Z',
          fill: '#4a7fc4',
        },
        opacity: 0.65,
      },
    },
    nodeStateStyles: {
      selected: {
        stroke: '#00e5ff',
        lineWidth: 5,
        shadowColor: 'rgba(0, 229, 255, 0.9)',
        shadowBlur: 50,
        shadowOffsetX: 0,
        shadowOffsetY: 0,
      },
      hover: {
        stroke: '#40d8ff',
        lineWidth: 5,
        shadowColor: 'rgba(64, 216, 255, 0.7)',
        shadowBlur: 36,
        shadowOffsetX: 0,
        shadowOffsetY: 0,
      },
      searchMatch: {
        shadowColor: 'rgba(255, 255, 255, 0.95)',
        shadowBlur: 40,
        lineWidth: 5,
        stroke: '#ffffff',
      },
      searchDim: {
        opacity: 0.1,
      },
      typeHighlight: {
        shadowColor: 'rgba(0, 229, 255, 0.8)',
        shadowBlur: 35,
        lineWidth: 4,
        stroke: '#00e5ff',
      },
      typeDim: {
        opacity: 0.18,
      },
    },
    edgeStateStyles: {
      selected: { stroke: '#00e5ff', lineWidth: 3.5, opacity: 1 },
      hover: { stroke: '#40d8ff', lineWidth: 3, opacity: 0.9 },
    },
    modes: {
      default: [
        { type: 'drag-canvas', enableOptimize: true },
        { type: 'zoom-canvas', enableOptimize: true },
        'drag-node',
        {
          type: 'click-select',
          multiple: true,
        },
        {
          type: 'brush-select',
          trigger: 'ctrl',
        },
      ],
    },
  })

  return graph
}

function applyFocusMode() {
  const focusIdStr = String(focusNodeId.value!)
  const visSet = visibleNodeIds.value
  const visIdStrs = new Set([...visSet].map(String))

  // 收集可见边的 id
  const visibleEdgeIds = new Set<string>()
  for (const e of edges.value) {
    if (visSet.has(e.from_entity_id!) && visSet.has(e.to_entity_id!)) {
      visibleEdgeIds.add('E' + e.id)
    }
  }

  // show/hide nodes
  const savedData = graph!.save() as any
  for (const nd of (savedData.nodes || [])) {
    const item = graph!.findById(nd.id)
    if (!item) continue
    if (visIdStrs.has(nd.id)) {
      if (item.get('visible') === false) item.show()
    } else {
      if (item.get('visible') !== false) item.hide()
    }
  }

  // show/hide edges
  for (const ed of (savedData.edges || [])) {
    const item = graph!.findById(ed.id)
    if (!item) continue
    if (visibleEdgeIds.has(ed.id)) {
      if (item.get('visible') === false) item.show()
    } else {
      if (item.get('visible') !== false) item.hide()
    }
  }
  // 视口聚焦
  const focusItem = graph!.findById(focusIdStr)
  if (focusItem) {
    const pos = focusItem.getModel()
    const savedForCheck = graph!.save() as any
    const fromSave = (savedForCheck.nodes || []).find((n: any) => n.id === focusIdStr)

    const gx = fromSave?.x ?? pos.x
    const gy = fromSave?.y ?? pos.y

    zoomFromSlider = true
    currentZoom.value = 0.7
    sliderZoom.value = Math.round(zoomToSlider(0.7))

    const group = graph!.get('group')
    const startM = group.getMatrix()
    const startZoom = startM[0] as number
    const startTx = startM[6] as number
    const startTy = startM[7] as number
    const w = graph!.getWidth()
    const h = graph!.getHeight()
    const targetZoom = 0.7
    const targetTx = w / 2 - targetZoom * gx!
    const targetTy = h / 2 - targetZoom * gy!

    group.animate(function(ratio: number) {
      // ease-out-back with stronger overshoot — "dive into globe" feel
      const c1 = 1.5
      const c3 = c1 + 1
      const t = ratio >= 1 ? 1 : 1 + c3 * Math.pow(ratio - 1, 3) + c1 * Math.pow(ratio - 1, 2)
      const z = startZoom + (targetZoom - startZoom) * t
      const tx = startTx + (targetTx - startTx) * t
      const ty = startTy + (targetTy - startTy) * t
      group.setMatrix([z, 0, 0, 0, z, 0, tx, ty, 1])
    }, 700)

    const nc = graph!.getCanvasByPoint(gx!, gy!)

    setTimeout(() => {
      zoomFromSlider = false
    }, 400)
  }
  nextTick(() => applyTypeHighlight())
}

async function applyFiltersToGraph() {
  if (!graph) return
  if (suppressApplyFilter) { suppressApplyFilter = false; return }
  const mode = focusNodeId.value !== null ? 'FOCUS' : 'GLOBAL'
  // ── FOCUS 模式：show/hide 控制可见性，不触发 changeData，避免自动布局 ──
  if (mode === 'FOCUS' && focusNodeId.value !== null) {
    return applyFocusMode()
  }

  // ── GLOBAL 模式：changeData 全量渲染 ──
  // 保存当前位置（从当前 graph 数据）
  const currentData = graph.save() as any
  const posMap: Record<string, { x: number; y: number }> = {}
  for (const nd of (currentData.nodes || [])) {
    if (nd.x != null) posMap[nd.id] = { x: nd.x, y: nd.y }
  }
  const visSet = visibleNodeIds.value
  const visibleNodes = nodes.value.filter(n => visSet.has(n.id))
  const visibleEdges = edges.value.filter(
    e => visSet.has(e.from_entity_id!) && visSet.has(e.to_entity_id!)
  )
  const data = transformToG6Data(visibleNodes, visibleEdges)

  // 恢复位置
  let skipLayout = false
  if (Object.keys(globalPositionCache).length > 0) {
    let restoredCount = 0
    for (const nd of data.nodes as any[]) {
      const saved = globalPositionCache[nd.id]
      if (saved) {
        nd.x = saved.x
        nd.y = saved.y
        restoredCount++
      }
    }
    if (restoredCount >= data.nodes.length * 0.85) {
      skipLayout = true
    }
  }
  if (!skipLayout) {
    for (const nd of data.nodes as any[]) {
      const saved = posMap[nd.id]
      if (saved) {
        nd.x = saved.x
        nd.y = saved.y
      }
    }
  }
  graph.changeData(data)
  nextTick(() => applyTypeHighlight())
  // GLOBAL 模式下 changeData 后确保所有 item 可见（FOCUS 模式可能隐藏过）
  const allItems = graph.save() as any
  for (const nd of (allItems.nodes || [])) {
    const item = graph.findById(nd.id)
    if (item && item.get('visible') === false) item.show()
  }
  for (const ed of (allItems.edges || [])) {
    const item = graph.findById(ed.id)
    if (item && item.get('visible') === false) item.show()
  }
  if (visibleNodes.length > 100 && !skipLayout) {
    graph.updateLayout(g6LayoutOptions)
    await graph.layout()
    const updatedData = graph.save() as any
    for (const nd of (updatedData.nodes || [])) {
      if (nd.x != null) globalPositionCache[nd.id] = { x: nd.x, y: nd.y }
    }
  }

  if (visibleNodes.length > 100) {
    graph.fitView()
    graph.zoomTo(0.25)
    currentZoom.value = 0.25
    sliderZoom.value = Math.round(zoomToSlider(0.25))
  }
}

function wireGraphEvents() {
  if (!graph) return

  // 节点单击 → 飞入放大 + 邻接筛选
  graph.on('node:click', (evt: any) => {
    const nodeItem = evt.item
    if (!nodeItem) return
    const model = nodeItem.getModel()
    const nodeId = model.id
    if (!nodeId) return

    if (evt.originalEvent?.shiftKey) {
      nextTick(() => syncSelectionFromGraph())
      return
    }

    const nodeData = nodes.value.find(n => String(n.id) === nodeId)
    if (nodeData) {
      detailNode.value = nodeData
      showDetailPanel.value = true
    }

    focusNodeId.value = Number(nodeId)
    if (model.x != null) {
      focusNodePos = { x: model.x, y: model.y }
    }
    if (localFilterTimer) clearTimeout(localFilterTimer)
    localFilterTimer = setTimeout(() => applyFiltersToGraph(), 30)
  })

  // 双击 → 钻入文档
  graph.on('node:dblclick', (evt: any) => {
    const nodeItem = evt.item
    if (!nodeItem) return
    const nodeId = Number(nodeItem.getModel().id)
    const nodeData = nodes.value.find(n => n.id === nodeId)
    if (nodeData?.source_doc_uuid) {
      filterDocUuid.value = nodeData.source_doc_uuid
      reloadGraph()
    }
  })

  // 双击空白 → 返回全局
  graph.on('canvas:dblclick', () => {
    focusNodeId.value = null
    focusNodePos = null
    neighborTypeFilter.value = []

    if (localFilterTimer) clearTimeout(localFilterTimer)
    localFilterTimer = setTimeout(() => applyFiltersToGraph(), 30)
  })

  graph.on('nodestatechange', () => {
    syncSelectionFromGraph()
  })

  graph.on('viewportchange', () => {
    if (!zoomFromSlider) {
      const z = graph!.getZoom()
      currentZoom.value = Math.round(z * 100) / 100
      sliderZoom.value = Math.round(zoomToSlider(z))
    }
  })

}

async function rerunLayout() {
  if (!graph) return

  const visSet = visibleNodeIds.value
  const data = graph.save() as any
  const w = graph.getWidth()
  const h = graph.getHeight()
  const visibleIds = [...visSet].map(String)
  const centerX = w / 2
  const centerY = h / 2
  const radius = Math.min(w, h) * 0.35

  // 圆形散开可见节点
  let idx = 0
  for (const nd of (data.nodes || [])) {
    if (nd.id && visSet.has(Number(nd.id))) {
      const angle = (2 * Math.PI * idx) / visibleIds.length
      nd.x = centerX + radius * Math.cos(angle)
      nd.y = centerY + radius * Math.sin(angle)
      idx++
    }
  }

  graph.changeData(data)

  graph.updateLayout({
    type: 'force2',
    animate: false,
    maxIteration: 200,
    nodeStrength: 1200,
    linkDistance: 280,
    edgeStrength: 200,
    preventOverlap: true,
    nodeSize: G6_NODE_SIZE,
    nodeSpacing: 30,
  })
  graph.layout()
}

function syncSelectionFromGraph() {
  if (!graph) return
  selectedNodeIds.clear()
  const selected = graph.findAllByState('node', 'selected')
  for (const item of selected) {
    const model = item.getModel()
    selectedNodeIds.add(Number(model.id))
  }
}

// ── 工具函数 ────────────────────────────────────────────────────────────

function getNodeName(id: number | null): string {
  if (!id) return '?'
  const n = nodes.value.find(n => n.id === id)
  return n?.entity_name || `#${id}`
}

// ── 数据加载 ────────────────────────────────────────────────────────────

async function reloadGraph() {
  if (!graph) {
    console.warn('Graph instance not ready')
    return
  }
  
  loading.value = true
  try {
    const params: any = { /* 你的参数 */ }
    const graphRes = await getReviewGraph(params)
    
    // 更新本地数据
    nodes.value = graphRes.nodes
    edges.value = graphRes.edges
    
    //  使用 changeData
    const data = transformToG6Data(nodes.value, edges.value)
    graph.changeData(data)
    
    // 重新布局并适配视图
    graph.updateLayout(g6LayoutOptions)
    graph.layout()
    graph.fitView()
    graph.zoomTo(0.25)
  } catch (err) {
    console.error('重载数据失败:', err)
    ElMessage.error('数据重载失败')
  } finally {
    loading.value = false
  }
}

// ── 操作 ────────────────────────────────────────────────────────────────

async function doApproveEntity(id: number) {
  await approveEntity(id, '审核员')
  updateNodeStatus(id, 'approved')
  ElMessage.success('已通过')
}

async function doRejectEntity(id: number) {
  await rejectEntity(id, '审核员')
  updateNodeStatus(id, 'rejected')
  ElMessage.warning('已拒绝')
}

function updateNodeStatus(id: number, status: string) {
  const node = nodes.value.find(n => n.id === id)
  if (node) node.review_status = status
  if (detailNode.value?.id === id) detailNode.value.review_status = status

  if (graph) {
    const color = statusColor(status)
    const item = graph.findById(String(id))
    if (item) {
      graph.updateItem(item, {
        style: { fill: color },
        review_status: status,
      })
    }
  }
}

async function doApproveRelation(id: number) {
  await approveRelation(id, '审核员')
  const edge = edges.value.find(e => e.id === id)
  if (edge) edge.review_status = 'approved'

  if (graph) {
    const item = graph.findById('E' + id)
    if (item) {
      graph.updateItem(item, {
        style: { stroke: '#00ff88', lineDash: undefined, opacity: 0.7 },
        review_status: 'approved',
      })
    }
  }
  ElMessage.success('关系已通过')
}

async function doRejectRelation(id: number) {
  await rejectRelation(id, '审核员')
  const edge = edges.value.find(e => e.id === id)
  if (edge) edge.review_status = 'rejected'

  if (graph) {
    const item = graph.findById('E' + id)
    if (item) {
      graph.updateItem(item, {
        style: { stroke: '#ff3366', lineDash: [6, 4], opacity: 0.35 },
        review_status: 'rejected',
      })
    }
  }
  ElMessage.warning('关系已拒绝')
}

async function saveEntityData() {
  if (!detailNode.value) return
  savingData.value = true
  try {
    const rawData: Record<string, any> = {}
    for (const [k, v] of Object.entries(editableData)) {
      if (typeof v === 'string' && v.includes(',')) {
        const arr = v.split(',').map(s => s.trim()).filter(Boolean)
        rawData[k] = arr.length > 1 ? arr : v
      } else {
        rawData[k] = v
      }
    }
    await patchEntity(detailNode.value.id, rawData)
    detailNode.value.entity_data = JSON.parse(JSON.stringify(rawData))
    const node = nodes.value.find(n => n.id === detailNode.value!.id)
    if (node) node.entity_data = JSON.parse(JSON.stringify(rawData))
    ElMessage.success('元数据已保存')
  } catch (e: any) {
    ElMessage.error('保存失败: ' + (e.message || ''))
  } finally {
    savingData.value = false
  }
}

async function batchApproveSelected() {
  if (selectedNodeIds.size === 0) return
  try {
    const ids = [...selectedNodeIds]
    await Promise.all(ids.map(id => approveEntity(id, '审核员')))
    for (const id of ids) updateNodeStatus(id, 'approved')
    ElMessage.success(`已批量通过 ${ids.length} 个实体`)
    selectedNodeIds.clear()
  } catch (e: any) {
    ElMessage.error('批量通过失败')
  }
}

async function batchRejectSelected() {
  if (selectedNodeIds.size === 0) return
  try {
    const ids = [...selectedNodeIds]
    await Promise.all(ids.map(id => rejectEntity(id, '审核员')))
    for (const id of ids) updateNodeStatus(id, 'rejected')
    ElMessage.warning(`已批量拒绝 ${ids.length} 个实体`)
    selectedNodeIds.clear()
  } catch (e: any) {
    ElMessage.error('批量拒绝失败')
  }
}

function showNeighbors(id: number) {
  focusNodeId.value = id
}

function goToDocument(uuid: string) {
  router.push(`/documents/${uuid}`)
}

// ── 工具函数 ────────────────────────────────────────────────────────────

function statusTagType(s: string): string {
  if (s === 'approved' || s === 'auto_approved') return 'success'
  if (s === 'rejected') return 'danger'
  return 'warning'
}

function statusLabel(s: string): string {
  const map: Record<string, string> = {
    pending: '待审核',
    approved: '已通过',
    auto_approved: '自动通过',
    rejected: '已拒绝',
  }
  return map[s] || s
}

function onResize() {
  if (graph && graphContainerRef.value) {
    const { width, height } = graphContainerRef.value.getBoundingClientRect()
    if (width > 0 && height > 0) {
      graph.changeSize(width, height)
    }
  }
}

onMounted(async () => {
  await loadMergeQueue()
  await nextTick()
  
  // 1. 先确保容器存在且 graph 未初始化
  if (graphContainerRef.value && !graph) {
    try {
      // 2. 创建 graph 实例
      graph = initGraph(graphContainerRef.value)
      wireGraphEvents()
      
      // 3. 获取数据
      loading.value = true
      const params: any = {
        review_status: filterReviewStatus.value.join(','),
        limit: filterLimit.value,
        include_relations: true,
        traversal: filterTraversal.value,
        max_depth: filterMaxDepth.value,
      }
      if (filterEntityTypes.value.length) params.entity_types = filterEntityTypes.value.join(',')
      if (filterSourceSite.value) params.source_site = filterSourceSite.value
      if (filterDocUuid.value) params.source_doc_uuid = filterDocUuid.value

      const graphRes = await getReviewGraph(params)

      // 4. 更新数据（阻止 watch 触发 applyFiltersToGraph）
      suppressApplyFilter = true
      nodes.value = graphRes.nodes
      edges.value = graphRes.edges
      entityTypeOptions.value = graphRes.entity_types || []
      siteOptions.value = graphRes.source_sites || []

      // 5. 初始渲染
      const data = transformToG6Data(nodes.value, edges.value)
      graph.read(data)

      // 6. 布局 + 视图
      graph.updateLayout(g6LayoutOptions)
      await graph.layout()

      graph.fitView()
      graph.zoomTo(0.25)
      currentZoom.value = 0.25
      sliderZoom.value = Math.round(zoomToSlider(0.25))

      // 7. 填充位置缓存
      const posData = graph.save() as any
      for (const nd of (posData.nodes || [])) {
        if (nd.x != null) globalPositionCache[nd.id] = { x: nd.x, y: nd.y }
      }
      
    } catch (err) {
      console.error('初始化图谱失败:', err)
      ElMessage.error('图谱初始化失败')
    } finally {
      loading.value = false
    }
  }
  
  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  if (graph) {
    graph.destroy()
    graph = null
  }
})

watch(focusNodeId, (val) => {
  if (val === null) neighborTypeFilter.value = []
})

watch(highlightTypeFilter, () => {
  applyTypeHighlight()
})

watch([visibleNodeIds, visibleEdges], () => {
  debouncedApplyFilters()
})

watch(detailNode, (node) => {
  if (node) {
    Object.keys(editableData).forEach(k => delete editableData[k])
    Object.assign(editableData, JSON.parse(JSON.stringify(node.entity_data || {})))
  }
})
</script>

<style scoped>
.graph-review { height: calc(100vh - 40px); display: flex; flex-direction: column; padding: 0 16px; }

.graph-toolbar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--dp-border);
  border-radius: var(--dp-radius-md);
  margin-bottom: 4px;
  background: var(--dp-card);
  flex-shrink: 0;
}
.toolbar-left { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.toolbar-right { display: flex; align-items: center; gap: 6px; margin-left: auto; }
.toolbar-local {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 12px 6px;
  flex-shrink: 0;
}
.conf-slider { display: flex; align-items: center; gap: 4px; }
.slider-label { font-size: 12px; color: var(--dp-text-secondary); white-space: nowrap; }
.local-filter :deep(.el-input__wrapper) { background: var(--dp-success-light); }
.node-count { font-size: 12px; color: var(--dp-text-secondary); white-space: nowrap; }

.graph-main {
  flex: 1;
  position: relative;
  border: 1px solid var(--dp-border);
  border-radius: var(--dp-radius-lg);
  overflow: hidden;
  background: var(--dp-muted-bg-secondary);
}

.graph-container {
  position: absolute;
  inset: 0;
  background-color: #0a0f1e;
  background-image:
    linear-gradient(rgba(0, 212, 255, 0.08) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0, 212, 255, 0.08) 1px, transparent 1px);
  background-size: 48px 48px;
}

.graph-legend {
  position: absolute;
  left: 12px;
  bottom: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 11px;
  color: var(--dp-text-regular);
  background: var(--dp-card);
  padding: 6px 10px;
  border-radius: var(--dp-radius-md);
  border: 1px solid var(--dp-border);
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  z-index: 2;
  pointer-events: none;
}
.graph-legend .legend-item { display: flex; align-items: center; gap: 4px; }
.legend-sep { color: var(--dp-border); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.pending { background: var(--dp-graph-pending); }
.dot.approved { background: var(--dp-graph-approved); }
.dot.rejected { background: var(--dp-graph-rejected); }
.graph-tip {
  position: absolute;
  right: 12px;
  bottom: 12px;
  font-size: 11px;
  color: var(--dp-text-placeholder);
  background: var(--dp-card);
  padding: 6px 10px;
  border-radius: var(--dp-radius-md);
  border: 1px solid var(--dp-border);
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  z-index: 2;
  pointer-events: none;
}
.graph-zoom-slider {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(10, 14, 30, 0.85);
  padding: 4px 14px;
  border-radius: 20px;
  border: 1px solid rgba(0, 212, 255, 0.25);
  z-index: 2;
}
.zoom-label { font-size: 14px; }
.zoom-value {
  font-size: 11px;
  font-weight: 600;
  color: #00d4ff;
  min-width: 44px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.field-value { font-weight: 500; }
.empty-hint { color: var(--dp-text-placeholder); font-size: 13px; }

.related-relations { max-height: 200px; overflow-y: auto; }
.rel-item { padding: 6px 8px; border-radius: var(--dp-radius-xs); margin-bottom: 4px; display: flex; align-items: center; gap: 6px; font-size: 13px; }
.rel-pending { border-left: 3px solid var(--dp-warning-dark); background: var(--dp-warning-bg); }
.rel-approved { border-left: 3px solid var(--dp-success-dark); background: var(--dp-success-bg); }
.rel-type { flex-shrink: 0; }
.rel-nodes { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rel-actions { flex-shrink: 0; }

.detail-actions { display: flex; gap: 8px; }

.merge-queue-panel { padding: 0 4px; }
.merge-card {
  border: 1px solid var(--dp-border-light); border-radius: var(--dp-radius-md); padding: 12px; margin-bottom: 10px;
  background: var(--dp-card);
}
.merge-card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.merge-source { font-size: 12px; color: var(--dp-text-secondary); }
.merge-row { display: flex; gap: 10px; }
.merge-col { flex: 1; min-width: 0; }
.merge-label { font-size: 11px; color: var(--dp-text-secondary); margin-bottom: 4px; }
.merge-value {
  font-size: 12px; background: var(--dp-muted-bg); border-radius: var(--dp-radius-xs); padding: 6px 8px;
  margin: 0; max-height: 120px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
}
.merge-card-actions { display: flex; gap: 8px; margin-top: 10px; }

.badge {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  font-size: 11px;
  font-weight: 500;
  border-radius: var(--dp-radius-xs);
  border: 1px solid transparent;
}
.badge-relation-type { border-color: rgba(240,138,63,0.3); background: rgba(240,138,63,0.05); color: var(--dp-warning); }

@media (max-width: 1200px) {
  .graph-toolbar { flex-direction: column; align-items: flex-start; }
  .toolbar-right { margin-left: 0; width: 100%; }
}
</style>
