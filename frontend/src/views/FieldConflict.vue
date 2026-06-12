<template>
  <div class="field-conflict-page">
    <el-card shadow="never" class="header-bar">
      <div class="header-row">
        <h3>属性冲突处理</h3>
        <div class="header-meta">
          <span>待处理: <strong>{{ totalGroups }}</strong> 组</span>
          <el-button size="small" @click="loadData" :loading="loading">刷新</el-button>
        </div>
      </div>
    </el-card>

    <div class="conflict-list" v-loading="loading">
      <el-empty v-if="!groups.length && !loading" description="暂无待处理的属性冲突" :image-size="100" />

      <div v-for="group in groups" :key="group.dict_id" class="group-card">
        <el-card shadow="hover">
          <!-- 实体上下文 -->
          <div class="group-header">
            <div class="group-title">
              <strong>{{ group.entity_name }}</strong>
              <el-tag size="small" type="info">{{ group.entity_type }}</el-tag>
              <span class="group-meta">dict_id={{ group.dict_id }} · 来源: {{ group.canonical_source }}</span>
            </div>
            <el-button size="small" type="primary" @click="resolveAllInGroup(group)">
              一键全部解决
            </el-button>
          </div>

          <!-- 完整 canonical_data 上下文（折叠无冲突字段） -->
          <div class="cd-context">
            <span class="context-label">当前 canonical_data:</span>
            <div class="cd-fields">
              <template v-for="(val, key) in group.canonical_data" :key="key">
                <div
                  class="cd-field"
                  :class="{ 'cd-field-conflict': group.conflicts.some(c => c.field_name === key) }"
                >
                  <span class="cd-field-name">{{ key }}</span>
                  <span class="cd-field-val">{{ formatValue(val) }}</span>
                  <el-tag v-if="group.conflicts.some(c => c.field_name === key)" size="small" type="warning" effect="plain">冲突</el-tag>
                </div>
              </template>
            </div>
          </div>

          <el-divider />

          <!-- 冲突字段逐条 -->
          <div v-for="c in group.conflicts" :key="c.merge_id" class="conflict-card">
            <div class="card-header">
              <div class="card-tags">
                <el-tag size="small" type="danger">{{ c.field_name }}</el-tag>
                <el-tag size="small" type="info">{{ c.source_site }}</el-tag>
              </div>
            </div>

            <el-row :gutter="16" class="compare-row">
              <el-col :span="12">
                <div class="value-box existing">
                  <div class="value-label">
                    现有值
                    <template v-if="c.existing_sources?.length">
                      <router-link
                        v-for="s in c.existing_sources"
                        :key="s.source_doc_uuid"
                        :to="`/review/${s.source_doc_uuid}`"
                        :title="s.source_doc_uuid + ' (' + s.source_site + ')'"
                        class="doc-link"
                      >
                        {{ s.source_doc_uuid.slice(0, 8) }}...
                      </router-link>
                    </template>
                  </div>
                  <pre>{{ formatValue(c.existing_value) }}</pre>
                </div>
              </el-col>
              <el-col :span="12">
                <div class="value-box incoming">
                  <div class="value-label">
                    新值
                    <template v-if="c.new_sources?.length">
                      <router-link
                        v-for="s in c.new_sources"
                        :key="s.source_doc_uuid"
                        :to="`/review/${s.source_doc_uuid}`"
                        :title="s.source_doc_uuid + ' (' + s.source_site + ')'"
                        class="doc-link"
                      >
                        {{ s.source_doc_uuid.slice(0, 8) }}...
                      </router-link>
                    </template>
                  </div>
                  <pre>{{ formatValue(c.new_value) }}</pre>
                </div>
              </el-col>
            </el-row>

            <div class="card-actions">
              <el-input
                v-if="showCustom[c.merge_id]"
                v-model="customValues[c.merge_id]"
                placeholder='自定义值 (JSON 格式)'
                size="small"
                style="margin-bottom:6px"
              />
              <div class="action-btns">
                <el-button size="small" type="success" @click="doResolve(c.merge_id, 'accept_new', group.dict_id)">
                  采用新值
                </el-button>
                <el-button size="small" type="warning" @click="doResolve(c.merge_id, 'keep_existing', group.dict_id)">
                  保留现有
                </el-button>
                <template v-if="!showCustom[c.merge_id]">
                  <el-button size="small" text type="primary" @click="showCustom[c.merge_id] = true">
                    自定义
                  </el-button>
                </template>
                <template v-else>
                  <el-button
                    size="small" type="primary"
                    :disabled="!customValues[c.merge_id]"
                    @click="doResolve(c.merge_id, 'custom', group.dict_id, customValues[c.merge_id])"
                  >
                    确认
                  </el-button>
                  <el-button size="small" text @click="showCustom[c.merge_id] = false; delete customValues[c.merge_id]">
                    取消
                  </el-button>
                </template>
              </div>
            </div>
          </div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getMergeQueue, resolveMerge, batchResolveMerge, type MergeQueueGroup } from '@/api/review'

const groups = ref<MergeQueueGroup[]>([])
const totalGroups = ref(0)
const loading = ref(false)
const showCustom = ref<Record<number, boolean>>({})
const customValues = ref<Record<number, string>>({})

async function loadData() {
  loading.value = true
  try {
    const res = await getMergeQueue(50)
    groups.value = res.groups
    totalGroups.value = res.total_groups
  } catch (e: any) {
    ElMessage.error('加载失败: ' + (e.message || ''))
  } finally {
    loading.value = false
  }
}

async function doResolve(mergeId: number, resolution: string, dictId: number, customValue?: string) {
  let resolvedValue: any = undefined
  if (resolution === 'custom' && customValue) {
    try {
      resolvedValue = JSON.parse(customValue)
    } catch {
      ElMessage.error('JSON 格式无效')
      return
    }
  }

  try {
    await resolveMerge(mergeId, '审核员', resolution, resolvedValue)
    ElMessage.success({
      accept_new: '已采用新值',
      keep_existing: '已保留现有值',
      custom: '已使用自定义值',
    }[resolution] || '已处理')

    removeConflict(dictId, mergeId)
  } catch (e: any) {
    ElMessage.error('操作失败: ' + (e.message || ''))
  }
}

async function resolveAllInGroup(group: MergeQueueGroup) {
  const decisions = group.conflicts.map(c => ({
    merge_id: c.merge_id,
    resolution: 'keep_existing',
  }))

  try {
    await batchResolveMerge(decisions, '审核员')
    ElMessage.success(`已保留 group dict_id=${group.dict_id} 的全部 ${decisions.length} 个现有值`)
    groups.value = groups.value.filter(g => g.dict_id !== group.dict_id)
  } catch (e: any) {
    ElMessage.error('批量处理失败: ' + (e.message || ''))
  }
}

function removeConflict(dictId: number, mergeId: number) {
  for (const g of groups.value) {
    if (g.dict_id === dictId) {
      g.conflicts = g.conflicts.filter(c => c.merge_id !== mergeId)
      break
    }
  }
  groups.value = groups.value.filter(g => g.conflicts.length > 0)
}

function formatValue(val: any): string {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'string') return val
  return JSON.stringify(val, null, 2)
}

loadData()
</script>

<style scoped>
.field-conflict-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
}
.header-bar { flex-shrink: 0; }
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.header-row h3 { margin: 0; }
.header-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  color: #909399;
  font-size: 14px;
}
.conflict-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.group-card {
  max-width: 960px;
}
.group-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.group-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.group-meta {
  font-size: 12px;
  color: #909399;
}
.cd-context {
  margin-bottom: 4px;
}
.context-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 6px;
  display: block;
}
.cd-fields {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.cd-field {
  display: flex;
  align-items: center;
  gap: 4px;
  background: #f5f7fa;
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 12px;
}
.cd-field.cd-field-conflict {
  background: #fef0f0;
  border: 1px solid #fde2e2;
}
.cd-field-name {
  font-weight: 500;
  color: #606266;
}
.cd-field-val {
  color: #909399;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conflict-card {
  margin-bottom: 12px;
  padding: 12px;
  background: #fafafa;
  border-radius: 6px;
}
.card-header { margin-bottom: 8px; }
.card-tags {
  display: flex;
  gap: 8px;
  align-items: center;
}
.card-source {
  font-size: 12px;
  color: #c0c4cc;
}
.doc-link {
  font-size: 12px;
  color: #409eff;
  text-decoration: none;
}
.doc-link:hover {
  text-decoration: underline;
}
.compare-row { margin-bottom: 10px; }
.value-box {
  background: #fff;
  border-radius: 6px;
  padding: 8px 12px;
  min-height: 50px;
}
.value-box.existing { border-left: 3px solid #e6a23c; }
.value-box.incoming { border-left: 3px solid #409eff; }
.value-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
  font-weight: 500;
}
.value-box pre {
  margin: 0;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 150px;
  overflow-y: auto;
}
.card-actions { margin-top: 8px; }
.action-btns {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
