<template>
  <div class="workbench" v-loading="loading">
    <el-page-header @back="$router.back()" content="审核工作台" style="margin-bottom:16px" />

    <!-- 顶部操作栏 -->
    <el-card shadow="never" style="margin-bottom:12px">
      <el-row align="middle" :gutter="12">
        <el-col :span="12">
          <el-input v-model="reviewer" placeholder="审核人姓名" style="width:200px" />
          <el-input v-model="batchNote" placeholder="批量审核备注（可选）" style="width:240px;margin-left:8px" />
        </el-col>
        <el-col :span="12" style="text-align:right">
          <el-button type="success" @click="approveAll">一键通过高置信度（≥0.8）</el-button>
          <el-button type="danger" @click="rejectAll">一键全部拒绝</el-button>
        </el-col>
      </el-row>
    </el-card>

    <el-row :gutter="16">
      <!-- 左侧：原文 -->
      <el-col :span="12">
        <el-card shadow="never" class="content-card">
          <template #header>原文全文</template>
          <div
            class="markdown-body"
            ref="contentRef"
            v-html="renderedContent"
          />
        </el-card>
      </el-col>

      <!-- 右侧：实体 + 关系 -->
      <el-col :span="12">
        <el-card shadow="never" class="content-card">
          <template #header>
            <el-tabs v-model="activeTab">
              <el-tab-pane :label="`实体（${entities.length}）`" name="entities" />
              <el-tab-pane :label="`关系（${relations.length}）`" name="relations" />
            </el-tabs>
          </template>

          <!-- 实体列表 -->
          <div v-if="activeTab === 'entities'" class="card-list">
            <div
              v-for="ent in entities"
              :key="ent.id"
              class="review-card"
              :class="{ 'card-approved': ent.review_status === 'approved', 'card-rejected': ent.review_status === 'rejected' }"
              @mouseenter="highlightText(ent.source_text)"
              @mouseleave="clearHighlight"
            >
              <div class="card-header">
                <el-tag size="small" type="primary">{{ ent.entity_type }}</el-tag>
                <span class="entity-name">{{ ent.entity_name }}</span>
                <el-tag size="small" :type="confidenceType(ent.confidence_score)">
                  {{ (ent.confidence_score * 100).toFixed(0) }}%
                </el-tag>
                <el-tag size="small" :type="disambigType(ent.dict_id)">
                  {{ disambigLabel(ent.dict_id) }}
                </el-tag>
              </div>
              <div class="source-text" v-if="ent.source_text">
                "{{ ent.source_text.slice(0, 80) }}{{ ent.source_text.length > 80 ? '...' : '' }}"
              </div>
              <el-collapse>
                <el-collapse-item title="字段详情">
                  <pre class="entity-data">{{ JSON.stringify(ent.entity_data, null, 2) }}</pre>
                </el-collapse-item>
              </el-collapse>
              <div class="card-actions" v-if="ent.review_status === 'pending'">
                <el-button size="small" type="success" @click="doApproveEntity(ent.id)">✅ 通过</el-button>
                <el-button size="small" type="danger" @click="doRejectEntity(ent.id)">❌ 拒绝</el-button>
              </div>
              <el-tag v-else size="small" :type="ent.review_status === 'approved' ? 'success' : 'danger'">
                {{ ent.review_status === 'approved' ? '已通过' : '已拒绝' }}
              </el-tag>
            </div>
            <el-empty v-if="!entities.length" description="暂无待审核实体" />
          </div>

          <!-- 关系列表 -->
          <div v-if="activeTab === 'relations'" class="card-list">
            <div
              v-for="rel in relations"
              :key="rel.id"
              class="review-card"
              :class="{ 'card-approved': rel.review_status === 'approved', 'card-rejected': rel.review_status === 'rejected' }"
              @mouseenter="highlightText(rel.source_text)"
              @mouseleave="clearHighlight"
            >
              <div class="card-header">
                <el-tag size="small" type="warning">{{ rel.relation_type }}</el-tag>
                <span class="entity-name">
                  实体 {{ rel.from_entity_id }} → 实体 {{ rel.to_entity_id }}
                </span>
                <el-tag size="small" :type="confidenceType(rel.confidence_score)">
                  {{ (rel.confidence_score * 100).toFixed(0) }}%
                </el-tag>
              </div>
              <div class="source-text" v-if="rel.source_text">
                "{{ rel.source_text.slice(0, 80) }}{{ rel.source_text.length > 80 ? '...' : '' }}"
              </div>
              <div class="card-actions" v-if="rel.review_status === 'pending'">
                <el-button size="small" type="success" @click="doApproveRelation(rel.id)">✅ 通过</el-button>
                <el-button size="small" type="danger" @click="doRejectRelation(rel.id)">❌ 拒绝</el-button>
              </div>
              <el-tag v-else size="small" :type="rel.review_status === 'approved' ? 'success' : 'danger'">
                {{ rel.review_status === 'approved' ? '已通过' : '已拒绝' }}
              </el-tag>
            </div>
            <el-empty v-if="!relations.length" description="暂无待审核关系" />
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { marked } from 'marked'
import { getDocument } from '@/api/documents'
import {
  getEntities, getRelations,
  approveEntity, rejectEntity,
  approveRelation, rejectRelation,
  type EntityItem, type RelationItem,
} from '@/api/review'

const route = useRoute()
const uuid = route.params.uuid as string

const loading = ref(false)
const rawContent = ref('')
const entities = ref<EntityItem[]>([])
const relations = ref<RelationItem[]>([])
const activeTab = ref('entities')
const reviewer = ref('审核员')
const batchNote = ref('')
const contentRef = ref<HTMLElement | null>(null)

const renderedContent = computed(() => {
  if (!rawContent.value) return '<p style="color:#999">暂无内容</p>'
  return marked(rawContent.value) as string
})

async function load() {
  loading.value = true
  try {
    const [doc, entRes, relRes] = await Promise.all([
      getDocument(uuid),
      getEntities({ source_doc_uuid: uuid, page_size: 200 } as any),
      getRelations({ source_doc_uuid: uuid, page_size: 200 } as any),
    ])
    rawContent.value = doc.raw_content
    entities.value = entRes.items
    relations.value = relRes.items
  } finally {
    loading.value = false
  }
}

function highlightText(text: string) {
  if (!text || !contentRef.value) return
  const el = contentRef.value
  const html = el.innerHTML
  const escaped = text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  el.innerHTML = html.replace(
    new RegExp(escaped, 'g'),
    `<mark style="background:#ffe58f">${text}</mark>`
  )
}

function clearHighlight() {
  if (!contentRef.value) return
  contentRef.value.innerHTML = renderedContent.value
}

async function doApproveEntity(id: number) {
  await approveEntity(id, reviewer.value)
  const ent = entities.value.find(e => e.id === id)
  if (ent) ent.review_status = 'approved'
}

async function doRejectEntity(id: number) {
  await rejectEntity(id, reviewer.value)
  const ent = entities.value.find(e => e.id === id)
  if (ent) ent.review_status = 'rejected'
}

async function doApproveRelation(id: number) {
  await approveRelation(id, reviewer.value)
  const rel = relations.value.find(r => r.id === id)
  if (rel) rel.review_status = 'approved'
}

async function doRejectRelation(id: number) {
  await rejectRelation(id, reviewer.value)
  const rel = relations.value.find(r => r.id === id)
  if (rel) rel.review_status = 'rejected'
}

async function approveAll() {
  const pending = entities.value.filter(e => e.review_status === 'pending' && e.confidence_score >= 0.8)
  await Promise.all(pending.map(e => approveEntity(e.id, reviewer.value, batchNote.value)))
  pending.forEach(e => { e.review_status = 'approved' })
  ElMessage.success(`已通过 ${pending.length} 个高置信度实体`)
}

async function rejectAll() {
  const pending = entities.value.filter(e => e.review_status === 'pending')
  await Promise.all(pending.map(e => rejectEntity(e.id, reviewer.value, batchNote.value)))
  pending.forEach(e => { e.review_status = 'rejected' })
  ElMessage.warning(`已拒绝 ${pending.length} 个实体`)
}

const confidenceType = (s: number) => s >= 0.8 ? 'success' : s >= 0.5 ? 'warning' : 'danger'
const disambigLabel = (dictId: number | null) =>
  dictId !== null ? '🟢 精确匹配' : '🔵 新实体'
const disambigType = (dictId: number | null) => dictId !== null ? 'success' : 'info'

onMounted(load)
</script>

<style scoped>
.workbench { height: 100%; }
.content-card { height: calc(100vh - 200px); display: flex; flex-direction: column; }
.content-card :deep(.el-card__body) { flex: 1; overflow: hidden; padding: 0; }
.markdown-body {
  height: 100%;
  overflow-y: auto;
  padding: 16px;
  font-size: 14px;
  line-height: 1.7;
}
.card-list { height: calc(100vh - 260px); overflow-y: auto; padding: 12px; }
.review-card {
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 12px;
  margin-bottom: 10px;
  transition: border-color 0.2s;
}
.review-card:hover { border-color: #409eff; }
.card-approved { border-color: #67c23a; background: #f0f9eb; }
.card-rejected { border-color: #f56c6c; background: #fef0f0; opacity: 0.7; }
.card-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.entity-name { font-weight: 600; font-size: 14px; }
.source-text { font-size: 12px; color: #909399; margin: 6px 0; font-style: italic; }
.entity-data { font-size: 11px; background: #f5f7fa; padding: 8px; border-radius: 4px; overflow-x: auto; }
.card-actions { margin-top: 8px; display: flex; gap: 8px; }
</style>
