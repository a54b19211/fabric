<template>
  <div class="workbench" v-loading="loading">
    <el-page-header @back="$router.back()" content="审核工作台" style="margin-bottom:16px" />

    <!-- 顶部操作栏 -->
    <el-card shadow="never" style="margin-bottom:12px">
      <div class="toolbar">
        <div class="toolbar-filters">
          <el-input v-model="reviewer" placeholder="审核人姓名" style="width:160px" />
          <el-input v-model="batchNote" placeholder="审核备注（可选）" style="width:200px" />
          <el-select v-model="reviewStatusFilter" style="width:130px" @change="load">
            <el-option label="待审核" value="pending" />
            <el-option label="自动通过" value="auto_approved" />
            <el-option label="已通过" value="approved" />
            <el-option label="已拒绝" value="rejected" />
          </el-select>
        </div>
        <div class="toolbar-actions">
          <el-button size="small" type="success" @click="approveAllCurrentPage">&#x2713; 通过高置信度(≥0.8)</el-button>
          <el-button size="small" type="danger" @click="rejectAllCurrentPage">&#x2717; 拒绝本页全部</el-button>
          <el-button size="small" type="success" @click="approveAllEntities">&#x2713; 通过所有实体</el-button>
          <el-button size="small" type="danger" @click="rejectAllEntities">&#x2717; 拒绝所有实体</el-button>
        </div>
      </div>
    </el-card>

    <el-row :gutter="16">
      <!-- 左侧：原文（60%） -->
      <el-col :span="14">
        <el-card shadow="never" class="content-card">
          <template #header>原文全文</template>
          <div
            class="markdown-body"
            ref="contentRef"
            v-html="renderedContent"
          />
        </el-card>
      </el-col>

      <!-- 右侧：实体 + 关系（40%） -->
      <el-col :span="10">
        <el-card shadow="never" class="content-card">
          <template #header>
            <el-tabs v-model="activeTab">
              <el-tab-pane :label="`实体（${entities.length}）`" name="entities" />
              <el-tab-pane :label="`关系（${relations.length}）`" name="relations" />
              <el-tab-pane :label="`合并队列（${mergeQueue.length}）`" name="merges" />
            </el-tabs>
            <div v-if="activeTab === 'relations'" class="relation-header-actions">
              <el-button size="small" type="success" @click="approveAllRelationsCurrentPage">&#x2713; 通过高置信度</el-button>
              <el-button size="small" type="danger" @click="rejectAllRelationsCurrentPage">&#x2717; 拒绝本页</el-button>
              <el-button size="small" type="success" @click="approveAllRelations">&#x2713; 通过所有</el-button>
              <el-button size="small" type="danger" @click="rejectAllRelations">&#x2717; 拒绝所有</el-button>
            </div>
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
                <el-tag size="small" :type="disambigType(ent)">
                  {{ disambigLabel(ent) }}
                </el-tag>
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
              <el-collapse>
                <el-collapse-item title="字段详情">
                  <pre class="entity-data">{{ JSON.stringify(ent.entity_data, null, 2) }}</pre>
                </el-collapse-item>
              </el-collapse>
              <div class="card-actions" v-if="ent.review_status === 'pending'">
                <el-button size="small" type="success" @click="doApproveEntity(ent.id)">&#x2713; 通过</el-button>
                <el-button v-if="ent.disambig?.status && ent.disambig.status !== 'new_entity'" size="small" type="primary" @click="doApproveEntityAsNew(ent.id)">新实体</el-button>
                <el-button size="small" type="danger" @click="doRejectEntity(ent.id)">&#x2717; 拒绝</el-button>
              </div>
              <el-tag v-else size="small" :type="ent.review_status === 'approved' ? 'success' : 'danger'">
                {{ ent.review_status === 'approved' ? '已通过' : '已拒绝' }}
              </el-tag>
            </div>
            <el-empty v-if="!entities.length" description="暂无待审核实体" />
            
            <!-- 实体分页 -->
            <div v-if="entities.length > 0" class="pagination-container">
              <el-pagination
                v-model:current-page="entityPagination.page"
                v-model:page-size="entityPagination.pageSize"
                :page-sizes="[10, 20, 50, 100]"
                :total="entityPagination.total"
                layout="total, sizes, prev, pager, next, jumper"
                @current-change="handleEntityPageChange"
                @size-change="handleEntityPageSizeChange"
              />
            </div>
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
                  {{ rel.from_entity_name || rel.from_entity_id || '?' }} → {{ rel.to_entity_name || rel.to_entity_id || '?' }}
                </span>
                <el-tag size="small" :type="confidenceType(rel.confidence_score)">
                  {{ (rel.confidence_score * 100).toFixed(0) }}%
                </el-tag>
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
            <el-empty v-if="!relations.length" description="暂无待审核关系" />

            <!-- 关系分页 -->
            <div v-if="relations.length > 0" class="pagination-container">
              <el-pagination
                v-model:current-page="relationPagination.page"
                v-model:page-size="relationPagination.pageSize"
                :page-sizes="[10, 20, 50, 100]"
                :total="relationPagination.total"
                layout="total, sizes, prev, pager, next, jumper"
                @current-change="handleRelationPageChange"
                @size-change="handleRelationPageSizeChange"
              />
            </div>
          </div>

          <!-- 合并队列 -->
          <div v-if="activeTab === 'merges'" class="card-list">
            <div v-for="group in mergeQueue" :key="group.dict_id" class="review-card">
              <div class="card-header">
                <el-tag size="small" type="primary">{{ group.entity_type }}</el-tag>
                <span class="entity-name">{{ group.entity_name }}</span>
                <span style="font-size:12px;color:#909399">dict_id={{ group.dict_id }}</span>
              </div>
              <div v-for="c in group.conflicts" :key="c.merge_id" style="margin-top:8px;padding:8px;background:#fafafa;border-radius:4px">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                  <el-tag size="small" type="danger">{{ c.field_name }}</el-tag>
                  <el-tag size="small" type="info">{{ c.source_site }}</el-tag>
                </div>
                <div class="merge-row">
                  <div class="merge-col">
                    <div class="merge-label">
                      现有值
                      <router-link
                        v-for="s in c.existing_sources"
                        :key="s.source_doc_uuid"
                        :to="`/review/${s.source_doc_uuid}`"
                        :title="s.source_doc_uuid + ' (' + s.source_site + ')'"
                        style="font-size:11px;color:#409eff;margin-left:4px"
                      >
                        {{ s.source_doc_uuid.slice(0, 8) }}...
                      </router-link>
                    </div>
                    <pre class="merge-value">{{ JSON.stringify(c.existing_value, null, 2) }}</pre>
                  </div>
                  <div class="merge-col">
                    <div class="merge-label">
                      新值
                      <router-link
                        v-for="s in c.new_sources"
                        :key="s.source_doc_uuid"
                        :to="`/review/${s.source_doc_uuid}`"
                        :title="s.source_doc_uuid + ' (' + s.source_site + ')'"
                        style="font-size:11px;color:#409eff;margin-left:4px"
                      >
                        {{ s.source_doc_uuid.slice(0, 8) }}...
                      </router-link>
                    </div>
                    <pre class="merge-value">{{ JSON.stringify(c.new_value, null, 2) }}</pre>
                  </div>
                </div>
                <div class="card-actions">
                  <el-button size="small" type="success" @click="doResolveMerge(c.merge_id, 'accept_new', group.dict_id)">采用新值</el-button>
                  <el-button size="small" type="warning" @click="doResolveMerge(c.merge_id, 'keep_existing', group.dict_id)">保留现有</el-button>
                </div>
              </div>
            </div>
            <el-empty v-if="!mergeQueue.length" description="暂无待处理的合并冲突" />
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { marked } from 'marked'
import { getDocument } from '@/api/documents'
import {
  getEntities, getRelations,
  approveEntity, rejectEntity,
  approveRelation, rejectRelation,
  batchApproveEntities, batchRejectEntities,
  batchApproveRelations, batchRejectRelations,
  getMergeQueue, resolveMerge,
  type EntityItem, type RelationItem, type MergeQueueGroup,
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
const reviewStatusFilter = ref('pending')
const contentRef = ref<HTMLElement | null>(null)
const selectedCandidate = reactive<Record<number, number>>({})

// 分页相关
const entityPagination = ref({
  page: 1,
  pageSize: 20,
  total: 0
})
const relationPagination = ref({
  page: 1,
  pageSize: 20,
  total: 0
})

const mergeQueue = ref<MergeQueueGroup[]>([])

const renderedContent = computed(() => {
  if (!rawContent.value) return '<p style="color:#999">暂无内容</p>'
  return marked(rawContent.value) as string
})

async function load() {
  loading.value = true
  try {
    const [doc, entRes, relRes] = await Promise.all([
      getDocument(uuid),
      getEntities({ 
        source_doc_uuid: uuid, 
        page: entityPagination.value.page,
        page_size: entityPagination.value.pageSize,
        review_status: reviewStatusFilter.value,
      }),
      getRelations({ 
        source_doc_uuid: uuid, 
        page: relationPagination.value.page,
        page_size: relationPagination.value.pageSize,
        review_status: reviewStatusFilter.value,
      }),
    ])
    rawContent.value = doc.raw_content
    entities.value = entRes.items
    for (const ent of entRes.items) {
      if (ent.disambig?.candidates?.length && !(ent.id in selectedCandidate)) {
        selectedCandidate[ent.id] = ent.disambig.candidates[0].dict_id
      }
    }
    entityPagination.value.total = entRes.total
    relations.value = relRes.items
    relationPagination.value.total = relRes.total
  } finally {
    loading.value = false
  }
}

// 实体分页变化
function handleEntityPageChange(page: number) {
  entityPagination.value.page = page
  load()
}

// 关系分页变化
function handleRelationPageChange(page: number) {
  relationPagination.value.page = page
  load()
}

// 每页显示数量变化
function handleEntityPageSizeChange(size: number) {
  entityPagination.value.pageSize = size
  entityPagination.value.page = 1
  load()
}

function handleRelationPageSizeChange(size: number) {
  relationPagination.value.pageSize = size
  relationPagination.value.page = 1
  load()
}

let originalHtml = ''

function highlightText(text: string) {
  if (!text || !contentRef.value) return
  // 保存原始 HTML（仅首次）
  if (!originalHtml) {
    originalHtml = contentRef.value.innerHTML
  }
  // 先恢复原始内容
  contentRef.value.innerHTML = originalHtml

  const escaped = text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const walker = document.createTreeWalker(
    contentRef.value,
    NodeFilter.SHOW_TEXT,
    null,
  )
  const textNodes: Text[] = []
  let node: Text | null
  while ((node = walker.nextNode() as Text | null)) {
    textNodes.push(node)
  }

  for (const tNode of textNodes) {
    const idx = tNode.data.indexOf(text)
    if (idx === -1) continue
    // 分割：前缀 + 匹配 + 后缀
    const before = tNode.data.slice(0, idx)
    const match = tNode.data.slice(idx, idx + text.length)
    const after = tNode.data.slice(idx + text.length)

    const mark = document.createElement('mark')
    mark.style.background = '#ffe58f'
    mark.style.padding = '1px 2px'
    mark.style.borderRadius = '2px'
    mark.textContent = match

    const parent = tNode.parentNode!
    if (before) parent.insertBefore(document.createTextNode(before), tNode)
    parent.insertBefore(mark, tNode)
    if (after) parent.insertBefore(document.createTextNode(after), tNode)
    parent.removeChild(tNode)

    // 只替换第一个匹配
    break
  }

  // 滚动到高亮位置
  const firstMark = contentRef.value.querySelector('mark')
  if (firstMark) {
    firstMark.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
}

function clearHighlight() {
  if (!contentRef.value || !originalHtml) return
  contentRef.value.innerHTML = originalHtml
  originalHtml = ''
}

async function doApproveEntity(id: number) {
  const dictId = selectedCandidate[id] ?? null
  await approveEntity(id, reviewer.value, '', false, dictId)
  const ent = entities.value.find(e => e.id === id)
  if (ent) ent.review_status = 'approved'
}

async function doApproveEntityAsNew(id: number) {
  await approveEntity(id, reviewer.value, '', true)
  const ent = entities.value.find(e => e.id === id)
  if (ent) {
    ent.review_status = 'approved'
    ent.dict_id = null  // 本地更新，下次刷新从 DB 拉
  }
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

async function approveAllCurrentPage() {
  const pending = entities.value.filter(e => e.review_status === 'pending' && e.confidence_score >= 0.8)
  if (pending.length === 0) {
    ElMessage.info('当前页没有高置信度待审核实体')
    return
  }
  await Promise.all(pending.map(e => approveEntity(e.id, reviewer.value, batchNote.value, false, selectedCandidate[e.id] ?? null)))
  pending.forEach(e => { e.review_status = 'approved' })
  ElMessage.success(`已通过本页 ${pending.length} 个高置信度实体`)
}

async function rejectAllCurrentPage() {
  const pending = entities.value.filter(e => e.review_status === 'pending')
  if (pending.length === 0) {
    ElMessage.info('当前页没有待审核实体')
    return
  }
  await Promise.all(pending.map(e => rejectEntity(e.id, reviewer.value, batchNote.value)))
  pending.forEach(e => { e.review_status = 'rejected' })
  ElMessage.warning(`已拒绝本页 ${pending.length} 个实体`)
}

async function approveAllEntities() {
  try {
    const result = await batchApproveEntities({
      source_doc_uuid: uuid,
      min_confidence: 0.8,
      reviewer: reviewer.value,
      note: batchNote.value
    })
    if (result.success) {
      ElMessage.success(result.message || '已通过所有高置信度实体')
      // 重新加载数据
      load()
    }
  } catch (error: any) {
    ElMessage.error(error.message || '批量通过失败')
  }
}

async function rejectAllEntities() {
  try {
    const result = await batchRejectEntities({
      source_doc_uuid: uuid,
      reviewer: reviewer.value,
      note: batchNote.value
    })
    if (result.success) {
      ElMessage.warning(result.message || '已拒绝所有实体')
      // 重新加载数据
      load()
    }
  } catch (error: any) {
    ElMessage.error(error.message || '批量拒绝失败')
  }
}

async function approveAllRelationsCurrentPage() {
  const pending = relations.value.filter(r => r.review_status === 'pending' && r.confidence_score >= 0.8)
  if (pending.length === 0) {
    ElMessage.info('当前页没有高置信度待审核关系')
    return
  }
  await Promise.all(pending.map(r => approveRelation(r.id, reviewer.value, batchNote.value)))
  pending.forEach(r => { r.review_status = 'approved' })
  ElMessage.success(`已通过本页 ${pending.length} 个高置信度关系`)
}

async function rejectAllRelationsCurrentPage() {
  const pending = relations.value.filter(r => r.review_status === 'pending')
  if (pending.length === 0) {
    ElMessage.info('当前页没有待审核关系')
    return
  }
  await Promise.all(pending.map(r => rejectRelation(r.id, reviewer.value, batchNote.value)))
  pending.forEach(r => { r.review_status = 'rejected' })
  ElMessage.warning(`已拒绝本页 ${pending.length} 个关系`)
}

async function approveAllRelations() {
  try {
    const result = await batchApproveRelations({
      source_doc_uuid: uuid,
      min_confidence: 0.8,
      reviewer: reviewer.value,
      note: batchNote.value
    })
    if (result.success) {
      ElMessage.success(result.message || '已通过所有高置信度关系')
      // 重新加载数据
      load()
    }
  } catch (error: any) {
    ElMessage.error(error.message || '批量通过关系失败')
  }
}

async function rejectAllRelations() {
  try {
    const result = await batchRejectRelations({
      source_doc_uuid: uuid,
      reviewer: reviewer.value,
      note: batchNote.value
    })
    if (result.success) {
      ElMessage.warning(result.message || '已拒绝所有关系')
      // 重新加载数据
      load()
    }
  } catch (error: any) {
    ElMessage.error(error.message || '批量拒绝关系失败')
  }
}

async function loadMergeQueue() {
  try {
    const res = await getMergeQueue(50)
    mergeQueue.value = res.groups
  } catch (e: any) {
    ElMessage.error('加载合并队列失败')
  }
}

async function doResolveMerge(mergeId: number, resolution: string, dictId: number) {
  try {
    await resolveMerge(mergeId, reviewer.value, resolution)
    ElMessage.success('冲突已解决')
    // Remove resolved conflict from local state
    for (const g of mergeQueue.value) {
      if (g.dict_id === dictId) {
        g.conflicts = g.conflicts.filter(c => c.merge_id !== mergeId)
        break
      }
    }
    mergeQueue.value = mergeQueue.value.filter(g => g.conflicts.length > 0)
  } catch (e: any) {
    ElMessage.error('解决冲突失败')
  }
}

const confidenceType = (s: number) => s >= 0.8 ? 'success' : s >= 0.5 ? 'warning' : 'danger'

const disambigLabel = (ent: EntityItem) => {
  const status = ent.disambig?.status || (ent.dict_id !== null ? 'auto_matched' : 'new_entity')
  const map: Record<string, string> = {
    auto_matched: '🟢 自动匹配',
    candidate_matched: '🟡 候选匹配',
    new_entity: '🔵 新实体',
    ambiguous: '🟠 模糊待确认',
  }
  return map[status] || '⚪ 未知'
}

const disambigType = (ent: EntityItem) => {
  const status = ent.disambig?.status || (ent.dict_id !== null ? 'auto_matched' : 'new_entity')
  const map: Record<string, string> = {
    auto_matched: 'success',
    candidate_matched: 'warning',
    new_entity: 'info',
    ambiguous: 'danger',
  }
  return map[status] || 'info'
}

onMounted(load)

watch(activeTab, (tab) => {
  if (tab === 'merges') loadMergeQueue()
})
</script>

<style scoped>
.workbench { height: 100%; }

/* ── 顶部工具栏自适应 ── */
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.toolbar-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.toolbar-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

/* ── 关系 tab 头部按钮自适应 ── */
.relation-header-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-left: auto;
}

/* ── 小屏幕适配 ── */
@media (max-width: 1200px) {
  .toolbar {
    flex-direction: column;
    align-items: flex-start;
  }
  .toolbar-actions {
    margin-left: 0;
    width: 100%;
  }
  .relation-header-actions {
    position: static;
    margin-top: 8px;
    margin-left: 0;
  }
}
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
.card-approved { border-color: #0e9e8e; background: #e8f8f6; }
.card-rejected { border-color: #c0392b; background: #fdf0ee; opacity: 0.7; }
.card-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.entity-name { font-weight: 600; font-size: 14px; }
.source-text { font-size: 12px; color: #909399; margin: 6px 0; font-style: italic; }
.reason-text { font-size: 12px; color: #606266; margin: 4px 0; padding: 4px 8px; background: #f0f9eb; border-radius: 4px; }
.candidates-section { margin-top: 6px; padding: 8px; background: #fdf6ec; border-radius: 4px; }
.candidates-label { font-size: 12px; color: #909399; margin-bottom: 4px; }
.candidate-item { display: flex; align-items: center; gap: 6px; margin: 2px 0; font-size: 13px; }
.candidate-name { margin-right: 6px; font-weight: 500; }
.candidates-radio-group { width: 100%; }
.candidates-radio-group :deep(.el-radio) { margin-right: 0; display: flex; align-items: center; }
.entity-data { font-size: 11px; background: #f5f7fa; padding: 8px; border-radius: 4px; overflow-x: auto; }
.card-actions { margin-top: 8px; display: flex; gap: 8px; }
.pagination-container {
  margin-top: 16px;
  padding: 12px;
  border-top: 1px solid #e4e7ed;
  background: #fafafa;
  display: flex;
  justify-content: center;
}
.merge-row { display: flex; gap: 12px; margin-top: 8px; }
.merge-col { flex: 1; min-width: 0; }
.merge-label { font-size: 12px; color: #909399; margin-bottom: 4px; }
.merge-value { font-size: 11px; background: #f5f7fa; padding: 6px; border-radius: 4px; overflow-x: auto; max-height: 120px; }
</style>
