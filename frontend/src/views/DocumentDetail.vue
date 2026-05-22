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
              <span :style="{ color: doc.confidence_score < 0.8 ? '#f56c6c' : '#67c23a' }">
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
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { marked } from 'marked'
import { getDocument, recrawlDocument, type DocumentDetail } from '@/api/documents'
import { triggerExtraction } from '@/api/extraction'

const route = useRoute()
const uuid = route.params.uuid as string

const doc = ref<DocumentDetail | null>(null)
const loading = ref(false)
const triggering = ref(false)
const recrawling = ref(false)

const renderedContent = computed(() => {
  if (!doc.value?.raw_content) return '<p style="color:#999">暂无内容</p>'
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
  ({ pending: 'info', processing: 'warning', done: 'success', failed: 'danger' }[s] ?? '')

onMounted(load)
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
  background: #f5f7fa;
  padding: 12px;
  border-radius: 4px;
  overflow-x: auto;
}
</style>
