<template>
  <div class="graph-review" v-loading="loading">
    <div class="graph-toolbar">
      <div class="toolbar-left">
        <el-input v-model="filterName" placeholder="搜索实体名..." clearable size="small" style="width:160px" class="local-filter" />
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
      </div>
      <div class="toolbar-right">
        <span class="node-count">节点: {{ visibleNodeCount }} / {{ nodes.length }} | 边: {{ visibleEdgeCount }} / {{ edges.length }}</span>
        <template v-if="focusNodeId !== null">
          <el-tag size="small" type="warning" closable @close="focusNodeId = null">邻接: {{ focusNodeName }}</el-tag>
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
    </div>

    <div class="graph-main">
      <div ref="cyContainerRef" class="cy-container" />
      <div class="graph-legend">
        <span class="legend-item"><span class="dot pending"></span>待审核</span>
        <span class="legend-item"><span class="dot approved"></span>已通过</span>
        <span class="legend-item"><span class="dot rejected"></span>已拒绝</span>
        <span class="legend-sep">|</span>
        <span class="legend-item">虚线=待审核 实线=已通过</span>
      </div>
      <div class="graph-tip">点击节点查看详情 | Shift+多选 | Ctrl+框选 | 滚轮缩放 | 中键平移</div>
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
import cytoscape, { type Core, type EventObject } from 'cytoscape'
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

// ── 本地筛选 ────────────────────────────────────────────────────────────
const filterName = ref('')
const focusNodeId = ref<number | null>(null)
const filterIsolated = ref('all')
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
  }

  if (filterName.value.trim()) {
    const q = filterName.value.trim().toLowerCase()
    result = new Set(
      [...result].filter(id => {
        const n = nodes.value.find(nd => nd.id === id)
        return n?.entity_name.toLowerCase().includes(q)
      })
    )
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

// ── Cytoscape ────────────────────────────────────────────────────────────

const cyContainerRef = ref<HTMLDivElement | null>(null)
let cy: Core | null = null

function nodeCssClass(status: string): string {
  if (status === 'approved' || status === 'auto_approved') return 'approved'
  if (status === 'rejected') return 'rejected'
  return 'pending'
}

function transformToCyData(nodes: GraphNode[], edges: GraphEdge[]) {
  const nodeIdSet = new Set(nodes.map(n => n.id))

  return {
    nodes: nodes.map(n => ({
      data: {
        id: String(n.id),
        label: (n.is_verified ? '✓ ' : '') + (n.entity_name || '').slice(0, 14),
        entity_type: n.entity_type,
        entity_name: n.entity_name,
        review_status: n.review_status,
        confidence_score: n.confidence_score,
        is_verified: n.is_verified,
        source_doc_uuid: n.source_doc_uuid,
        source_site: n.source_site,
        dict_id: n.dict_id,
        entity_data: n.entity_data,
      },
      classes: nodeCssClass(n.review_status),
    })),
    edges: edges
      .filter(e => nodeIdSet.has(e.from_entity_id!) && nodeIdSet.has(e.to_entity_id!))
      .map(e => ({
        data: {
          id: String(e.id),
          source: String(e.from_entity_id),
          target: String(e.to_entity_id),
          relation_type: e.relation_type,
          review_status: e.review_status,
          relation_props: e.relation_props,
          source_doc_uuid: e.source_doc_uuid,
        },
        classes: nodeCssClass(e.review_status),
      })),
  }
}

const cyStyles = [
  { selector: 'node', style: {
    'shape': 'ellipse',
    'label': 'data(label)',
    'text-valign': 'bottom',
    'text-halign': 'center',
    'font-size': '11px',
    'font-family': 'system-ui, -apple-system, sans-serif',
    'color': '#303133',
    'text-margin-y': 5,
    'text-max-width': '110px',
    'text-wrap': 'ellipsis',
    'border-width': 2.5,
    'border-color': '#ffffff',
    'width': 'mapData(confidence_score, 0, 1, 28, 48)',
    'height': 'mapData(confidence_score, 0, 1, 28, 48)',
    'background-opacity': 0.9,
    'transition-property': 'background-color, border-color, border-width, border-opacity',
    'transition-duration': 180,
  }},
  { selector: 'node.pending', style: {
    'background-color': '#f0a841',
    'background-blacken': -0.08,
  }},
  { selector: 'node.approved', style: {
    'background-color': '#14b8a6',
    'background-blacken': -0.08,
  }},
  { selector: 'node.rejected', style: {
    'background-color': '#e85d75',
    'background-blacken': -0.08,
  }},
  { selector: 'node:selected', style: {
    'border-color': '#3b82f6',
    'border-width': 4,
    'border-opacity': 1,
    'shadow-color': '#3b82f6',
    'shadow-blur': 16,
    'shadow-opacity': 0.45,
    'shadow-offset-x': 0,
    'shadow-offset-y': 0,
  }},
  { selector: 'node:active', style: {
    'border-color': '#60a5fa',
    'border-width': 3,
    'border-opacity': 0.9,
    'overlay-opacity': 0.12,
    'overlay-color': '#3b82f6',
    'overlay-padding': 8,
  }},
  { selector: 'edge', style: {
    'width': 1.6,
    'curve-style': 'bezier',
    'target-arrow-shape': 'triangle',
    'arrow-scale': 0.8,
    'line-opacity': 0.7,
    'transition-property': 'line-color, line-style, width, line-opacity',
    'transition-duration': 180,
  }},
  { selector: 'edge.pending', style: {
    'line-color': '#f0a841',
    'line-style': 'dashed',
    'line-dash-pattern': [6, 4],
    'target-arrow-color': '#f0a841',
  }},
  { selector: 'edge.approved', style: {
    'line-color': '#14b8a6',
    'line-style': 'solid',
    'target-arrow-color': '#14b8a6',
  }},
  { selector: 'edge.rejected', style: {
    'line-color': '#e85d75',
    'line-style': 'dashed',
    'line-dash-pattern': [4, 4],
    'target-arrow-color': '#e85d75',
    'line-opacity': 0.4,
  }},
  { selector: 'edge:selected', style: {
    'width': 3,
    'line-opacity': 1,
  }},
]

const layoutOptions = {
  name: 'cose',
  nodeRepulsion: 800000,
  edgeElasticity: 0.45,
  idealEdgeLength: 220,
  gravity: 0.1,
  numIter: 2000,
  initialTemp: 300,
  coolingFactor: 0.97,
  refresh: 20,
  animate: 'during' as const,
  animationDuration: 1000,
  fit: true,
  padding: 80,
  randomize: false,
  nodeDimensionsIncludeLabels: true,
}

function initCy(container: HTMLDivElement): Core {
  return cytoscape({
    container,
    style: cyStyles as any,
    boxSelectionEnabled: false,
    autounselectify: false,
    selectionType: 'additive',
    layout: layoutOptions as any,
    minZoom: 0.2,
    maxZoom: 3,
    wheelSensitivity: 0.6,
    styleEnabled: true,
  })
}

function applyFiltersToGraph() {
  if (!cy) return
  const visSet = visibleNodeIds.value
  const visEdgeIds = new Set(visibleEdges.value.map(e => e.id))

  cy.batch(() => {
    cy!.nodes().forEach(n => {
      n.style('display', visSet.has(Number(n.id())) ? 'element' : 'none')
    })
    cy!.edges().forEach(e => {
      e.style('display', visEdgeIds.has(Number(e.id())) ? 'element' : 'none')
    })
  })
}

function wireCyEvents() {
  if (!cy) return

  cy.on('tap', 'node', (evt: EventObject) => {
    const node = evt.target
    const nodeId = Number(node.id())
    const nodeData = nodes.value.find(n => n.id === nodeId)
    if (!nodeData) return

    if ((evt.originalEvent as MouseEvent)?.shiftKey) {
      // Shift+click: cytoscape handles multi-select natively with selectionType:'additive'
      // Sync our selectedNodeIds with cy selection
      syncSelectionFromCy()
    } else {
      detailNode.value = nodeData
      showDetailPanel.value = true
    }
  })

  cy.on('tap', (evt: EventObject) => {
    if (evt.target === cy) {
      selectedNodeIds.clear()
    }
  })

  cy.on('boxend', () => {
    syncSelectionFromCy()
  })

  cy.on('boxend', () => {
    syncSelectionFromCy()
  })

  cy.on('select unselect', () => {
    syncSelectionFromCy()
  })

  cy.on('dbltap', 'node', (evt: EventObject) => {
    const nodeId = Number(evt.target.id())
    const nodeData = nodes.value.find(n => n.id === nodeId)
    if (nodeData?.source_doc_uuid) {
      filterDocUuid.value = nodeData.source_doc_uuid
      reloadGraph()
    }
  })

  cy.on('zoom', () => {
    updateLabelVisibility()
  })
}

function updateLabelVisibility() {
  if (!cy) return
  const zoom = cy.zoom()
  const showLabel = zoom > 0.8
  const fontSize = zoom > 1.5 ? '11px' : zoom > 0.8 ? '9px' : '0px'
  cy.nodes().style({
    'font-size': fontSize,
    'text-opacity': showLabel ? 1 : 0,
  })
}

function rerunLayout() {
  if (!cy) return

  // 计算可见节点数量
  const visibleCount = cy.nodes().filter(n => n.style('display') !== 'none').length
  const isLarge = visibleCount > 50

  // 先随机化位置，确保节点不重叠
  const nodes = cy.nodes()
  const box = cy.container()?.getBoundingClientRect()
  const w = box ? box.width : 800
  const h = box ? box.height : 600

  nodes.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / nodes.length
    const radius = Math.min(w, h) * 0.35
    n.position({
      x: w / 2 + radius * Math.cos(angle),
      y: h / 2 + radius * Math.sin(angle),
    })
  })

  // 执行力导向布局，用固定的大数值确保节点充分展开
  cy.layout({
    name: 'cose',
    nodeRepulsion: 2000000,
    edgeElasticity: 0.5,
    idealEdgeLength: 300,
    gravity: 0.05,
    numIter: 3000,
    initialTemp: 500,
    coolingFactor: 0.98,
    refresh: 30,
    animate: 'during' as const,
    animationDuration: 2000,
    fit: true,
    padding: 120,
    randomize: false,
    nodeDimensionsIncludeLabels: true,
  } as any).run()

  // 布局完成后更新标签可见性
  setTimeout(() => {
    updateLabelVisibility()
  }, 2200)
}

function syncSelectionFromCy() {
  if (!cy) return
  selectedNodeIds.clear()
  cy.nodes(':selected').forEach(n => {
    selectedNodeIds.add(Number(n.id()))
  })
}

// ── 工具函数 ────────────────────────────────────────────────────────────

function getNodeName(id: number | null): string {
  if (!id) return '?'
  const n = nodes.value.find(n => n.id === id)
  return n?.entity_name || `#${id}`
}

// ── 数据加载 ────────────────────────────────────────────────────────────

async function reloadGraph() {
  loading.value = true
  try {
    const params: any = {
      review_status: filterReviewStatus.value.join(','),
      limit: 500,
      include_relations: true,
    }
    if (filterEntityTypes.value.length) params.entity_types = filterEntityTypes.value.join(',')
    if (filterSourceSite.value) params.source_site = filterSourceSite.value
    if (filterDocUuid.value) params.source_doc_uuid = filterDocUuid.value

    const graphRes = await getReviewGraph(params)
    nodes.value = graphRes.nodes
    edges.value = graphRes.edges

    entityTypeOptions.value = graphRes.entity_types || []
    siteOptions.value = graphRes.source_sites || []

    detailNode.value = null
    Object.keys(editableData).forEach(k => delete editableData[k])
    selectedNodeIds.clear()
    focusNodeId.value = null

    await nextTick()

    if (cy) {
      const data = transformToCyData(nodes.value, edges.value)
      cy.elements().remove()
      cy.add(data.nodes)
      cy.add(data.edges)
      cy.layout(layoutOptions as any).run()
      updateLabelVisibility()
    }
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

  if (cy) {
    const ele = cy.getElementById(String(id))
    if (ele.length) {
      ele.removeClass('pending approved rejected').addClass(nodeCssClass(status))
      ele.data('review_status', status)
    }
  }
}

async function doApproveRelation(id: number) {
  await approveRelation(id, '审核员')
  const edge = edges.value.find(e => e.id === id)
  if (edge) edge.review_status = 'approved'

  if (cy) {
    const ele = cy.getElementById(String(id))
    if (ele.length) {
      ele.removeClass('pending approved rejected').addClass('approved')
      ele.data('review_status', 'approved')
    }
  }
  ElMessage.success('关系已通过')
}

async function doRejectRelation(id: number) {
  await rejectRelation(id, '审核员')
  const edge = edges.value.find(e => e.id === id)
  if (edge) edge.review_status = 'rejected'

  if (cy) {
    const ele = cy.getElementById(String(id))
    if (ele.length) {
      ele.removeClass('pending approved rejected').addClass('rejected')
      ele.data('review_status', 'rejected')
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
  if (cy && cyContainerRef.value) {
    const { width, height } = cyContainerRef.value.getBoundingClientRect()
    if (width > 0 && height > 0) {
      cy.resize().fit(undefined, 30)
    }
  }
}

function onKeyDown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && !e.repeat) {
    cy?.boxSelectionEnabled(true)
  }
}

function onKeyUp(e: KeyboardEvent) {
  if (!e.ctrlKey && !e.metaKey) {
    cy?.boxSelectionEnabled(false)
  }
}

onMounted(async () => {
  await Promise.all([reloadGraph(), loadMergeQueue()])

  await nextTick()
  if (cyContainerRef.value && !cy) {
    cy = initCy(cyContainerRef.value)
    wireCyEvents()
    const data = transformToCyData(nodes.value, edges.value)
    cy.add(data.nodes)
    cy.add(data.edges)
    cy.layout(layoutOptions as any).run()
  }

  window.addEventListener('resize', onResize)

  document.addEventListener('keydown', onKeyDown)
  document.addEventListener('keyup', onKeyUp)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  document.removeEventListener('keydown', onKeyDown)
  document.removeEventListener('keyup', onKeyUp)
  cy?.destroy()
  cy = null
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

.cy-container {
  position: absolute;
  inset: 0;
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
