<template>
  <div class="identity-merge-page">
    <el-card shadow="never" class="header-bar">
      <div class="header-row">
        <h3>身份合并提案</h3>
        <div class="header-meta">
          <span>待处理: <strong>{{ proposals.length }}</strong> 条</span>
          <el-button size="small" @click="loadData" :loading="loading">刷新</el-button>
        </div>
      </div>
    </el-card>

    <div class="proposal-list" v-loading="loading">
      <el-empty v-if="!proposals.length && !loading" description="暂无身份合并提案" :image-size="100" />

      <div v-for="p in proposals" :key="p.id" class="proposal-card">
        <el-card shadow="hover">
          <div class="proposal-header">
            <div class="agent-badge">
              <el-tag type="warning" size="small">Agent 建议合并</el-tag>
              <span class="confidence">置信度: {{ (p.agent_confidence * 100).toFixed(0) }}%</span>
            </div>
            <span class="proposal-id">#{{ p.id }}</span>
          </div>

          <el-row :gutter="20" class="compare-row">
            <!-- 源：extracted_entity -->
            <el-col :span="12">
              <div class="entity-box source">
                <div class="box-title">
                  <el-tag size="small" type="info">源实体 (extracted_entity)</el-tag>
                  <span class="entity-id">ID: {{ p.source_entity_id }}</span>
                </div>
                <div class="box-body">
                  <div class="field-row">
                    <span class="field-label">名称</span>
                    <span class="field-value strong">{{ p.source_entity_snapshot.entity_name }}</span>
                  </div>
                  <div class="field-row">
                    <span class="field-label">类型</span>
                    <el-tag size="small">{{ p.source_entity_snapshot.entity_type }}</el-tag>
                  </div>
                  <div class="field-row">
                    <span class="field-label">来源站点</span>
                    <span class="field-value">{{ p.source_entity_snapshot.source_site || '—' }}</span>
                  </div>
                  <div class="field-row" v-if="p.source_entity_snapshot.source_doc_uuid">
                    <span class="field-label">来源文档</span>
                    <router-link :to="`/review/${p.source_entity_snapshot.source_doc_uuid}`" class="doc-link">
                      {{ p.source_entity_snapshot.source_doc_uuid.slice(0, 12) }}...
                    </router-link>
                  </div>
                  <div class="field-row" v-if="p.source_entity_snapshot.source_text">
                    <span class="field-label">原文片段</span>
                    <span class="field-value text-preview">
                      {{ p.source_entity_snapshot.source_text.slice(0, 200) }}
                    </span>
                  </div>
                  <div v-if="sourceFields(p).length > 0" class="field-row">
                    <span class="field-label">属性</span>
                    <div class="props-grid">
                      <div v-for="(val, key) in p.source_entity_snapshot.entity_data" :key="key" class="prop-item">
                        <span class="prop-key">{{ key }}</span>
                        <span class="prop-val">{{ formatValue(val) }}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </el-col>

            <!-- 目标：entity_dict -->
            <el-col :span="12">
              <div class="entity-box target">
                <div class="box-title">
                  <el-tag size="small" type="success">目标实体 (entity_dict)</el-tag>
                  <span class="entity-id">dict_id={{ p.target_dict_id }}</span>
                </div>
                <div class="box-body">
                  <div class="field-row">
                    <span class="field-label">名称</span>
                    <span class="field-value strong">
                      {{ p.target_dict_snapshot.canonical_name }}
                    </span>
                  </div>
                  <div class="field-row">
                    <span class="field-label">类型</span>
                    <el-tag size="small">{{ p.target_dict_snapshot.entity_type }}</el-tag>
                  </div>
                  <div class="field-row">
                    <span class="field-label">来源</span>
                    <span class="field-value">{{ p.target_dict_snapshot.source_site || '—' }}</span>
                  </div>
                  <div class="field-row">
                    <span class="field-label">状态</span>
                    <el-tag v-if="p.target_dict_snapshot.is_verified" size="small" type="success">已审核</el-tag>
                    <el-tag v-else size="small" type="info">未审核</el-tag>
                  </div>
                  <div class="field-row" v-if="p.target_dict_snapshot.aliases?.length">
                    <span class="field-label">别名</span>
                    <div class="alias-tags">
                      <el-tag v-for="a in p.target_dict_snapshot.aliases" :key="a" size="small" type="info" effect="plain">
                        {{ a }}
                      </el-tag>
                    </div>
                  </div>
                  <div v-if="Object.keys(p.target_dict_snapshot.key_fields || {}).length > 0" class="field-row">
                    <span class="field-label">关键标识</span>
                    <div class="props-grid">
                      <div v-for="(val, key) in p.target_dict_snapshot.key_fields" :key="key" class="prop-item highlight">
                        <span class="prop-key">{{ key }}</span>
                        <span class="prop-val">{{ val || '—' }}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </el-col>
          </el-row>

          <!-- Agent 推理 -->
          <div class="reason-section" v-if="p.agent_reason">
            <div class="reason-label">Agent 推理:</div>
            <div class="reason-text">{{ p.agent_reason }}</div>
          </div>

          <!-- 操作 -->
          <div class="action-bar">
            <el-input v-model="reviewers[p.id]" placeholder="操作人" size="small" style="width: 140px" />
            <el-button type="primary" size="small" @click="onApprove(p)" :loading="acting === p.id">
              确认链接
            </el-button>
            <el-button type="danger" size="small" plain @click="onReject(p)" :loading="acting === p.id">
              拒绝
            </el-button>
            <el-button size="small" text type="primary" @click="$router.push('/review/merge-workbench')">
              去合并工作台对比 →
            </el-button>
          </div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getIdentityMergeProposals,
  approveIdentityMergeProposal,
  rejectIdentityMergeProposal,
  type IdentityMergeProposal,
} from '@/api/review'

const proposals = ref<IdentityMergeProposal[]>([])
const loading = ref(false)
const acting = ref<number>(0)
const reviewers = reactive<Record<number, string>>({})

function sourceFields(p: IdentityMergeProposal): string[] {
  const data = p.source_entity_snapshot.entity_data
  if (!data) return []
  return Object.keys(data)
}

async function loadData() {
  loading.value = true
  try {
    const res = await getIdentityMergeProposals('pending', 100)
    proposals.value = res.items
    for (const p of proposals.value) {
      if (!reviewers[p.id]) reviewers[p.id] = '审核员'
    }
  } catch (e: any) {
    ElMessage.error('加载失败: ' + (e.message || ''))
  } finally {
    loading.value = false
  }
}

async function onApprove(p: IdentityMergeProposal) {
  if (!reviewers[p.id]?.trim()) {
    ElMessage.warning('请输入操作人')
    return
  }
  acting.value = p.id
  try {
    await approveIdentityMergeProposal(p.id, reviewers[p.id].trim())
    ElMessage.success(`已链接: ${p.source_entity_snapshot.entity_name} → dict_id=${p.target_dict_id}`)
    proposals.value = proposals.value.filter(x => x.id !== p.id)
  } catch (e: any) {
    ElMessage.error('操作失败: ' + (e.message || ''))
  } finally {
    acting.value = 0
  }
}

async function onReject(p: IdentityMergeProposal) {
  if (!reviewers[p.id]?.trim()) {
    ElMessage.warning('请输入操作人')
    return
  }
  acting.value = p.id
  try {
    await rejectIdentityMergeProposal(p.id, reviewers[p.id].trim())
    ElMessage.info(`已拒绝: ${p.source_entity_snapshot.entity_name}`)
    proposals.value = proposals.value.filter(x => x.id !== p.id)
  } catch (e: any) {
    ElMessage.error('操作失败: ' + (e.message || ''))
  } finally {
    acting.value = 0
  }
}

function formatValue(val: any): string {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'object') return JSON.stringify(val)
  return String(val)
}

onMounted(() => {
  loadData()
})
</script>

<style scoped>
.identity-merge-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  padding: 12px;
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
  color: var(--dp-text-secondary);
  font-size: 14px;
}
.proposal-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-bottom: 24px;
}
.proposal-card { max-width: 1100px; }
.proposal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.agent-badge {
  display: flex;
  align-items: center;
  gap: 10px;
}
.confidence {
  font-size: 13px;
  color: var(--dp-text-secondary);
}
.proposal-id {
  font-size: 12px;
  color: var(--dp-text-placeholder);
}
.compare-row { margin-bottom: 14px; }
.entity-box {
  border-radius: var(--dp-radius-sm);
  border: 1px solid var(--dp-border-light);
  overflow: hidden;
}
.entity-box.source { border-left: 4px solid var(--dp-info); }
.entity-box.target { border-left: 4px solid var(--dp-success); }
.box-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  background: var(--dp-muted-bg);
  border-bottom: 1px solid var(--dp-border-light);
}
.entity-id {
  font-size: 12px;
  color: var(--dp-text-placeholder);
}
.box-body {
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.field-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.field-label {
  font-size: 12px;
  color: var(--dp-text-secondary);
  font-weight: 500;
}
.field-value {
  font-size: 14px;
  color: var(--dp-text-regular);
}
.field-value.strong {
  font-size: 16px;
  font-weight: 600;
}
.field-value.text-preview {
  font-size: 13px;
  color: var(--dp-text-secondary);
  line-height: 1.5;
  max-height: 80px;
  overflow-y: auto;
}
.doc-link {
  font-size: 13px;
  color: var(--dp-link);
  text-decoration: none;
}
.doc-link:hover { text-decoration: underline; }
.props-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.prop-item {
  display: flex;
  align-items: center;
  gap: 4px;
  background: var(--dp-muted-bg);
  padding: 2px 8px;
  border-radius: var(--dp-radius-xs);
  font-size: 12px;
}
.prop-item.highlight {
  background: var(--dp-info-light);
  border: 1px solid var(--dp-info-lighter);
}
.prop-key {
  font-weight: 500;
  color: var(--dp-text-regular);
}
.prop-val {
  color: var(--dp-text-secondary);
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.alias-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.reason-section {
  background: var(--dp-muted-bg);
  border-radius: var(--dp-radius-sm);
  padding: 12px 14px;
  margin-bottom: 14px;
}
.reason-label {
  font-size: 12px;
  color: var(--dp-text-secondary);
  font-weight: 500;
  margin-bottom: 6px;
}
.reason-text {
  font-size: 14px;
  color: var(--dp-text-regular);
  line-height: 1.6;
  white-space: pre-wrap;
}
.action-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-content: flex-end;
}
</style>
