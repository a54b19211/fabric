<template>
  <div class="workbench" v-loading="loading">
    <el-page-header @back="$router.back()" content="批量审核工作台" style="margin-bottom:16px" />

    <el-card shadow="never" style="margin-bottom:12px">
      <div class="toolbar">
        <div class="toolbar-left">
          <span class="toolbar-label">审核人：</span>
          <el-input v-model="reviewer" placeholder="审核人姓名" style="width:140px" />
          <span class="toolbar-label">备注：</span>
          <el-input v-model="batchNote" placeholder="审核备注（可选）" style="width:180px" />
          <el-select v-model="reviewStatusFilter" style="width:130px" @change="loadPage">
            <el-option label="待审核" value="pending" />
            <el-option label="自动通过" value="auto_approved" />
            <el-option label="已通过" value="approved" />
            <el-option label="已拒绝" value="rejected" />
          </el-select>
          <span class="toolbar-label">每页篇数：</span>
          <el-select v-model="pageSize" style="width:100px" @change="onPageSizeChange">
            <el-option :value="1" label="1 篇" />
            <el-option :value="2" label="2 篇" />
            <el-option :value="3" label="3 篇" />
            <el-option :value="5" label="5 篇" />
            <el-option :value="10" label="10 篇" />
          </el-select>
        </div>
        <div class="toolbar-right">
          <span class="page-info">第 {{ currentPage }} / {{ totalPages }} 页（共 {{ totalDocs }} 篇）</span>
          <el-button-group>
            <el-button size="small" :disabled="currentPage <= 1" @click="goPage(currentPage - 1)">
              <el-icon><ArrowLeft /></el-icon>上一页
            </el-button>
            <el-button size="small" :disabled="currentPage >= totalPages" @click="goPage(currentPage + 1)">
              下一页<el-icon><ArrowRight /></el-icon>
            </el-button>
          </el-button-group>
        </div>
      </div>
      <div class="toolbar-actions-row">
        <el-button size="small" type="success" @click="approveHighConfEntities">&#x2713; 通过高置信度(≥0.8)</el-button>
        <el-button size="small" type="danger" @click="rejectCurrentPageEntities">&#x2717; 拒绝本页全部</el-button>
        <el-button size="small" type="success" @click="approveAllEntitiesForAllDocs">&#x2713; 通过所有实体</el-button>
        <el-button size="small" type="danger" @click="rejectAllEntitiesForAllDocs">&#x2717; 拒绝所有实体</el-button>
      </div>
    </el-card>

    <div class="content-row">
      <div class="content-left">
        <el-card shadow="never" class="content-card">
          <template #header>
            <span>原文全文</span>
            <span class="header-sub">（{{ currentDocs.length }} 篇）</span>
          </template>
          <div class="markdown-body" ref="contentRef">
            <div v-for="(doc, idx) in currentDocs" :key="doc.uuid" class="doc-section">
              <div class="doc-section-header" :id="`doc-${doc.uuid}`">
                <el-tag size="small" type="success">{{ doc.source_site }}</el-tag>
                <span class="doc-section-title">{{ doc.title || '（无标题）' }}</span>
                <span class="doc-index">第 {{ (currentPage - 1) * pageSize + idx + 1 }} 篇</span>
              </div>
              <div class="doc-content" v-html="doc.rendered" :data-doc-uuid="doc.uuid" />
              <el-divider v-if="idx < currentDocs.length - 1" />
            </div>
          </div>
        </el-card>
      </div>

      <div class="content-right">
        <el-card shadow="never" class="content-card">
          <template #header>
            <el-tabs v-model="activeTab">
              <el-tab-pane :label="`实体（${allEntities.length}）`" name="entities" />
              <el-tab-pane :label="`关系（${allRelations.length}）`" name="relations" />
            </el-tabs>
          </template>

          <div v-if="activeTab === 'entities'" class="card-list">
            <div v-for="group in entityGroups" :key="group.uuid" class="entity-group">
              <div class="entity-group-title">
                <el-tag size="small">{{ group.title }}</el-tag>
                <span class="entity-count">{{ group.items.length }} 个实体</span>
              </div>
              <div
                v-for="ent in group.items"
                :key="ent.id"
                class="review-card"
                :class="{ 'card-approved': ent.review_status === 'approved', 'card-rejected': ent.review_status === 'rejected' }"
              >
                <div class="card-header">
                  <span class="badge badge-entity-type">{{ ent.entity_type }}</span>
                  <span class="entity-name">{{ ent.entity_name }}</span>
                  <span :class="['badge badge-confidence', confidenceClass(ent.confidence_score)]">
                    {{ (ent.confidence_score * 100).toFixed(0) }}%
                  </span>
                  <span :class="['badge badge-disambig', disambigClass(ent)]">
                    {{ disambigLabel(ent) }}
                  </span>
                </div>
                <div class="source-text" v-if="ent.source_text">
                  "{{ ent.source_text.slice(0, 80) }}{{ ent.source_text.length > 80 ? '...' : '' }}"
                </div>
                <div class="reason-text" v-if="ent.confidence_reason">
                  匹配原因：{{ ent.confidence_reason }}
                </div>
                <div v-if="ent.disambig?.candidates?.length && ent.disambig.status !== 'new_entity'" class="candidates-section">
                  <div class="candidates-label">选择匹配候选：</div>
                  <el-radio-group v-model="selectedCandidate[ent.id]" class="candidates-radio-group">
                    <div v-for="c in ent.disambig.candidates" :key="c.dict_id" class="candidate-item">
                      <el-radio :value="c.dict_id">
                        <span class="candidate-name">{{ c.name }}</span>
                        <el-tag size="small" type="warning">{{ (c.similarity_score * 100).toFixed(0) }}%</el-tag>
                        <el-tag size="small" type="info">{{ c.match_type }}</el-tag>
                      </el-radio>
                    </div>
                  </el-radio-group>
                </div>
                <details class="field-details">
                  <summary class="field-details-summary">字段详情</summary>
                  <pre class="entity-data">{{ JSON.stringify(ent.entity_data, null, 2) }}</pre>
                </details>
                <div class="card-actions" v-if="ent.review_status === 'pending'">
                  <el-button size="small" type="success" @click="doApproveEntity(ent.id)">&#x2713; 通过</el-button>
                  <el-button v-if="ent.disambig?.status && ent.disambig.status !== 'new_entity'" size="small" type="primary" @click="doApproveEntityAsNew(ent.id)">新实体</el-button>
                  <el-button size="small" type="danger" @click="doRejectEntity(ent.id)">&#x2717; 拒绝</el-button>
                </div>
                <span v-else :class="['badge', ent.review_status === 'approved' ? 'badge-emerald' : 'badge-red']" style="margin-top:6px">
                  {{ ent.review_status === 'approved' ? '已通过' : '已拒绝' }}
                </span>
              </div>
            </div>
            <el-empty v-if="!allEntities.length" description="暂无待审核实体" />
          </div>

          <div v-if="activeTab === 'relations'" class="card-list">
            <div class="relation-header-actions">
              <el-button size="small" type="success" @click="approveHighConfRelations">&#x2713; 通过高置信度</el-button>
              <el-button size="small" type="danger" @click="rejectCurrentPageRelations">&#x2717; 拒绝本页</el-button>
              <el-button size="small" type="success" @click="approveAllRelationsForAllDocs">&#x2713; 通过所有</el-button>
              <el-button size="small" type="danger" @click="rejectAllRelationsForAllDocs">&#x2717; 拒绝所有</el-button>
            </div>
            <div v-for="group in relationGroups" :key="group.uuid" class="entity-group">
              <div class="entity-group-title">
                <el-tag size="small">{{ group.title }}</el-tag>
                <span class="entity-count">{{ group.items.length }} 个关系</span>
              </div>
              <div
                v-for="rel in group.items"
                :key="rel.id"
                class="review-card"
                :class="{ 'card-approved': rel.review_status === 'approved', 'card-rejected': rel.review_status === 'rejected' }"
              >
                <div class="card-header">
                  <span class="badge badge-relation-type">{{ rel.relation_type }}</span>
                  <span class="entity-name">
                    {{ rel.from_entity_name || rel.from_entity_id || '?' }} → {{ rel.to_entity_name || rel.to_entity_id || '?' }}
                  </span>
                  <span :class="['badge badge-confidence', confidenceClass(rel.confidence_score)]">
                    {{ (rel.confidence_score * 100).toFixed(0) }}%
                  </span>
                </div>
                <div class="source-text" v-if="rel.source_text">
                  "{{ rel.source_text.slice(0, 80) }}{{ rel.source_text.length > 80 ? '...' : '' }}"
                </div>
                <div class="card-actions" v-if="rel.review_status === 'pending'">
                  <el-button size="small" type="success" @click="doApproveRelation(rel.id)">&#x2713; 通过</el-button>
                  <el-button size="small" type="danger" @click="doRejectRelation(rel.id)">&#x2717; 拒绝</el-button>
                </div>
                <el-tag v-else size="small" :type="rel.review_status === 'approved' ? 'success' : 'danger'">
                  {{ rel.review_status === 'approved' ? '已通过' : '已拒绝' }}
                </el-tag>
              </div>
            </div>
            <el-empty v-if="!allRelations.length" description="暂无待审核关系" />
          </div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, ArrowRight } from '@element-plus/icons-vue'
import { marked } from 'marked'
import { getDocument } from '@/api/documents'
import {
  getEntities, getRelations,
  approveEntity, rejectEntity,
  approveRelation, rejectRelation,
  batchApproveEntities, batchRejectEntities,
  batchApproveRelations, batchRejectRelations,
  type EntityItem, type RelationItem,
} from '@/api/review'

const route = useRoute()
const router = useRouter()

const allUuids: string[] = (route.query.uuids as string || '').split(',').filter(Boolean)
const totalDocs = allUuids.length

const loading = ref(false)
const reviewer = ref('审核员')
const batchNote = ref('')
const reviewStatusFilter = ref('pending')
const activeTab = ref('entities')
const selectedCandidate = reactive<Record<number, number>>({})

const pageSize = ref(3)
const currentPage = ref(1)
const totalPages = computed(() => Math.max(1, Math.ceil(totalDocs / pageSize.value)))

interface DocView { uuid: string; title: string; source_site: string; rendered: string }
const currentDocs = ref<DocView[]>([])
const allEntities = ref<EntityItem[]>([])
const allRelations = ref<RelationItem[]>([])

const entityGroups = computed(() => {
  const groups: { uuid: string; title: string; items: EntityItem[] }[] = []
  for (const doc of currentDocs.value) {
    const items = allEntities.value.filter(e => e.source_doc_uuid === doc.uuid)
    groups.push({ uuid: doc.uuid, title: doc.title || '（无标题）', items })
  }
  return groups
})

const relationGroups = computed(() => {
  const groups: { uuid: string; title: string; items: RelationItem[] }[] = []
  for (const doc of currentDocs.value) {
    const items = allRelations.value.filter(r => r.source_doc_uuid === doc.uuid)
    groups.push({ uuid: doc.uuid, title: doc.title || '（无标题）', items })
  }
  return groups
})

async function loadPage() {
  loading.value = true
  try {
    const start = (currentPage.value - 1) * pageSize.value
    const end = start + pageSize.value
    const uuids = allUuids.slice(start, end)

    const docResults = await Promise.all(
      uuids.map(async (uuid) => {
        try {
          const doc = await getDocument(uuid)
          return {
            uuid,
            title: doc.title,
            source_site: doc.source_site,
            rendered: doc.raw_content ? (marked(doc.raw_content) as string) : '<p style="color:#999">暂无内容</p>',
          }
        } catch {
          return { uuid, title: '加载失败', source_site: '', rendered: '<p style="color:var(--dp-danger)">文档加载失败</p>' }
        }
      })
    )
    currentDocs.value = docResults

    const entResults = await Promise.all(
      uuids.map(uuid =>
        getEntities({ source_doc_uuid: uuid, page_size: 200, review_status: reviewStatusFilter.value })
          .then(res => res.items).catch(() => [] as EntityItem[])
      )
    )
    allEntities.value = entResults.flat()
    for (const ent of allEntities.value) {
      if (ent.disambig?.candidates?.length && !(ent.id in selectedCandidate)) {
        selectedCandidate[ent.id] = ent.disambig.candidates[0].dict_id
      }
    }

    const relResults = await Promise.all(
      uuids.map(uuid =>
        getRelations({ source_doc_uuid: uuid, page_size: 200, review_status: reviewStatusFilter.value })
          .then(res => res.items).catch(() => [] as RelationItem[])
      )
    )
    allRelations.value = relResults.flat()

    router.replace({
      query: {
        uuids: allUuids.join(','),
        page: String(currentPage.value),
        pageSize: String(pageSize.value),
      }
    })
  } finally { loading.value = false }
}

function goPage(p: number) {
  if (p < 1 || p > totalPages.value) return
  currentPage.value = p
  loadPage()
}

function onPageSizeChange() { currentPage.value = 1; loadPage() }

async function doApproveEntity(id: number) {
  const dictId = selectedCandidate[id] ?? null
  await approveEntity(id, reviewer.value, '', false, dictId)
  const ent = allEntities.value.find(e => e.id === id)
  if (ent) ent.review_status = 'approved'
}

async function doApproveEntityAsNew(id: number) {
  await approveEntity(id, reviewer.value, '', true)
  const ent = allEntities.value.find(e => e.id === id)
  if (ent) {
    ent.review_status = 'approved'
    ent.dict_id = null
  }
}

async function doRejectEntity(id: number) {
  await rejectEntity(id, reviewer.value)
  const ent = allEntities.value.find(e => e.id === id)
  if (ent) ent.review_status = 'rejected'
}

async function doApproveRelation(id: number) {
  await approveRelation(id, reviewer.value)
  const rel = allRelations.value.find(r => r.id === id)
  if (rel) rel.review_status = 'approved'
}

async function doRejectRelation(id: number) {
  await rejectRelation(id, reviewer.value)
  const rel = allRelations.value.find(r => r.id === id)
  if (rel) rel.review_status = 'rejected'
}

async function approveHighConfEntities() {
  const pending = allEntities.value.filter(e => e.review_status === 'pending' && e.confidence_score >= 0.8)
  if (pending.length === 0) { ElMessage.info('当前页没有高置信度待审核实体'); return }
  await Promise.all(pending.map(e => approveEntity(e.id, reviewer.value, batchNote.value, false, selectedCandidate[e.id] ?? null)))
  pending.forEach(e => { e.review_status = 'approved' })
  ElMessage.success(`已通过本页 ${pending.length} 个高置信度实体`)
}

async function rejectCurrentPageEntities() {
  const pending = allEntities.value.filter(e => e.review_status === 'pending')
  if (pending.length === 0) { ElMessage.info('当前页没有待审核实体'); return }
  await Promise.all(pending.map(e => rejectEntity(e.id, reviewer.value, batchNote.value)))
  pending.forEach(e => { e.review_status = 'rejected' })
  ElMessage.warning(`已拒绝本页 ${pending.length} 个实体`)
}

async function approveAllEntitiesForAllDocs() {
  try {
    await Promise.all(
      currentDocs.value.map(doc =>
        batchApproveEntities({ source_doc_uuid: doc.uuid, min_confidence: 0, reviewer: reviewer.value, note: batchNote.value })
      )
    )
    ElMessage.success(`已通过所有文档的实体`)
    loadPage()
  } catch (e: any) { ElMessage.error(e.message || '批量通过失败') }
}

async function rejectAllEntitiesForAllDocs() {
  try {
    await Promise.all(
      currentDocs.value.map(doc =>
        batchRejectEntities({ source_doc_uuid: doc.uuid, reviewer: reviewer.value, note: batchNote.value })
      )
    )
    ElMessage.warning(`已拒绝所有文档的实体`)
    loadPage()
  } catch (e: any) { ElMessage.error(e.message || '批量拒绝失败') }
}

async function approveHighConfRelations() {
  const pending = allRelations.value.filter(r => r.review_status === 'pending' && r.confidence_score >= 0.8)
  if (pending.length === 0) { ElMessage.info('当前页没有高置信度待审核关系'); return }
  await Promise.all(pending.map(r => approveRelation(r.id, reviewer.value, batchNote.value)))
  pending.forEach(r => { r.review_status = 'approved' })
  ElMessage.success(`已通过本页 ${pending.length} 个高置信度关系`)
}

async function rejectCurrentPageRelations() {
  const pending = allRelations.value.filter(r => r.review_status === 'pending')
  if (pending.length === 0) { ElMessage.info('当前页没有待审核关系'); return }
  await Promise.all(pending.map(r => rejectRelation(r.id, reviewer.value, batchNote.value)))
  pending.forEach(r => { r.review_status = 'rejected' })
  ElMessage.warning(`已拒绝本页 ${pending.length} 个关系`)
}

async function approveAllRelationsForAllDocs() {
  try {
    await Promise.all(
      currentDocs.value.map(doc =>
        batchApproveRelations({ source_doc_uuid: doc.uuid, min_confidence: 0, reviewer: reviewer.value, note: batchNote.value })
      )
    )
    ElMessage.success(`已通过所有文档的关系`)
    loadPage()
  } catch (e: any) { ElMessage.error(e.message || '批量通过关系失败') }
}

async function rejectAllRelationsForAllDocs() {
  try {
    await Promise.all(
      currentDocs.value.map(doc =>
        batchRejectRelations({ source_doc_uuid: doc.uuid, reviewer: reviewer.value, note: batchNote.value })
      )
    )
    ElMessage.warning(`已拒绝所有文档的关系`)
    loadPage()
  } catch (e: any) { ElMessage.error(e.message || '批量拒绝关系失败') }
}

const confidenceClass = (s: number) => s >= 0.8 ? 'badge-emerald' : s >= 0.5 ? 'badge-amber' : 'badge-red'

const disambigLabel = (ent: EntityItem) => {
  const status = ent.disambig?.status || (ent.dict_id !== null ? 'auto_matched' : 'new_entity')
  const map: Record<string, string> = {
    auto_matched: '🟢 自动匹配', candidate_matched: '🟡 候选匹配',
    new_entity: '🔵 新实体', ambiguous: '🟠 模糊待确认',
  }
  return map[status] || '⚪ 未知'
}

const disambigClass = (ent: EntityItem) => {
  const status = ent.disambig?.status || (ent.dict_id !== null ? 'auto_matched' : 'new_entity')
  const map: Record<string, string> = { auto_matched: 'badge-emerald', candidate_matched: 'badge-amber', new_entity: 'badge-blue', ambiguous: 'badge-orange' }
  return map[status] || 'badge-gray'
}

onMounted(() => {
  currentPage.value = Number(route.query.page) || 1
  pageSize.value = Number(route.query.pageSize) || 3
  loadPage()
})
</script>

<style scoped>
.workbench { height: 100%; display: flex; flex-direction: column; }

.content-row { flex: 1; display: flex; gap: 16px; min-height: 0; }
.content-left { flex: 14; min-width: 0; display: flex; }
.content-right { flex: 10; min-width: 0; display: flex; }

.content-card { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.content-card :deep(.el-card__body) { flex: 1; overflow: hidden; padding: 0; min-height: 0; display: flex; flex-direction: column; }

.markdown-body { flex: 1; overflow-y: auto; padding: 16px; font-size: 14px; line-height: 1.7; min-height: 0; }

.toolbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.toolbar-left { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.toolbar-right { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.toolbar-actions-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--dp-border); }
.relation-header-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 10px; padding: 8px 12px; background: var(--dp-muted-bg-secondary); border-radius: var(--dp-radius-xs); }
.toolbar-label { font-size: 13px; color: var(--dp-text-regular); white-space: nowrap; }
.page-info { font-size: 13px; color: var(--dp-text-regular); white-space: nowrap; }
.header-sub { font-size: 12px; color: var(--dp-text-secondary); margin-left: 8px; }

.card-list { flex: 1; overflow-y: auto; padding: 12px; min-height: 0; }

.entity-group { margin-bottom: 16px; }
.entity-group-title { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--dp-border); margin-bottom: 8px; position: sticky; top: 0; background: var(--dp-card); z-index: 1; }
.entity-count { font-size: 12px; color: var(--dp-text-secondary); }

.doc-section { margin-bottom: 0; }
.doc-section-header { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: var(--dp-success-light); border-radius: var(--dp-radius-sm); margin-bottom: 12px; position: sticky; top: 0; z-index: 2; }
.doc-section-title { font-weight: 600; font-size: 14px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-index { font-size: 12px; color: var(--dp-text-secondary); white-space: nowrap; }
.doc-content { height: auto; overflow: visible; padding: 0; }

.review-card { border: 1px solid var(--dp-border); border-radius: var(--dp-radius-sm); padding: 12px; margin-bottom: 10px; transition: border-color 0.2s; }
.review-card:hover { border-color: var(--dp-primary); }
.card-approved { border-color: var(--dp-primary); background: var(--el-color-success-light-9); }
.card-rejected { border-color: var(--dp-danger); background: var(--el-color-danger-light-9); }
.card-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.entity-name { font-weight: 600; font-size: 14px; }
.source-text { font-size: 12px; color: var(--dp-text-secondary); margin: 6px 0; font-style: italic; }
.reason-text { font-size: 12px; color: var(--dp-text-regular); margin: 4px 0; padding: 4px 8px; background: var(--dp-success-light); border-radius: var(--dp-radius-xs); }
.entity-data { font-size: 11px; background: var(--dp-muted-bg); padding: 8px; border-radius: var(--dp-radius-xs); overflow-x: auto; }
.candidates-section { margin-top: 6px; padding: 8px; background: var(--dp-warning-light); border-radius: var(--dp-radius-xs); }
.candidates-label { font-size: 12px; color: var(--dp-text-secondary); margin-bottom: 4px; }
.candidate-item { display: flex; align-items: center; gap: 6px; margin: 2px 0; font-size: 13px; }
.candidate-name { margin-right: 6px; font-weight: 500; }
.candidates-radio-group { width: 100%; }
.candidates-radio-group :deep(.el-radio) { margin-right: 0; display: flex; align-items: center; }
.card-actions { margin-top: 8px; display: flex; gap: 8px; }

.badge {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  font-size: 12px;
  font-weight: 500;
  border-radius: var(--dp-radius-xs);
  border: 1px solid transparent;
}
.badge-entity-type { border-color: rgba(40,170,134,0.3); background: rgba(40,170,134,0.05); color: var(--dp-primary); }
.badge-relation-type { border-color: rgba(240,138,63,0.3); background: rgba(240,138,63,0.05); color: var(--dp-warning); }
.badge-confidence { font-size: 11px; }
.badge-emerald { border-color: var(--dp-success-dark); background: #ecfdf5; color: #065f46; }
.badge-amber { border-color: var(--dp-warning-dark); background: #fffbeb; color: #92400e; }
.badge-red { border-color: var(--dp-danger-dark); background: #fef2f2; color: #991b1b; }
.badge-blue { border-color: #3b82f6; background: #eff6ff; color: #1e40af; }
.badge-orange { border-color: #f97316; background: #fff7ed; color: #9a3412; }
.badge-gray { border-color: var(--dp-border); background: var(--dp-muted-bg); color: var(--dp-text-secondary); }

.field-details { margin-top: 6px; }
.field-details-summary {
  font-size: 12px;
  color: var(--dp-text-secondary);
  cursor: pointer;
  user-select: none;
}
.field-details-summary:hover { color: var(--dp-foreground); }
</style>
