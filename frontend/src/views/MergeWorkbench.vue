<template>
  <div class="merge-workbench">
    <!-- 顶部标题栏 -->
    <el-card shadow="never" class="header-bar">
      <div class="header-row">
        <h3>实体合并工作台</h3>
        <el-button @click="$router.push('/graph-review')">← 返回图谱审核</el-button>
      </div>
    </el-card>

    <el-row :gutter="12" class="main-row">
      <!-- 左侧：搜索 & 选择实体 -->
      <el-col :span="6">
        <el-card shadow="never" class="select-card">
          <template #header>搜索实体</template>
          <div class="search-section">
            <el-input
              v-model="searchQuery"
              placeholder="输入实体名搜索..."
              clearable
              @keyup.enter="onSearch"
              @clear="searchResults = []"
            >
              <template #append>
                <el-button @click="onSearch" :loading="searching">搜索</el-button>
              </template>
            </el-input>
          </div>

          <!-- 搜索结果 -->
          <div v-if="searchResults.length > 0" class="search-results">
            <div
              v-for="item in searchResults"
              :key="item.dict_id"
              class="search-item"
              :class="{ selected: selectedIds.has(item.dict_id) }"
              @click="toggleSelect(item)"
            >
              <el-checkbox :model-value="selectedIds.has(item.dict_id)" />
              <div class="item-info">
                <span class="item-name">{{ item.entity_name }}</span>
                <span class="item-meta">
                  <el-tag size="small" type="info">{{ item.entity_type }}</el-tag>
                  <span v-if="item.canonical_source">来源: {{ item.canonical_source }}</span>
                  <span>{{ item.extracted_entities_count }} 条抽取</span>
                </span>
                <span v-if="item.aliases.length > 0" class="item-aliases">
                  别名: {{ item.aliases.slice(0, 5).join(', ') }}
                  <template v-if="item.aliases.length > 5">等{{ item.aliases.length }}个</template>
                </span>
              </div>
            </div>
          </div>
          <el-empty v-else-if="searched && !searching" description="未找到匹配实体" :image-size="60" />

          <el-divider />

          <!-- 已选实体 -->
          <div class="selected-section">
            <h4>已选实体 ({{ selectedList.length }})</h4>
            <div v-if="selectedList.length === 0" class="hint">搜索并勾选 2 个以上实体进行对比</div>
            <div v-for="ent in selectedList" :key="ent.dict_id" class="selected-item">
              <div class="selected-info">
                <el-radio
                  :model-value="keeperDictId"
                  :value="ent.dict_id"
                  @change="keeperDictId = ent.dict_id"
                >
                  <strong>{{ ent.entity_name }}</strong>
                </el-radio>
                <span class="selected-meta">
                  <el-tag size="small" type="info">{{ ent.entity_type }}</el-tag>
                  dict_id={{ ent.dict_id }}
                </span>
              </div>
              <el-button size="small" text type="danger" @click="removeSelected(ent.dict_id)">移除</el-button>
            </div>
            <el-button
              v-if="selectedList.length >= 2"
              type="primary"
              style="width:100%; margin-top:12px"
              @click="onCompare"
              :loading="comparing"
            >
              对比选中实体
            </el-button>
          </div>
        </el-card>
      </el-col>

      <!-- 中间：字段对比 -->
      <el-col :span="12">
        <el-card shadow="never" class="diff-card" v-loading="comparing">
          <template #header>字段对比</template>
          <div v-if="!compareResult" class="placeholder">
            <el-empty description="请先搜索并选择实体，然后点击「对比选中实体」" :image-size="80" />
          </div>
          <div v-else class="diff-table-wrap">
            <el-table :data="compareResult.field_diff" border stripe size="small" max-height="calc(100vh - 320px)">
              <el-table-column prop="field" label="字段" width="160" fixed>
                <template #default="{ row }">
                  <span :class="{ 'conflict-field': row.conflict }">{{ row.field }}</span>
                  <el-tag v-if="row.conflict" size="small" type="warning" style="margin-left:6px">冲突</el-tag>
                </template>
              </el-table-column>
              <el-table-column
                v-for="ent in compareResult.entities"
                :key="ent.dict_id"
                :label="`${ent.entity_name} (${ent.dict_id})`"
                min-width="180"
              >
                <template #default="{ row }">
                  <div
                    class="cell-value"
                    :class="{
                      'cell-selected': row.conflict && fieldDecisions[row.field]?.picked_dict_id === ent.dict_id,
                      'cell-keeper': ent.dict_id === keeperDictId && !fieldDecisions[row.field]
                    }"
                    @click="row.conflict && pickField(row.field, ent.dict_id)"
                  >
                    <span v-if="row.conflict && fieldDecisions[row.field]?.picked_dict_id === ent.dict_id" class="check-mark">&#x2713;</span>
                    {{ formatValue(row.values && row.values[String(ent.dict_id)]) }}
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>

      <!-- 右侧：操作面板 -->
      <el-col :span="6">
        <el-card shadow="never" class="action-card">
          <template #header>操作面板</template>
          <div v-if="!compareResult" class="placeholder">
            <el-empty description="等待对比结果" :image-size="60" />
          </div>
          <div v-else class="action-content">
            <!-- Keeper 确认 -->
            <div class="action-section">
              <h4>保留实体 (Keeper)</h4>
              <el-select v-model="keeperDictId" style="width:100%">
                <el-option
                  v-for="ent in compareResult.entities"
                  :key="ent.dict_id"
                  :label="`${ent.entity_name} (dict_id=${ent.dict_id})`"
                  :value="ent.dict_id"
                />
              </el-select>
              <div class="hint">合并后保留此实体，其他实体会被吸收</div>
            </div>

            <el-divider />

            <!-- 冲突决策摘要 -->
            <div class="action-section">
              <h4>冲突字段 ({{ conflictCount }})</h4>
              <div v-if="conflictCount === 0" class="hint success">所有字段一致，无需决策</div>
              <div v-else v-for="fd in conflictFields" :key="fd.field" class="conflict-item">
                <span class="conflict-label">{{ fd.field }}</span>
                <el-select
                  :model-value="fieldDecisions[fd.field]?.picked_dict_id"
                  @change="(val: number) => pickField(fd.field, val)"
                  size="small"
                  style="width:100%"
                >
                  <el-option
                    v-for="ent in compareResult.entities"
                    :key="ent.dict_id"
                    :label="`${ent.entity_name}: ${formatValue(fd.values[String(ent.dict_id)])}`"
                    :value="ent.dict_id"
                  />
                </el-select>
              </div>
            </div>

            <el-divider />

            <!-- 预览 -->
            <div class="action-section">
              <h4>合并预览</h4>
              <div class="preview-box">
                <pre>{{ JSON.stringify(previewData, null, 2) }}</pre>
              </div>
            </div>

            <el-divider />

            <!-- 确认按钮 -->
            <el-input
              v-model="reviewer"
              placeholder="操作人"
              style="margin-bottom:8px"
            />
            <el-input
              v-model="mergeNote"
              placeholder="备注（可选）"
              style="margin-bottom:12px"
            />
            <el-button
              type="primary"
              style="width:100%"
              :disabled="!canMerge"
              :loading="merging"
              @click="onMerge"
            >
              确认合并
            </el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 底部：合并历史 -->
    <el-card shadow="never" class="history-card">
      <template #header>
        <span>合并历史</span>
        <el-button size="small" text style="float:right" @click="loadMergeLogs">刷新</el-button>
      </template>
      <el-table :data="mergeLogs" stripe size="small" max-height="240">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="keeper_dict_id" label="Keeper" width="80" />
        <el-table-column label="Absorbed" min-width="120">
          <template #default="{ row }">
            <el-tag v-for="id in row.absorbed_dict_ids" :key="id" size="small" style="margin-right:4px">
              dict_id={{ id }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="reviewer" label="操作人" width="100" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.is_rolled_back" type="warning" size="small">已回滚</el-tag>
            <el-tag v-else type="success" size="small">已合并</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="170">
          <template #default="{ row }">{{ row.created_at?.slice(0, 19).replace('T', ' ') }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-popconfirm
              v-if="!row.is_rolled_back"
              title="确定回滚此合并？"
              @confirm="onRollback(row.id)"
            >
              <template #reference>
                <el-button size="small" type="danger" text>回滚</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import http from '@/api/http'

// ── 类型 ──
interface SearchEntityItem {
  dict_id: number
  entity_type: string
  entity_name: string
  canonical_source: string
  aliases: string[]
  extracted_entities_count: number
}

interface CompareEntityInfo {
  dict_id: number
  entity_type: string
  entity_name: string
  canonical_data: Record<string, any>
  canonical_source: string
  aliases: string[]
  extracted_entities_count: number
}

interface FieldDiffItem {
  field: string
  values: Record<string, any>
  conflict: boolean
}

interface CompareResult {
  entities: CompareEntityInfo[]
  field_diff: FieldDiffItem[]
}

interface MergeLogItem {
  id: number
  keeper_dict_id: number
  absorbed_dict_ids: number[]
  merge_type: string
  reviewer: string
  note: string
  is_rolled_back: boolean
  created_at: string | null
}

// ── 搜索 ──
const searchQuery = ref('')
const searching = ref(false)
const searched = ref(false)
const searchResults = ref<SearchEntityItem[]>([])

// ── 选择 ──
const selectedIds = ref<Set<number>>(new Set())
const selectedList = computed(() => {
  return searchResults.value.filter(r => selectedIds.value.has(r.dict_id))
})

// ── 对比 ──
const comparing = ref(false)
const compareResult = ref<CompareResult | null>(null)
const keeperDictId = ref<number>(0)

// ── 字段决策 ──
const fieldDecisions = reactive<Record<string, { picked_dict_id: number }>>({})

// ── 合并 ──
const merging = ref(false)
const reviewer = ref('')
const mergeNote = ref('')

// ── 合并历史 ──
const mergeLogs = ref<MergeLogItem[]>([])

// ── 计算 ──
const conflictFields = computed(() => {
  if (!compareResult.value) return []
  return compareResult.value.field_diff.filter(fd => fd.conflict)
})

const conflictCount = computed(() => conflictFields.value.length)

const previewData = computed(() => {
  if (!compareResult.value || !keeperDictId.value) return {}
  const merged: Record<string, any> = { name: '' }
  for (const fd of compareResult.value.field_diff) {
    const decision = fieldDecisions[fd.field]
    if (decision && fd.values) {
      merged[fd.field] = fd.values[String(decision.picked_dict_id)]
    } else if (fd.values) {
      // 默认用 keeper 的值
      merged[fd.field] = fd.values[String(keeperDictId.value)]
    }
  }
  return merged
})

const canMerge = computed(() => {
  return (
    compareResult.value !== null &&
    keeperDictId.value > 0 &&
    conflictCount.value === 0 ||
    conflictFields.value.every(fd => fieldDecisions[fd.field]?.picked_dict_id !== undefined)
  )
})

// ── 方法 ──
async function onSearch() {
  const q = searchQuery.value.trim()
  if (!q) {
    ElMessage.warning('请输入搜索关键词')
    return
  }
  searching.value = true
  searched.value = true
  try {
    const data = await http.get<any, SearchEntityItem[]>('/review/search-entities', {
      params: { q, limit: 30 },
    })
    searchResults.value = data || []
    if (searchResults.value.length === 0) {
      ElMessage.info('未找到匹配实体')
    }
  } catch (e: any) {
    ElMessage.error('搜索失败: ' + (e.message || e))
  } finally {
    searching.value = false
  }
}

function toggleSelect(item: SearchEntityItem) {
  const ids = new Set(selectedIds.value)
  if (ids.has(item.dict_id)) {
    ids.delete(item.dict_id)
  } else {
    if (ids.size >= 10) {
      ElMessage.warning('最多选择 10 个实体')
      return
    }
    ids.add(item.dict_id)
  }
  selectedIds.value = ids
}

function removeSelected(dictId: number) {
  const ids = new Set(selectedIds.value)
  ids.delete(dictId)
  selectedIds.value = ids
  if (keeperDictId.value === dictId) {
    keeperDictId.value = 0
  }
}

async function onCompare() {
  if (selectedList.value.length < 2) {
    ElMessage.warning('请至少选择 2 个实体')
    return
  }
  comparing.value = true
  compareResult.value = null
  keeperDictId.value = 0
  // 清空决策
  Object.keys(fieldDecisions).forEach(k => delete fieldDecisions[k])

  try {
    const dictIds = selectedList.value.map(e => e.dict_id).join(',')
    const data = await http.get<any, CompareResult>('/review/compare-entities', {
      params: { dict_ids: dictIds },
    })
    compareResult.value = data
    // 默认 keeper = 第一个
    if (data.entities.length > 0) {
      keeperDictId.value = data.entities[0].dict_id
    }
  } catch (e: any) {
    ElMessage.error('对比失败: ' + (e.message || e))
  } finally {
    comparing.value = false
  }
}

function pickField(field: string, dictId: number) {
  fieldDecisions[field] = { picked_dict_id: dictId }
}

function formatValue(val: any): string {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'object') return JSON.stringify(val)
  return String(val)
}

async function onMerge() {
  if (!compareResult.value || !keeperDictId.value) return
  if (!reviewer.value.trim()) {
    ElMessage.warning('请输入操作人')
    return
  }

  const absorbedIds = compareResult.value.entities
    .filter(e => e.dict_id !== keeperDictId.value)
    .map(e => e.dict_id)

  if (absorbedIds.length === 0) {
    ElMessage.warning('没有需要吸收的实体')
    return
  }

  const fd: Record<string, any> = {}
  for (const [k, v] of Object.entries(fieldDecisions)) {
    fd[k] = v
  }

  merging.value = true
  try {
    await http.post('/review/merge-entities', {
      keeper_dict_id: keeperDictId.value,
      absorbed_dict_ids: absorbedIds,
      field_decisions: fd,
      reviewer: reviewer.value.trim(),
      note: mergeNote.value.trim(),
    })
    ElMessage.success('合并成功')

    // 清空状态
    compareResult.value = null
    selectedIds.value = new Set()
    searchResults.value = []
    searched.value = false
    searchQuery.value = ''
    Object.keys(fieldDecisions).forEach(k => delete fieldDecisions[k])
    loadMergeLogs()
  } catch (e: any) {
    ElMessage.error('合并失败: ' + (e.message || e))
  } finally {
    merging.value = false
  }
}

async function onRollback(mergeLogId: number) {
  try {
    await http.post('/review/rollback-merge', {
      merge_log_id: mergeLogId,
      reason: '用户手动回滚',
    })
    ElMessage.success('回滚成功')
    loadMergeLogs()
  } catch (e: any) {
    ElMessage.error('回滚失败: ' + (e.message || e))
  }
}

async function loadMergeLogs() {
  try {
    const data = await http.get<any, { total: number; items: MergeLogItem[] }>('/review/merge-logs', {
      params: { limit: 50 },
    })
    mergeLogs.value = data.items || []
  } catch (e: any) {
    // 静默失败
  }
}

onMounted(() => {
  loadMergeLogs()
})
</script>

<style scoped>
.merge-workbench {
  padding: 12px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow: hidden;
}
.header-bar {
  flex-shrink: 0;
}
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.header-row h3 {
  margin: 0;
}
.main-row {
  flex: 1;
  min-height: 0;
}
.main-row > .el-col {
  height: 100%;
  overflow: hidden;
}
.select-card,
.diff-card,
.action-card {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.select-card :deep(.el-card__body),
.diff-card :deep(.el-card__body),
.action-card :deep(.el-card__body) {
  flex: 1;
  overflow-y: auto;
}
.search-section {
  margin-bottom: 12px;
}
.search-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  margin-bottom: 6px;
  cursor: pointer;
  transition: background 0.2s;
}
.search-item:hover {
  background: #f5f7fa;
}
.search-item.selected {
  border-color: #409eff;
  background: #ecf5ff;
}
.item-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}
.item-name {
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.item-meta {
  font-size: 12px;
  color: #909399;
  display: flex;
  gap: 8px;
  align-items: center;
}
.item-aliases {
  font-size: 11px;
  color: #c0c4cc;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.selected-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 0;
  border-bottom: 1px solid #f2f3f5;
}
.selected-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.selected-meta {
  font-size: 11px;
  color: #909399;
  margin-left: 24px;
}
.hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
.hint.success {
  color: #67c23a;
}
.conflict-field {
  font-weight: 600;
  color: #e6a23c;
}
.cell-value {
  font-size: 13px;
  word-break: break-all;
}
.cell-value.cell-selected {
  color: #409eff;
  font-weight: 600;
  cursor: pointer;
}
.cell-value.cell-keeper {
  cursor: default;
}
.check-mark {
  margin-right: 4px;
  color: #67c23a;
  font-weight: bold;
}
.placeholder {
  padding: 40px 0;
}
.action-section {
  margin-bottom: 8px;
}
.action-section h4 {
  margin: 0 0 8px 0;
  font-size: 14px;
}
.conflict-item {
  margin-bottom: 10px;
}
.conflict-label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  margin-bottom: 4px;
  color: #e6a23c;
}
.preview-box {
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  padding: 8px;
  max-height: 200px;
  overflow: auto;
}
.preview-box pre {
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
.history-card {
  flex-shrink: 0;
}
.diff-table-wrap {
  overflow: auto;
}
</style>
