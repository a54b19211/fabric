<template>
  <div class="reasoning-timeline">
    <div v-if="!steps || steps.length === 0" class="empty-state">
      <el-empty description="无推理步骤" :image-size="60" />
    </div>

    <!-- Pipeline 步骤（实体/关系审核） -->
    <div v-else-if="isPipeline" class="pipeline">
      <div class="pipeline-bar">
        <div
          v-for="(step, idx) in pipelineSteps"
          :key="step.step_id"
          class="pipeline-step"
          :class="step.status"
        >
          <div class="step-indicator">
            <el-icon v-if="step.status === 'passed'" :size="16"><CircleCheck /></el-icon>
            <el-icon v-else-if="step.status === 'failed'" :size="16"><CircleClose /></el-icon>
            <span v-else class="step-num">{{ idx + 1 }}</span>
          </div>
          <div class="step-label">{{ step.step_name }}</div>
          <div class="step-duration">{{ step.duration_ms }}ms</div>

          <!-- 展开详情 -->
          <div v-if="step._expanded" class="step-detail">
            <div class="detail-section" v-if="step.step_id === 'source_evidence' && step.input?.source_text">
              <div class="detail-title">原文上下文</div>
              <div class="detail-text source-text">{{ step.input?.source_text }}</div>
            </div>
            <div class="detail-section" v-if="step.output?.reason">
              <div class="detail-title">判断</div>
              <div class="detail-text">{{ step.output?.reason }}</div>
            </div>
            <div class="detail-section" v-if="step.output?.suggested_type">
              <div class="detail-title">建议类型</div>
              <el-tag size="small">{{ step.output?.suggested_type }}</el-tag>
            </div>
            <div class="detail-section" v-if="step.output?.overall">
              <div class="detail-title">原文覆盖率</div>
              <el-tag size="small" :type="step.output?.overall === 'full' ? 'success' : step.output?.overall === 'partial' ? 'warning' : 'danger'">
                {{ overallLabel(step.output?.overall) }}
              </el-tag>
            </div>
            <div class="detail-section" v-if="step.step_id === 'source_evidence' && step.output?.fields?.length">
              <div class="detail-title">字段验证</div>
              <div v-for="f in step.output.fields" :key="f.name" class="field-row">
                <el-icon v-if="f.has_evidence" color="#67C23A"><CircleCheck /></el-icon>
                <el-icon v-else color="#F56C6C"><CircleClose /></el-icon>
                <span class="field-name">{{ f.name }}</span>
                <span v-if="f.evidence_span" class="field-evidence">→ "{{ f.evidence_span }}"</span>
              </div>
            </div>
          </div>
          <el-button
            text
            size="small"
            class="expand-btn"
            @click="toggleExpand(step)"
          >
            {{ step._expanded ? '收起' : '详情' }}
          </el-button>
        </div>

        <!-- 连接线 -->
        <div class="pipeline-connectors">
          <div
            v-for="i in pipelineSteps.length - 1"
            :key="'conn-' + i"
            class="connector"
            :class="pipelineSteps[i - 1]?.status === 'passed' ? 'active' : ''"
          />
        </div>
      </div>

      <div class="pipeline-decision" v-if="decision">
        <el-tag :type="decisionType" size="large">
          {{ decisionLabel }}
        </el-tag>
        <span class="decision-confidence">置信度 {{ (confidence * 100).toFixed(0) }}%</span>
      </div>
    </div>

    <!-- ReAct 步骤（冲突裁决/语义匹配） -->
    <div v-else class="timeline">
      <div v-for="step in reactSteps" :key="step.step" class="step">
        <div class="step-marker" :class="step.type">
          <el-icon v-if="step.type === 'ai'" :size="14"><ChatDotRound /></el-icon>
          <el-icon v-else :size="14"><Tools /></el-icon>
        </div>
        <div class="step-body">
          <div class="step-header">
            <span class="step-label">
              Step {{ step.step }}
              <el-tag size="small" :type="step.type === 'ai' ? 'primary' : 'warning'" class="step-tag">
                {{ step.type === 'ai' ? 'AI 推理' : '工具调用' }}
              </el-tag>
            </span>
            <span v-if="step.type === 'tool'" class="tool-name">
              {{ step.tool_name }}
            </span>
            <span v-if="step.type === 'ai' && step.tool_calls?.length" class="pending-calls">
              调用: {{ step.tool_calls.join(', ') }}
            </span>
          </div>
          <div v-if="step.content" class="step-content">
            <el-text truncated>{{ step.content }}</el-text>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { ChatDotRound, Tools, CircleCheck, CircleClose } from '@element-plus/icons-vue'
import type { ReviewStep, PipelineStep, AgentStep } from '@/api/agent'

const props = defineProps<{
  steps: ReviewStep[]
  decision?: string
  confidence?: number
}>()

// 自动展开所有 pipeline 步骤
watch(() => props.steps, (newSteps) => {
  for (const s of newSteps) {
    if (isPipelineStep(s)) {
      ;(s as any)._expanded = true
    }
  }
}, { immediate: true })

function isPipelineStep(s: ReviewStep): s is PipelineStep {
  return 'step_id' in s
}

const isPipeline = computed(() => props.steps.length > 0 && isPipelineStep(props.steps[0]))

const pipelineSteps = computed(() =>
  isPipeline.value ? (props.steps.map(s => ({ ...s, _expanded: !!(s as any)._expanded ?? true })) as (PipelineStep & { _expanded: boolean })[]) : []
)

const reactSteps = computed(() =>
  !isPipeline.value ? (props.steps as AgentStep[]) : []
)

function toggleExpand(step: PipelineStep & { _expanded: boolean }) {
  step._expanded = !step._expanded
}

function overallLabel(v: string) {
  const map: Record<string, string> = { full: '全部有据', partial: '部分有据', none: '无依据' }
  return map[v] || v
}

const decisionLabel = computed(() => {
  const map: Record<string, string> = {
    approved: '审核通过', rejected: '审核驳回', escalated: '转人工',
    keep_existing: '保留现有', accept_new: '接受新值', custom: '自定义值', error: '错误',
  }
  return map[props.decision || ''] || props.decision || ''
})

const decisionType = computed(() => {
  const d = props.decision
  if (!d) return ''
  if (['approved', 'accept_new'].includes(d)) return 'success'
  if (d === 'rejected') return 'danger'
  if (d === 'escalated') return 'warning'
  return 'info'
})
</script>

<style scoped>
.reasoning-timeline {
  padding: 4px 0;
}

.empty-state {
  padding: 20px 0;
}

/* ── Pipeline 步骤条 ── */
.pipeline-bar {
  display: flex;
  align-items: flex-start;
  gap: 0;
  position: relative;
  padding: 8px 0 20px;
}

.pipeline-step {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  position: relative;
  z-index: 1;
}

.step-indicator {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-fill-color-light);
  border: 2px solid var(--el-border-color);
  transition: all 0.3s;
}

.pipeline-step.passed .step-indicator {
  background: var(--el-color-success-light-9);
  border-color: var(--el-color-success);
  color: var(--el-color-success);
}

.pipeline-step.failed .step-indicator {
  background: var(--el-color-danger-light-9);
  border-color: var(--el-color-danger);
  color: var(--el-color-danger);
}

.pipeline-step.skipped .step-indicator {
  opacity: 0.4;
}

.step-num {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}

.step-label {
  margin-top: 8px;
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-regular);
}

.step-duration {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
  margin-top: 2px;
}

.pipeline-connectors {
  position: absolute;
  top: 16px;
  left: 0;
  right: 0;
  display: flex;
  padding: 0 calc(50% / 3);
  pointer-events: none;
}

.connector {
  flex: 1;
  height: 2px;
  background: var(--el-border-color-light);
}

.connector.active {
  background: var(--el-color-success);
}

/* 详情展开 */
.step-detail {
  margin-top: 8px;
  width: 100%;
  text-align: left;
  padding: 8px;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  font-size: 12px;
}

.detail-section {
  margin-bottom: 6px;
}

.detail-section:last-child {
  margin-bottom: 0;
}

.detail-title {
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin-bottom: 2px;
  font-size: 11px;
}

.detail-text {
  color: var(--el-text-color-regular);
  line-height: 1.5;
}

.field-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 0;
}

.field-name {
  font-family: monospace;
  font-size: 12px;
}

.field-evidence {
  color: var(--el-color-success);
  font-size: 12px;
}

.expand-btn {
  margin-top: 4px;
}

.pipeline-decision {
  display: flex;
  align-items: center;
  gap: 12px;
  justify-content: center;
  padding: 12px 0 4px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.decision-confidence {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* ── ReAct 时间线（保持不变） ── */
.timeline {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.step {
  display: flex;
  gap: 12px;
  padding: 8px 0;
  position: relative;
}

.step:not(:last-child)::before {
  content: '';
  position: absolute;
  left: 11px;
  top: 32px;
  bottom: -8px;
  width: 2px;
  background: var(--el-border-color-light);
}

.step-marker {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  z-index: 1;
}

.step-marker.ai {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}

.step-marker.tool {
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning);
}

.step-body {
  flex: 1;
  min-width: 0;
}

.step-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13px;
  line-height: 24px;
}

.step-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
}

.step-tag {
  font-size: 11px;
}

.tool-name {
  color: var(--el-color-warning);
  font-family: monospace;
  font-size: 12px;
}

.pending-calls {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.step-content {
  margin-top: 4px;
  padding: 8px 12px;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.5;
  max-height: 120px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
