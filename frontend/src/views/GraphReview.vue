<template>
  <div class="graph-review" v-loading="loading">
    <!-- 筛选栏 -->
    <el-card shadow="never" class="filter-bar">
      <div class="filter-row">
        <div class="filter-left">
          <el-input v-model="filterName" placeholder="搜索实体名..." clearable style="width:180px" class="local-filter" />
          <el-select v-model="filterEntityTypes" multiple collapse-tags placeholder="实体类型" clearable style="width:200px" @change="reloadGraph">
            <el-option v-for="t in entityTypeOptions" :key="t" :label="t" :value="t" />
          </el-select>
          <el-select v-model="filterSourceSite" placeholder="来源站点" clearable style="width:140px" @change="reloadGraph">
            <el-option v-for="s in siteOptions" :key="s" :label="s" :value="s" />
          </el-select>
          <el-select v-model="filterReviewStatus" multiple collapse-tags placeholder="审核状态" style="width:180px" @change="reloadGraph">
            <el-option label="待审核" value="pending" />
            <el-option label="已通过" value="approved" />
            <el-option label="自动通过" value="auto_approved" />
          </el-select>
          <el-input v-model="filterDocUuid" placeholder="文档UUID（可选）" clearable style="width:240px" @change="reloadGraph" />
        </div>
        <div class="filter-middle">
          <el-select v-model="filterIsolated" style="width:130px" class="local-filter">
            <el-option label="全部节点" value="all" />
            <el-option label="隐藏孤立点" value="hide" />
            <el-option label="只看孤立点" value="only" />
          </el-select>
          <el-select v-model="filterVerified" style="width:120px" class="local-filter">
            <el-option label="全部验证" value="all" />
            <el-option label="已验证" value="yes" />
            <el-option label="未验证" value="no" />
          </el-select>
          <div class="conf-slider">
            <span class="slider-label">置信度≥</span>
            <el-slider v-model="filterMinConf" :min="0" :max="1" :step="0.05" style="width:100px" class="local-filter" :show-tooltip="true" />
          </div>
        </div>
        <div class="filter-right">
          <template v-if="focusNodeId !== null">
            <el-tag type="warning" closable @close="focusNodeId = null">邻接模式: {{ focusNodeName }}</el-tag>
            <el-button size="small" type="primary" @click="focusNodeId = null">返回全局</el-button>
          </template>
          <span class="node-count">节点: {{ visibleNodeCount }} / {{ nodes.length }} | 边: {{ visibleEdgeCount }} / {{ edges.length }}</span>
          <el-button size="small" type="warning" plain @click="openMergeQueue">
            合并队列 {{ mergeQueueGroups.length > 0 ? `(${mergeQueueGroups.length} 组)` : '' }}
          </el-button>
          <el-button size="small" type="success" @click="batchApproveSelected" :disabled="selectedNodeIds.size === 0">
            &#x2713; 批量通过 ({{ selectedNodeIds.size }})
          </el-button>
          <el-button size="small" type="danger" @click="batchRejectSelected" :disabled="selectedNodeIds.size === 0">
            &#x2717; 批量拒绝
          </el-button>
          <el-button size="small" @click="reloadGraph">&#x21bb; 刷新</el-button>
        </div>
      </div>
    </el-card>

    <el-row :gutter="12" class="main-row">
      <el-col :span="detailNode ? 16 : 24">
        <el-card shadow="never" class="graph-card">
          <div class="graph-legend">
            <span class="legend-item"><span class="dot pending"></span> 待审核</span>
            <span class="legend-item"><span class="dot approved"></span> 已通过</span>
            <span class="legend-item"><span class="dot rejected"></span> 已拒绝</span>
            <span class="legend-item">— 虚线=待审核 实线=已通过</span>
            <span class="legend-tip">点击节点查看详情 | Shift+点击多选 | 滚轮缩放 | 拖拽平移</span>
          </div>
          <div ref="cyContainerRef" class="cy-container" />
        </el-card>
      </el-col>

      <!-- 详情面板 -->
      <el-col :span="8" v-if="detailNode">
        <el-card shadow="never" class="detail-panel">
          <template #header>
            <div class="detail-header">
              <span>实体详情 #{{ detailNode.id }}</span>
              <el-button size="small" text @click="detailNode = null">&#x2715;</el-button>
            </div>
          </template>

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
              <el-tag size="small" :type="statusTagType(detailNode.review_status)">
                {{ statusLabel(detailNode.review_status) }}
              </el-tag>
            </el-form-item>
            <el-form-item label="已验证">
              <el-tag size="small" :type="detailNode.is_verified ? 'success' : 'info'">
                {{ detailNode.is_verified ? '是' : '否' }}
              </el-tag>
            </el-form-item>
          </el-form>

          <el-divider content-position="left">元数据 (entity_data)</el-divider>
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
              <el-button type="primary" size="small" @click="saveEntityData" :loading="savingData">
                &#x1f4be; 保存元数据
              </el-button>
            </el-form-item>
          </el-form>

          <el-divider content-position="left">关联关系</el-divider>
          <div class="related-relations">
            <div v-for="rel in relatedEdges" :key="rel.id" class="rel-item" :class="{ 'rel-pending': rel.review_status === 'pending', 'rel-approved': rel.review_status === 'approved' }">
              <div class="rel-type">
                <el-tag size="small" type="warning">{{ rel.relation_type }}</el-tag>
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

          <el-divider />
          <div class="detail-actions">
            <el-button v-if="detailNode.review_status === 'pending'" type="success" @click="doApproveEntity(detailNode.id)">
              &#x2713; 审核通过
            </el-button>
            <el-button v-if="detailNode.review_status === 'pending'" type="danger" @click="doRejectEntity(detailNode.id)">
              &#x2717; 审核拒绝
            </el-button>
            <el-button type="primary" plain @click="showNeighbors(detailNode.id)">
              &#x1f50d; 查看邻接
            </el-button>
            <el-button v-if="detailNode.source_doc_uuid" @click="goToDocument(detailNode.source_doc_uuid)">
              &#x1f4c4; 查看文档
            </el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 合并队列 Drawer -->
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
  // Base node
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
    'border-color': '#fff',
    'width': 'mapData(confidence_score, 0, 1, 28, 48)',
    'height': 'mapData(confidence_score, 0, 1, 28, 48)',
    'background-opacity': 0.9,
    'transition-property': 'background-color, border-color, border-width, border-opacity',
    'transition-duration': 180,
  }},
  // Status colors
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
  // Selected state
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
  // Hover state
  { selector: 'node:active', style: {
    'border-color': '#60a5fa',
    'border-width': 3,
    'border-opacity': 0.9,
    'overlay-opacity': 0.12,
    'overlay-color': '#3b82f6',
    'overlay-padding': 8,
  }},
  // Base edge
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
  nodeRepulsion: 6000,
  edgeElasticity: 0.4,
  idealEdgeLength: 120,
  gravity: 0.3,
  numIter: 800,
  initialTemp: 150,
  coolingFactor: 0.95,
  refresh: 15,
  animate: 'during' as const,
  animationDuration: 1000,
  fit: true,
  padding: 60,
  randomize: false,
}

function initCy(container: HTMLDivElement): Core {
  return cytoscape({
    container,
    style: cyStyles as any,
    wheelSensitivity: 0.3,
    minZoom: 0.08,
    maxZoom: 5,
    boxSelectionEnabled: true,
    autounselectify: false,
    selectionType: 'additive',
    layout: layoutOptions as any,
    // subtle background grid via canvas style
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
      // Single click: show detail, cy auto-deselects others with default behavior
      detailNode.value = nodeData
    }
  })

  cy.on('tap', (evt: EventObject) => {
    if (evt.target === cy) {
      // Canvas background clicked
      selectedNodeIds.clear()
    }
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
      limit: 800,
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

// ── 生命周期 ────────────────────────────────────────────────────────────

onMounted(async () => {
  await Promise.all([reloadGraph(), loadMergeQueue()])

  await nextTick()
  if (cyContainerRef.value && !cy) {
    cy = initCy(cyContainerRef.value)
    wireCyEvents()
    // Load initial data
    const data = transformToCyData(nodes.value, edges.value)
    cy.add(data.nodes)
    cy.add(data.edges)
    cy.layout(layoutOptions as any).run()
  }

  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  cy?.destroy()
  cy = null
})

watch([visibleNodeIds, visibleEdges], () => {
  applyFiltersToGraph()
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

.filter-bar { margin-bottom: 8px; flex-shrink: 0; }
.filter-bar :deep(.el-card__body) { padding: 10px 16px; }
.filter-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.filter-left { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.filter-middle { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.filter-right { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.conf-slider { display: flex; align-items: center; gap: 4px; }
.slider-label { font-size: 12px; color: #909399; white-space: nowrap; }
.local-filter :deep(.el-input__wrapper) { background: #f0f9eb; }
.node-count { font-size: 13px; color: #909399; white-space: nowrap; }

.main-row { flex: 1; overflow: hidden; }

.graph-card { height: 100%; display: flex; flex-direction: column; }
.graph-card :deep(.el-card__body) { flex: 1; display: flex; flex-direction: column; padding: 8px 12px 12px; overflow: hidden; }
.graph-legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: #606266; margin-bottom: 6px; flex-shrink: 0; }
.legend-item { display: flex; align-items: center; gap: 4px; }
.legend-tip { color: #c0c4cc; margin-left: auto; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.dot.pending { background: #f0a841; }
.dot.approved { background: #14b8a6; }
.dot.rejected { background: #e85d75; }

.cy-container {
  flex: 1;
  background: #f8f9fb;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  position: relative;
  z-index: 1;
  overflow: hidden;
}

.detail-panel { height: 100%; overflow-y: auto; }
.detail-header { display: flex; justify-content: space-between; align-items: center; }
.field-value { font-weight: 500; }
.empty-hint { color: #c0c4cc; font-size: 13px; }

.related-relations { max-height: 200px; overflow-y: auto; }
.rel-item { padding: 6px 8px; border-radius: 4px; margin-bottom: 4px; display: flex; align-items: center; gap: 6px; font-size: 13px; }
.rel-pending { border-left: 3px solid #e6a23c; background: #fef8ee; }
.rel-approved { border-left: 3px solid #0e9e8e; background: #e8f8f6; }
.rel-type { flex-shrink: 0; }
.rel-nodes { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rel-actions { flex-shrink: 0; }

.detail-actions { display: flex; gap: 8px; }

.merge-queue-panel { padding: 0 4px; }
.merge-card {
  border: 1px solid #ebeef5; border-radius: 8px; padding: 12px; margin-bottom: 10px;
  background: #fff;
}
.merge-card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.merge-source { font-size: 12px; color: #909399; }
.merge-row { display: flex; gap: 10px; }
.merge-col { flex: 1; min-width: 0; }
.merge-label { font-size: 11px; color: #909399; margin-bottom: 4px; }
.merge-value {
  font-size: 12px; background: #f5f7fa; border-radius: 4px; padding: 6px 8px;
  margin: 0; max-height: 120px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
}
.merge-card-actions { display: flex; gap: 8px; margin-top: 10px; }

@media (max-width: 1400px) {
  .filter-row { flex-direction: column; align-items: flex-start; }
  .filter-right { margin-left: 0; width: 100%; }
}
</style>
