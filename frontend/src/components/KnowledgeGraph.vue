<template>
  <div class="kg-container">
    <div v-if="nodes.length === 0" class="kg-empty">暂无数据</div>
    <div ref="graphRef" class="kg-graph" />
    <div class="kg-legend">
      <span class="legend-item"><span class="legend-dot approved" /> 通过</span>
      <span class="legend-item"><span class="legend-dot rejected" /> 驳回</span>
      <span class="legend-item"><span class="legend-dot pending" /> 待审</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import G6, { type Graph, type GraphData } from '@antv/g6'

interface GraphNode {
  id: string
  label: string
  type: string
  decision: string
}

interface GraphEdge {
  source: string
  target: string
  label: string
  decision: string
}

const props = defineProps<{
  nodes: GraphNode[]
  edges: GraphEdge[]
}>()

const graphRef = ref<HTMLDivElement | null>(null)
let graph: Graph | null = null

const DECISION_COLORS: Record<string, string> = {
  approved: '#52c41a',
  rejected: '#f5222d',
  escalated: '#faad14',
  pending: '#bfbfbf',
  error: '#bfbfbf',
}

const ENTITY_TYPE_COLORS: Record<string, string> = {
  Drug: '#5b8ff9',
  Company: '#5ad8a6',
  Target: '#f6bd16',
  Disease: '#e86452',
  ClinicalTrial: '#6dc8ec',
  Compound: '#9270ca',
  Pathway: '#ff9d4d',
  Policy: '#269a99',
  Indication: '#5d7092',
  DevProject: '#ff99c3',
  DealEvent: '#b8c1ec',
  DealItem: '#a0d911',
  TrialIndication: '#f6903d',
  TCMFormula: '#c19277',
  SafetyEvent: '#eb2f96',
  RecallEvent: '#9f79ee',
  ApprovalEvent: '#52c41a',
}

function buildGraphData(): GraphData {
  const nodeMap = new Map(props.nodes.map(n => [n.id, n]))
  const dataNodes = props.nodes.map(n => ({
    id: n.id,
    label: n.label.length > 20 ? n.label.slice(0, 20) + '...' : n.label,
    type: 'kg-node',
    entityType: n.type,
    decision: n.decision,
  }))

  const dataEdges = props.edges
    .filter(e => nodeMap.has(e.source) && nodeMap.has(e.target))
    .map(e => ({
      source: e.source,
      target: e.target,
      label: e.label,
      type: 'kg-edge',
      decision: e.decision,
    }))

  return { nodes: dataNodes, edges: dataEdges }
}

function createGraph() {
  if (!graphRef.value) return

  const width = graphRef.value.clientWidth || 800
  const height = graphRef.value.clientHeight || 500
  const data = buildGraphData()

  if (graph) { graph.destroy(); graph = null }

  graph = new G6.Graph({
    container: graphRef.value,
    width,
    height,
    fitView: true,
    fitViewPadding: 40,
    animate: true,
    layout: {
      type: 'force',
      preventOverlap: true,
      nodeStrength: -300,
      edgeStrength: 0.1,
      linkDistance: 160,
      gravity: 10,
    },
    defaultNode: {
      type: 'circle',
      size: 32,
      labelCfg: {
        position: 'bottom',
        offset: 8,
        style: { fontSize: 11, fill: '#333' },
      },
    },
    defaultEdge: {
      type: 'polyline',
      style: { stroke: '#ccc', lineWidth: 1.5, endArrow: { path: G6.Arrow.triangle(6, 8, 0), fill: '#ccc' } },
      labelCfg: {
        autoRotate: true,
        style: { fontSize: 10, fill: '#666', background: { fill: '#fff', padding: [2, 4, 2, 4], radius: 2 } },
      },
    },
    modes: {
      default: ['drag-canvas', 'zoom-canvas', 'drag-node'],
    },
    nodeStateStyles: {
      hover: { lineWidth: 3, shadowColor: '#999', shadowBlur: 8 },
    },
  })

  graph.node(node => ({
    style: {
      fill: node.decision === 'approved'
        ? DECISION_COLORS.approved
        : node.decision === 'rejected'
          ? DECISION_COLORS.rejected
          : DECISION_COLORS.pending,
      stroke: ENTITY_TYPE_COLORS[node.entityType as string] || '#ccc',
      lineWidth: 2.5,
    },
  }))

  graph.edge(edge => ({
    style: {
      stroke: edge.decision === 'approved' ? DECISION_COLORS.approved : DECISION_COLORS.rejected,
      endArrow: {
        path: G6.Arrow.triangle(6, 8, 0),
        fill: edge.decision === 'approved' ? DECISION_COLORS.approved : DECISION_COLORS.rejected,
      },
    },
  }))

  graph.on('node:mouseenter', (e: any) => {
    const item = e.item
    graph!.setItemState(item, 'hover', true)
  })
  graph.on('node:mouseleave', (e: any) => {
    graph!.setItemState(e.item, 'hover', false)
  })

  graph.data(data)
  graph.render()
}

function updateGraph() {
  if (!graph) { createGraph(); return }
  const data = buildGraphData()
  graph.changeData(data)
  graph.fitView(40)
}

onMounted(() => {
  nextTick(() => createGraph())
})

watch(() => [props.nodes, props.edges], () => {
  nextTick(() => updateGraph())
}, { deep: true })

onUnmounted(() => {
  if (graph) { graph.destroy(); graph = null }
})
</script>

<style scoped>
.kg-container {
  position: relative;
  min-height: 400px;
  height: 500px;
}

.kg-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 400px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.kg-graph {
  width: 100%;
  height: 100%;
}

.kg-legend {
  position: absolute;
  bottom: 12px;
  left: 16px;
  display: flex;
  gap: 14px;
  background: rgba(255, 255, 255, 0.92);
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 12px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 5px;
}

.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.legend-dot.approved { background: #52c41a; }
.legend-dot.rejected { background: #f5222d; }
.legend-dot.pending { background: #bfbfbf; }
</style>
