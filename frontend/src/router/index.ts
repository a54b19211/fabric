import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/overview',
    },
    {
      path: '/overview',
      name: 'Overview',
      component: () => import('@/views/PipelineOverview.vue'),
      meta: { title: '数据面板', description: '抽取 → 审核 → 消歧 → 归并 → 同步 各阶段数据与状态分布总览' },
    },
    {
      path: '/documents',
      name: 'Documents',
      component: () => import('@/views/DocumentList.vue'),
      meta: { title: '文档列表', description: '管理采集文档，触发 LLM 抽取、重爬与上传' },
    },
    {
      path: '/documents/:uuid',
      name: 'DocumentDetail',
      component: () => import('@/views/DocumentDetail.vue'),
      meta: { title: '文档详情', description: '查看文档元信息与原文，触发抽取并预览实体关系图谱' },
    },
    {
      path: '/review/:uuid',
      name: 'ReviewWorkbench',
      component: () => import('@/views/ReviewWorkbench.vue'),
      meta: { title: '审核工作台', description: '单篇文档的人工实体/关系审核与消歧链接' },
    },
    {
      path: '/batch-review',
      name: 'BatchReviewWorkbench',
      component: () => import('@/views/BatchReviewWorkbench.vue'),
      meta: { title: '批量审核工作台', description: '跨文档批量人工审核实体与关系' },
    },
    {
      path: '/graph-review',
      name: 'GraphReview',
      component: () => import('@/views/GraphReview.vue'),
      meta: { title: '图谱审核', description: '以图视角可视化审核实体与关系' },
    },
    {
      path: '/entity-merge',
      name: 'EntityMerge',
      component: () => import('@/views/EntityMerge.vue'),
      meta: { title: '实体消歧', description: '对已审核实体执行 Agent 消歧链接' },
    },
    {
      path: '/review/merge-workbench',
      name: 'MergeWorkbench',
      component: () => import('@/views/MergeWorkbench.vue'),
      meta: { title: '实体身份合并', description: '合并重复的字典实体并裁决字段' },
    },
    {
      path: '/review/field-conflict',
      name: 'FieldConflict',
      component: () => import('@/views/FieldConflict.vue'),
      meta: { title: '属性冲突处理', description: '逐字段裁决实体属性冲突' },
    },
    {
      path: '/review/identity-merge',
      name: 'IdentityMergeProposals',
      component: () => import('@/views/IdentityMergeProposals.vue'),
      meta: { title: '身份合并提案', description: '人工确认 Agent 生成的身份合并提案' },
    },
    {
      path: '/neo4j',
      name: 'Neo4j',
      component: () => import('@/views/Neo4jManager.vue'),
      meta: { title: 'Neo4j 图数据推送', description: '推送实体关系到 Neo4j 图库' },
    },
    {
      path: '/weknora',
      name: 'Weknora',
      component: () => import('@/views/WeknoraManager.vue'),
      meta: { title: 'WeKnora 知识库同步', description: '文档同步到 WeKnora 知识库' },
    },
    {
      path: '/agent',
      name: 'AutoReview',
      component: () => import('@/views/AutoReviewDashboard.vue'),
      meta: { title: '自动审核', description: 'Agent 自动审核的全局统计与批量触发' },
    },
    {
      path: '/agent/document-review',
      name: 'DocumentReview',
      component: () => import('@/views/DocumentReview.vue'),
      meta: { title: '文档审核', description: '按文档触发 Agent 审核并复核分步结果' },
    },
  ],
})

router.afterEach((to) => {
  document.title = `${to.meta.title ?? ''} — ZK Data Fabric`
})

export default router
