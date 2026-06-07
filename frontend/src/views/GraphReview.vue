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
            合并队列 {{ mergeQueue.length > 0 ? `(${mergeQueue.length})` : '' }}
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
      <!-- 图谱画布 -->
      <el-col :span="detailNode ? 16 : 24">
        <el-card shadow="never" class="graph-card">
          <div class="graph-legend">
            <span class="legend-item"><span class="dot pending"></span> 待审核</span>
            <span class="legend-item"><span class="dot approved"></span> 已通过</span>
            <span class="legend-item"><span class="dot rejected"></span> 已拒绝</span>
            <span class="legend-item">— 虚线=待审核 实线=已通过</span>
            <span class="legend-tip">拖拽节点 | 滚轮缩放 | 点击节点查看详情 | Shift+点击多选</span>
          </div>
          <div class="canvas-wrapper" ref="canvasWrapperRef">
            <canvas
              ref="canvasRef"
              @mousedown="onMouseDown"
              @mousemove="onMouseMove"
              @mouseup="onMouseUp"
              @wheel="onWheel"
              @dblclick="onDblClick"
              @contextmenu.prevent
            />
          </div>
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
    <el-drawer v-model="showMergeDrawer" title="合并队列" size="460px" direction="rtl">
      <div class="merge-queue-panel" v-loading="mergeQueueLoading">
        <div v-for="m in mergeQueue" :key="m.id" class="merge-card">
          <div class="merge-card-header">
            <el-tag size="small" type="danger">{{ m.field_name }}</el-tag>
            <span class="merge-source">entity #{{ m.source_entity_id }} → dict #{{ m.target_dict_id }}</span>
            <el-tag size="small" type="info">{{ m.source_site }}</el-tag>
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
            <el-button size="small" type="success" @click="doResolveMerge(m.id, 'accept_new')">采用新值</el-button>
            <el-button size="small" type="warning" @click="doResolveMerge(m.id, 'keep_existing')">保留现有</el-button>
          </div>
        </div>
        <el-empty v-if="!mergeQueue.length && !mergeQueueLoading" description="暂无待处理的合并冲突" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  getReviewGraph, getReviewStats,
  approveEntity, rejectEntity,
  approveRelation, rejectRelation,
  batchApproveEntities, batchRejectEntities,
  patchEntity,
  getMergeQueue, resolveMerge,
  type GraphNode, type GraphEdge, type ReviewStats,
  type MergeQueueItem,
} from '@/api/review'

const router = useRouter()

// ── 数据状态 ────────────────────────────────────────────────────────────

const loading = ref(false)
const savingData = ref(false)
const mergeQueueLoading = ref(false)
const nodes = ref<GraphNode[]>([])
const edges = ref<GraphEdge[]>([])
const stats = ref<ReviewStats | null>(null)
const entityTypeOptions = ref<string[]>([])
const siteOptions = ref<string[]>([])

// ── 筛选 ────────────────────────────────────────────────────────────────

const filterEntityTypes = ref<string[]>([])
const filterSourceSite = ref('')
const filterReviewStatus = ref<string[]>(['pending', 'approved'])
const filterDocUuid = ref('')

// ── 本地筛选（客户端过滤，不请求 API）──────────────────────────────────
const filterName = ref('')                    // 实体名模糊搜索
const focusNodeId = ref<number | null>(null)  // 邻接模式：只看该节点 + 1-hop 邻居
const filterIsolated = ref('all')             // all / hide / only
const filterVerified = ref('all')             // all / yes / no
const filterMinConf = ref(0)                  // 最低置信度

const focusNodeName = computed(() => {
  if (focusNodeId.value === null) return ''
  return nodes.value.find(n => n.id === focusNodeId.value)?.entity_name || ''
})

// 每个节点的度数（用于孤立点判断）
const nodeDegrees = computed(() => {
  const deg: Record<number, number> = {}
  for (const n of nodes.value) deg[n.id] = 0
  for (const e of edges.value) {
    if (e.from_entity_id != null) deg[e.from_entity_id] = (deg[e.from_entity_id] || 0) + 1
    if (e.to_entity_id != null) deg[e.to_entity_id] = (deg[e.to_entity_id] || 0) + 1
  }
  return deg
})

// 邻接模式的邻居集合
const neighborSet = computed(() => {
  if (focusNodeId.value === null) return null
  const s = new Set<number>([focusNodeId.value])
  for (const e of edges.value) {
    if (e.from_entity_id === focusNodeId.value && e.to_entity_id != null) s.add(e.to_entity_id)
    if (e.to_entity_id === focusNodeId.value && e.from_entity_id != null) s.add(e.from_entity_id)
  }
  return s
})

// 过滤后的可见节点 ID 集合
const visibleNodeIds = computed(() => {
  let result = new Set<number>(nodes.value.map(n => n.id))

  // 邻接模式
  if (neighborSet.value !== null) {
    result = new Set([...result].filter(id => neighborSet.value!.has(id)))
  }

  // 实体名搜索
  if (filterName.value.trim()) {
    const q = filterName.value.trim().toLowerCase()
    result = new Set(
      [...result].filter(id => {
        const n = nodes.value.find(nd => nd.id === id)
        return n?.entity_name.toLowerCase().includes(q)
      })
    )
  }

  // 孤立点
  if (filterIsolated.value === 'hide') {
    result = new Set([...result].filter(id => (nodeDegrees.value[id] || 0) > 0))
  } else if (filterIsolated.value === 'only') {
    result = new Set([...result].filter(id => (nodeDegrees.value[id] || 0) === 0))
  }

  // 验证状态
  if (filterVerified.value === 'yes') {
    result = new Set([...result].filter(id => nodes.value.find(n => n.id === id)?.is_verified))
  } else if (filterVerified.value === 'no') {
    result = new Set([...result].filter(id => !nodes.value.find(n => n.id === id)?.is_verified))
  }

  // 置信度
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
const mergeQueue = ref<MergeQueueItem[]>([])

async function openMergeQueue() {
  showMergeDrawer.value = true
  await loadMergeQueue()
}

async function loadMergeQueue() {
  mergeQueueLoading.value = true
  try {
    const res = await getMergeQueue(50)
    mergeQueue.value = res.items
  } catch {
    // ignore
  } finally {
    mergeQueueLoading.value = false
  }
}

async function doResolveMerge(mergeId: number, resolution: string) {
  try {
    await resolveMerge(mergeId, '审核员', resolution)
    ElMessage.success(resolution === 'accept_new' ? '已采用新值' : '已保留现有值')
    mergeQueue.value = mergeQueue.value.filter(m => m.id !== mergeId)
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

// ── Canvas 力导向图 ──────────────────────────────────────────────────────

const canvasRef = ref<HTMLCanvasElement | null>(null)
const canvasWrapperRef = ref<HTMLDivElement | null>(null)

interface SimNode {
  id: number
  x: number
  y: number
  vx: number
  vy: number
  radius: number
  color: string
  status: string
}

interface SimEdge {
  source: number
  target: number
  status: string
}

let simNodes: SimNode[] = []
let simEdges: SimEdge[] = []
let animFrameId = 0
let transform = { x: 0, y: 0, scale: 1 }
let dragging: { node: SimNode; ox: number; oy: number } | null = null
let hoveredNode: SimNode | null = null
let isShiftDown = false
let canvasWidth = 800
let canvasHeight = 600

function initSimulation() {
  const w = canvasWidth
  const h = canvasHeight
  const cx = w / 2
  const cy = h / 2

  // 初始化节点位置（圆形排布 + 随机偏移）
  simNodes = nodes.value.map((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(nodes.value.length, 1)
    const r = Math.min(w, h) * 0.35
    return {
      id: n.id,
      x: cx + r * Math.cos(angle) + (Math.random() - 0.5) * 80,
      y: cy + r * Math.sin(angle) + (Math.random() - 0.5) * 80,
      vx: 0,
      vy: 0,
      radius: 12 + Math.min(n.confidence_score * 10, 12),
      color: nodeColor(n.review_status),
      status: n.review_status,
    }
  })

  // 构建邻接表
  const nodeIdSet = new Set(simNodes.map(n => n.id))
  simEdges = edges.value
    .filter(e => nodeIdSet.has(e.from_entity_id!) && nodeIdSet.has(e.to_entity_id!))
    .map(e => ({
      source: e.from_entity_id!,
      target: e.to_entity_id!,
      status: e.review_status,
    }))
}

function nodeColor(status: string): string {
  if (status === 'approved' || status === 'auto_approved') return '#0e9e8e'
  if (status === 'rejected') return '#c0392b'
  return '#e6a23c' // pending
}

function tick() {
  if (!simNodes.length) return

  // 力参数
  const repulsion = 800
  const attraction = 0.005
  const damping = 0.85
  const centerForce = 0.01
  const w = canvasWidth
  const h = canvasHeight
  const cx = w / 2
  const cy = h / 2

  for (const n of simNodes) {
    if (dragging && dragging.node === n) continue

    // 中心引力
    n.vx += (cx - n.x) * centerForce
    n.vy += (cy - n.y) * centerForce

    // 节点间斥力
    for (const m of simNodes) {
      if (n === m) continue
      let dx = n.x - m.x
      let dy = n.y - m.y
      const dist = Math.sqrt(dx * dx + dy * dy) || 1
      const force = repulsion / (dist * dist)
      n.vx += (dx / dist) * force
      n.vy += (dy / dist) * force
    }
  }

  // 边引力
  for (const e of simEdges) {
    const src = simNodes.find(n => n.id === e.source)
    const tgt = simNodes.find(n => n.id === e.target)
    if (!src || !tgt) continue
    const dx = tgt.x - src.x
    const dy = tgt.y - src.y
    const dist = Math.sqrt(dx * dx + dy * dy) || 1
    const force = dist * attraction
    const fx = (dx / dist) * force
    const fy = (dy / dist) * force
    if (!dragging || dragging.node !== src) { src.vx += fx; src.vy += fy }
    if (!dragging || dragging.node !== tgt) { tgt.vx -= fx; tgt.vy -= fy }
  }

  // 应用速度 + 阻尼 + 边界约束
  for (const n of simNodes) {
    if (dragging && dragging.node === n) continue
    n.vx *= damping
    n.vy *= damping
    n.x += n.vx
    n.y += n.vy
    n.x = Math.max(n.radius, Math.min(w - n.radius, n.x))
    n.y = Math.max(n.radius, Math.min(h - n.radius, n.y))
  }
}

function draw() {
  const canvas = canvasRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')!
  const { x, y, scale } = transform

  ctx.clearRect(0, 0, canvasWidth, canvasHeight)
  ctx.save()
  ctx.translate(x, y)
  ctx.scale(scale, scale)

  const visSet = visibleNodeIds.value
  const visEdges = visibleEdges.value

  // 边（仅两端都可见的）
  for (const e of visEdges) {
    const src = simNodes.find(n => n.id === e.from_entity_id)
    const tgt = simNodes.find(n => n.id === e.to_entity_id)
    if (!src || !tgt) continue
    if (!visSet.has(src.id) || !visSet.has(tgt.id)) continue
    ctx.beginPath()
    ctx.moveTo(src.x, src.y)
    ctx.lineTo(tgt.x, tgt.y)
    ctx.strokeStyle = e.review_status === 'approved' || e.review_status === 'auto_approved' ? '#0e9e8e' : '#e6a23c'
    ctx.lineWidth = 1.2 / scale
    if (e.review_status === 'pending') ctx.setLineDash([4 / scale, 3 / scale])
    else ctx.setLineDash([])
    ctx.stroke()
  }
  ctx.setLineDash([])

  // 节点（仅可见的）
  for (const n of simNodes) {
    if (!visSet.has(n.id)) continue

    const isSelected = selectedNodeIds.has(n.id)
    const isHovered = hoveredNode === n
    const isDetail = detailNode.value?.id === n.id
    const isFocus = focusNodeId.value === n.id

    // 选中/悬浮/焦点光晕
    if (isSelected || isHovered || isDetail) {
      ctx.beginPath()
      ctx.arc(n.x, n.y, n.radius + 5, 0, Math.PI * 2)
      ctx.fillStyle = isDetail || isFocus ? 'rgba(64,158,255,0.35)' : 'rgba(64,158,255,0.15)'
      ctx.fill()
    }

    // 节点圆
    ctx.beginPath()
    ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2)
    ctx.fillStyle = n.color
    ctx.fill()
    ctx.strokeStyle = isFocus ? '#409EFF' : '#fff'
    ctx.lineWidth = isFocus ? 2.5 / scale : 1.5 / scale
    ctx.stroke()

    // 已验证标记（小对勾）
    const nodeData = nodes.value.find(nd => nd.id === n.id)
    if (nodeData?.is_verified) {
      ctx.fillStyle = '#fff'
      ctx.font = `${10 / scale}px sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText('✓', n.x, n.y)
    }

    // 标签
    const label = nodeData?.entity_name || ''
    if (label && scale > 0.3) {
      ctx.fillStyle = '#303133'
      ctx.font = `${11 / scale}px sans-serif`
      ctx.textAlign = 'center'
      ctx.fillText(label.slice(0, 12), n.x, n.y + n.radius + 12 / scale)
    }
  }

  ctx.restore()
}

function animate() {
  tick()
  draw()
  animFrameId = requestAnimationFrame(animate)
}

// ── Canvas 交互 ──────────────────────────────────────────────────────────

function canvasPos(e: MouseEvent): { x: number; y: number } {
  const rect = canvasRef.value!.getBoundingClientRect()
  return {
    x: (e.clientX - rect.left - transform.x) / transform.scale,
    y: (e.clientY - rect.top - transform.y) / transform.scale,
  }
}

function hitTest(sx: number, sy: number): SimNode | null {
  const visSet = visibleNodeIds.value
  for (let i = simNodes.length - 1; i >= 0; i--) {
    const n = simNodes[i]
    if (!visSet.has(n.id)) continue
    const dx = n.x - sx
    const dy = n.y - sy
    if (Math.sqrt(dx * dx + dy * dy) <= n.radius + 4) return n
  }
  return null
}

function onMouseDown(e: MouseEvent) {
  const pos = canvasPos(e)
  const hit = hitTest(pos.x, pos.y)

  if (hit) {
    if (e.shiftKey) {
      // Shift+点击：多选
      if (selectedNodeIds.has(hit.id)) selectedNodeIds.delete(hit.id)
      else selectedNodeIds.add(hit.id)
    } else {
      // 点击节点
      selectNode(hit.id)
    }
    dragging = { node: hit, ox: hit.x - pos.x, oy: hit.y - pos.y }
  } else {
    // 点击空白：拖拽画布
    dragging = { node: null as any, ox: e.clientX - transform.x, oy: e.clientY - transform.y }
  }
}

function onMouseMove(e: MouseEvent) {
  const pos = canvasPos(e)

  if (dragging) {
    if (dragging.node) {
      dragging.node.x = pos.x + dragging.ox
      dragging.node.y = pos.y + dragging.oy
    } else {
      transform.x = e.clientX - dragging.ox
      transform.y = e.clientY - dragging.oy
    }
  } else {
    hoveredNode = hitTest(pos.x, pos.y)
    const canvas = canvasRef.value!
    canvas.style.cursor = hoveredNode ? 'pointer' : 'default'
  }
}

function onMouseUp(_e: MouseEvent) {
  dragging = null
}

function onWheel(e: WheelEvent) {
  e.preventDefault()
  const delta = e.deltaY > 0 ? 0.9 : 1.1
  const newScale = transform.scale * delta
  if (newScale < 0.1 || newScale > 5) return
  // 以鼠标位置为中心缩放
  const rect = canvasRef.value!.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const my = e.clientY - rect.top
  transform.x = mx - (mx - transform.x) * (newScale / transform.scale)
  transform.y = my - (my - transform.y) * (newScale / transform.scale)
  transform.scale = newScale
}

function onDblClick(e: MouseEvent) {
  const pos = canvasPos(e)
  const hit = hitTest(pos.x, pos.y)
  if (hit) {
    // 双击展开：以该节点为中心，重新加载关联节点
    const nodeData = nodes.value.find(n => n.id === hit.id)
    if (nodeData?.source_doc_uuid) {
      filterDocUuid.value = nodeData.source_doc_uuid
      reloadGraph()
    }
  }
}

// ── 节点选择与详情 ──────────────────────────────────────────────────────

function selectNode(id: number) {
  const node = nodes.value.find(n => n.id === id)
  if (node) {
    detailNode.value = node
    // 复制 entity_data 到可编辑对象
    Object.keys(editableData).forEach(k => delete editableData[k])
    Object.assign(editableData, JSON.parse(JSON.stringify(node.entity_data || {})))
    if (!isShiftDown) {
      selectedNodeIds.clear()
      selectedNodeIds.add(id)
    }
  }
}

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

    const [graphRes, statsRes] = await Promise.all([
      getReviewGraph(params),
      getReviewStats(),
    ])
    nodes.value = graphRes.nodes
    edges.value = graphRes.edges
    stats.value = statsRes

    // 提取筛选选项
    entityTypeOptions.value = Object.keys(statsRes.by_type).sort()
    siteOptions.value = Object.keys(statsRes.by_site).sort()

    detailNode.value = null
    Object.keys(editableData).forEach(k => delete editableData[k])
    selectedNodeIds.clear()
    focusNodeId.value = null

    await nextTick()
    initSimulation()
  } finally {
    loading.value = false
  }
}

// ── 操作 ────────────────────────────────────────────────────────────────

async function doApproveEntity(id: number) {
  await approveEntity(id, '审核员')
  ElMessage.success('已通过')
  updateNodeStatus(id, 'approved')
}

async function doRejectEntity(id: number) {
  await rejectEntity(id, '审核员')
  ElMessage.warning('已拒绝')
  updateNodeStatus(id, 'rejected')
}

function updateNodeStatus(id: number, status: string) {
  const node = nodes.value.find(n => n.id === id)
  if (node) node.review_status = status
  const simNode = simNodes.find(n => n.id === id)
  if (simNode) { simNode.status = status; simNode.color = nodeColor(status) }
  if (detailNode.value?.id === id) detailNode.value.review_status = status
}

async function doApproveRelation(id: number) {
  await approveRelation(id, '审核员')
  const edge = edges.value.find(e => e.id === id)
  if (edge) edge.review_status = 'approved'
  const simEdge = simEdges.find(e => {
    const r = edges.value.find(re => re.id === id)
    return r && e.source === r.from_entity_id && e.target === r.to_entity_id
  })
  if (simEdge) simEdge.status = 'approved'
  ElMessage.success('关系已通过')
}

async function doRejectRelation(id: number) {
  await rejectRelation(id, '审核员')
  const edge = edges.value.find(e => e.id === id)
  if (edge) edge.review_status = 'rejected'
  ElMessage.warning('关系已拒绝')
}

async function saveEntityData() {
  if (!detailNode.value) return
  savingData.value = true
  try {
    const rawData: Record<string, any> = {}
    for (const [k, v] of Object.entries(editableData)) {
      // 尝试解析数组
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
    await Promise.all([...selectedNodeIds].map(id => approveEntity(id, '审核员')))
    for (const id of selectedNodeIds) updateNodeStatus(id, 'approved')
    ElMessage.success(`已批量通过 ${selectedNodeIds.size} 个实体`)
    selectedNodeIds.clear()
  } catch (e: any) {
    ElMessage.error('批量通过失败')
  }
}

async function batchRejectSelected() {
  if (selectedNodeIds.size === 0) return
  try {
    await Promise.all([...selectedNodeIds].map(id => rejectEntity(id, '审核员')))
    for (const id of selectedNodeIds) updateNodeStatus(id, 'rejected')
    ElMessage.warning(`已批量拒绝 ${selectedNodeIds.size} 个实体`)
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

function resetLocalFilters() {
  filterName.value = ''
  focusNodeId.value = null
  filterIsolated.value = 'all'
  filterVerified.value = 'all'
  filterMinConf.value = 0
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

// ── 生命周期 ────────────────────────────────────────────────────────────

function onKeyDown(e: KeyboardEvent) { isShiftDown = e.shiftKey }
function onKeyUp(e: KeyboardEvent) { isShiftDown = e.shiftKey }
function onResize() {
  if (!canvasWrapperRef.value || !canvasRef.value) return
  const rect = canvasWrapperRef.value.getBoundingClientRect()
  canvasWidth = rect.width
  canvasHeight = rect.height
  const dpr = window.devicePixelRatio || 1
  canvasRef.value.width = canvasWidth * dpr
  canvasRef.value.height = canvasHeight * dpr
  canvasRef.value.style.width = canvasWidth + 'px'
  canvasRef.value.style.height = canvasHeight + 'px'
  const ctx = canvasRef.value.getContext('2d')!
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
}

onMounted(async () => {
  window.addEventListener('keydown', onKeyDown)
  window.addEventListener('keyup', onKeyUp)
  window.addEventListener('resize', onResize)
  onResize()
  await Promise.all([reloadGraph(), loadMergeQueue()])
  animFrameId = requestAnimationFrame(animate)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('keyup', onKeyUp)
  window.removeEventListener('resize', onResize)
  cancelAnimationFrame(animFrameId)
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

/* 筛选栏 */
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

/* 主区域 */
.main-row { flex: 1; overflow: hidden; }

/* 图谱 */
.graph-card { height: 100%; display: flex; flex-direction: column; }
.graph-card :deep(.el-card__body) { flex: 1; display: flex; flex-direction: column; padding: 8px 12px 12px; overflow: hidden; }
.graph-legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: #606266; margin-bottom: 6px; flex-shrink: 0; }
.legend-item { display: flex; align-items: center; gap: 4px; }
.legend-tip { color: #c0c4cc; margin-left: auto; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.dot.pending { background: #e6a23c; }
.dot.approved { background: #0e9e8e; }
.dot.rejected { background: #c0392b; }
.canvas-wrapper { flex: 1; overflow: hidden; background: #fafbfc; border-radius: 6px; border: 1px solid #ebeef5; }
.canvas-wrapper canvas { display: block; }

/* 详情面板 */
.detail-panel { height: 100%; overflow-y: auto; }
.detail-header { display: flex; justify-content: space-between; align-items: center; }
.field-value { font-weight: 500; }
.empty-hint { color: #c0c4cc; font-size: 13px; }

/* 关联关系 */
.related-relations { max-height: 200px; overflow-y: auto; }
.rel-item { padding: 6px 8px; border-radius: 4px; margin-bottom: 4px; display: flex; align-items: center; gap: 6px; font-size: 13px; }
.rel-pending { border-left: 3px solid #e6a23c; background: #fef8ee; }
.rel-approved { border-left: 3px solid #0e9e8e; background: #e8f8f6; }
.rel-type { flex-shrink: 0; }
.rel-nodes { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rel-actions { flex-shrink: 0; }

.detail-actions { display: flex; gap: 8px; }

/* 合并队列 */
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

/* 响应式 */
@media (max-width: 1400px) {
  .filter-row { flex-direction: column; align-items: flex-start; }
  .filter-right { margin-left: 0; width: 100%; }
}
</style>
