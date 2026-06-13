<template>
  <div v-loading="loading">
    <el-page-header @back="$router.back()" :content="doc?.title || '文档详情'" />

    <el-row :gutter="16" style="margin-top:16px" v-if="doc">
      <!-- 基本信息 -->
      <el-col :span="24">
        <el-card shadow="never" style="margin-bottom:16px">
          <el-descriptions :column="4" border size="small">
            <el-descriptions-item label="来源站点">{{ doc.source_site }}</el-descriptions-item>
            <el-descriptions-item label="发布日期">{{ doc.publish_date }}</el-descriptions-item>
            <el-descriptions-item label="文档类型">{{ doc.doc_type }}</el-descriptions-item>
            <el-descriptions-item label="语言">{{ doc.language }}</el-descriptions-item>
            <el-descriptions-item label="置信度">
              <span :style="{ color: doc.confidence_score < 0.8 ? 'var(--dp-danger)' : 'var(--dp-success)' }">
                {{ doc.confidence_score.toFixed(2) }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="解析方式">{{ doc.parse_method }}</el-descriptions-item>
            <el-descriptions-item label="文档状态">
              <el-tag size="small">{{ doc.doc_status }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="抽取状态">
              <el-tag :type="extractionStatusType(doc.extraction_status)" size="small">
                {{ doc.extraction_status }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="抽取时间">
              <span :style="{ color: 'var(--dp-text-secondary)' }">{{ formatTime(doc.extracted_at) }}</span>
            </el-descriptions-item>
          </el-descriptions>

          <!-- 抽取错误提示 -->
          <el-alert
            v-if="doc.extraction_status === 'failed'"
            :title="`抽取失败：${doc.extraction_error}`"
            type="error"
            style="margin-top:12px"
            :closable="false"
          />

          <!-- 操作按钮 -->
          <div style="margin-top:12px">
            <el-button
              v-if="doc.extraction_status === 'pending' || doc.extraction_status === 'failed'"
              type="primary"
              @click="triggerExtract"
              :loading="triggering"
            >触发 LLM 抽取</el-button>
            <el-button
              v-if="doc.extraction_status === 'done'"
              type="success"
              @click="$router.push(`/review/${doc.uuid}`)"
            >查看抽取结果 →</el-button>
            <el-button
              type="warning"
              @click="triggerRecrawl"
              :loading="recrawling"
            >重新爬取</el-button>
          </div>
        </el-card>
      </el-col>

      <!-- 原文 -->
      <el-col :span="doc.attachments.length ? 18 : 24">
        <el-card shadow="never">
          <template #header>原文全文</template>
          <div class="markdown-body" v-html="renderedContent" />
        </el-card>
      </el-col>

      <!-- 附件 -->
      <el-col :span="6" v-if="doc.attachments.length">
        <el-card shadow="never">
          <template #header>附件（{{ doc.attachments.length }}）</template>
          <el-list>
            <el-list-item v-for="att in doc.attachments" :key="att.file_url">
              <el-link :href="att.file_url" target="_blank" type="primary">
                {{ att.file_name || att.file_url }}
              </el-link>
            </el-list-item>
          </el-list>
        </el-card>
      </el-col>

      <!-- 知识图谱 -->
      <el-col :span="24" v-if="doc.extraction_status === 'done'">
        <el-card shadow="never" style="margin-top:16px">
          <template #header>
            <div style="display:flex;align-items:center;justify-content:space-between">
              <span>实体关系图（{{ graphNodeCount }} 实体 · {{ graphEdgeCount }} 关系）</span>
              <div style="display:flex;align-items:center;gap:12px">
                <div class="graph-legend-inline">
                  <span class="legend-dot pending"></span>待审核
                  <span class="legend-dot approved"></span>已通过
                  <span class="legend-dot rejected"></span>已拒绝
                </div>
                <el-button size="small" type="primary" plain title="自动调整布局，使实体不堆叠" @click="rerunLayout">&#x2699;&#xFE0F; 自动调整</el-button>
              </div>
            </div>
          </template>
          <div v-if="graphLoading" style="text-align:center;padding:40px;color:var(--dp-text-secondary)">加载图谱...</div>
          <div v-else-if="graphEmpty" style="text-align:center;padding:40px;color:var(--dp-text-secondary)">该文档暂无实体数据</div>
          <div v-else ref="graphContainerRef" class="doc-graph-container" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import cytoscape, { type Core } from 'cytoscape'
import { marked } from 'marked'
import { getDocument, recrawlDocument, type DocumentDetail } from '@/api/documents'
import { triggerExtraction } from '@/api/extraction'
import { getReviewGraph } from '@/api/review'
import { relativeTime } from '@/utils/time'

const route = useRoute()
const router = useRouter()
const uuid = route.params.uuid as string

const doc = ref<DocumentDetail | null>(null)
const loading = ref(false)
const triggering = ref(false)
const recrawling = ref(false)

const renderedContent = computed(() => {
  if (!doc.value?.raw_content) return '<p style="color:var(--dp-text-secondary)">暂无内容</p>'
  return marked(doc.value.raw_content) as string
})

async function load() {
  loading.value = true
  try {
    doc.value = await getDocument(uuid)
  } finally {
    loading.value = false
  }
}

async function triggerExtract() {
  triggering.value = true
  try {
    await triggerExtraction([uuid])
    ElMessage.success('已触发抽取，请稍后刷新查看状态')
    await load()
  } finally {
    triggering.value = false
  }
}

async function triggerRecrawl() {
  recrawling.value = true
  try {
    const res = await recrawlDocument(uuid)
    ElMessage.success(res.message || '重爬任务已提交，请稍后刷新查看状态')
    setTimeout(load, 1500)
  } finally {
    recrawling.value = false
  }
}

const extractionStatusType = (s: string) =>
  ({ pending: 'info', queued: '', processing: 'warning', done: 'success', failed: 'danger' }[s] ?? '')

const timeTick = ref(0)
const formatTime = (iso: string | null) => {
  timeTick.value
  return relativeTime(iso)
}

let timeTimer: ReturnType<typeof setInterval> | null = null

// ── 文档知识图谱 ──────────────────────────────────────────────────────

const graphContainerRef = ref<HTMLDivElement | null>(null)
const graphLoading = ref(false)
const graphNodeCount = ref(0)
const graphEdgeCount = ref(0)
const graphEmpty = computed(() => graphNodeCount.value === 0)
let graphCy: Core | null = null

const graphStyles = [
  { selector: 'node', style: { 'shape': 'ellipse', 'label': 'data(label)', 'font-size': 10, 'font-family': 'system-ui, -apple-system, sans-serif', 'color': '#303133', 'text-valign': 'bottom', 'text-halign': 'center', 'text-margin-y': 4, 'text-wrap': 'ellipsis', 'text-max-width': 80, 'width': 26, 'height': 26, 'border-width': 2, 'border-color': '#fff', 'transition-property': 'background-color', 'transition-duration': 150 }},
  { selector: 'node.pending', style: { 'background-color': '#f0a841' }},
  { selector: 'node.approved', style: { 'background-color': '#14b8a6' }},
  { selector: 'node.rejected', style: { 'background-color': '#e85d75' }},
  { selector: 'edge', style: { 'width': 1.4, 'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'arrow-scale': 0.7, 'line-opacity': 0.7 }},
  { selector: 'edge.pending', style: { 'line-color': '#f0a841', 'line-style': 'dashed', 'line-dash-pattern': [6, 4], 'target-arrow-color': '#f0a841' }},
  { selector: 'edge.approved', style: { 'line-color': '#14b8a6', 'target-arrow-color': '#14b8a6' }},
  { selector: 'edge.rejected', style: { 'line-color': '#e85d75', 'line-style': 'dashed', 'line-dash-pattern': [4, 4], 'target-arrow-color': '#e85d75', 'line-opacity': 0.4 }},
]

const graphLayout = {
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

async function loadGraph() {
  if (!doc.value || doc.value.extraction_status !== 'done') return
  graphLoading.value = true
  try {
    const res = await getReviewGraph({ source_doc_uuid: uuid, include_relations: true, limit: 200 })
    graphNodeCount.value = res.nodes.length
    graphEdgeCount.value = res.edges.length
    if (res.nodes.length === 0) {
      graphLoading.value = false
      return
    }

    graphLoading.value = false
    await nextTick()

    if (graphContainerRef.value && !graphCy) {
      graphCy = cytoscape({
        container: graphContainerRef.value,
        style: graphStyles as any,
        wheelSensitivity: 0.6,
        minZoom: 0.3,
        maxZoom: 3,
        layout: graphLayout as any,
      })
      graphCy.on('tap', 'node', () => {
        router.push(`/review/${uuid}`)
      })
      graphCy.on('zoom', () => updateGraphLabels())
    }

    if (!graphCy) return
    graphCy.elements().remove()
    for (const n of res.nodes) {
      graphCy.add({
        group: 'nodes', data: { id: String(n.id), label: n.entity_name },
        classes: n.review_status === 'approved' || n.review_status === 'auto_approved' ? 'approved' : n.review_status === 'rejected' ? 'rejected' : 'pending',
      })
    }
    for (const e of res.edges) {
      if (e.from_entity_id == null || e.to_entity_id == null) continue
      graphCy.add({
        group: 'edges', data: { id: String(e.id), source: String(e.from_entity_id), target: String(e.to_entity_id) },
        classes: e.review_status === 'approved' || e.review_status === 'auto_approved' ? 'approved' : e.review_status === 'rejected' ? 'rejected' : 'pending',
      })
    }
    graphCy.layout(graphLayout as any).run()
    updateGraphLabels()
  } catch {
    graphLoading.value = false
  }
}

function updateGraphLabels() {
  if (!graphCy) return
  const zoom = graphCy.zoom()
  const showLabel = zoom > 0.8
  const fontSize = zoom > 1.5 ? '10px' : zoom > 0.8 ? '8px' : '0px'
  graphCy.nodes().style({
    'font-size': fontSize,
    'text-opacity': showLabel ? 1 : 0,
  })
}

function rerunLayout() {
  if (!graphCy) return

  // 先随机化位置，确保节点不重叠
  const graphNodes = graphCy.nodes()
  const box = graphCy.container()?.getBoundingClientRect()
  const w = box ? box.width : 800
  const h = box ? box.height : 420

  graphNodes.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / graphNodes.length
    const radius = Math.min(w, h) * 0.35
    n.position({
      x: w / 2 + radius * Math.cos(angle),
      y: h / 2 + radius * Math.sin(angle),
    })
  })

  // 执行力导向布局，用固定的大数值确保节点充分展开
  graphCy.layout({
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
}

function destroyGraph() {
  graphCy?.destroy()
  graphCy = null
}

onMounted(async () => {
  await load()
  await nextTick()
  if (doc.value?.extraction_status === 'done') loadGraph()
  timeTimer = setInterval(() => timeTick.value++, 10000)
})

onUnmounted(() => {
  destroyGraph()
  if (timeTimer) clearInterval(timeTimer)
})
</script>

<style scoped>
.markdown-body {
  line-height: 1.7;
  font-size: 14px;
  max-height: 70vh;
  overflow-y: auto;
  padding: 4px;
}
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) { margin: 12px 0 8px; }
.markdown-body :deep(p) { margin: 6px 0; }
.markdown-body :deep(pre) {
  background: var(--dp-muted-bg);
  padding: 12px;
  border-radius: var(--dp-radius-xs);
  overflow-x: auto;
}

.doc-graph-container {
  width: 100%;
  height: 560px;
  border-radius: var(--dp-radius-lg);
  overflow: hidden;
  background: var(--dp-muted-bg-secondary);
}

.graph-legend-inline {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
  color: var(--dp-text-secondary);
}

.legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 4px;
}
.legend-dot.pending { background: var(--dp-graph-pending); }
.legend-dot.approved { background: var(--dp-graph-approved); }
.legend-dot.rejected { background: var(--dp-graph-rejected); }
</style>
