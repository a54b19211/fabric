import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/documents',
    },
    {
      path: '/documents',
      name: 'Documents',
      component: () => import('@/views/DocumentList.vue'),
      meta: { title: '文档列表' },
    },
    {
      path: '/documents/:uuid',
      name: 'DocumentDetail',
      component: () => import('@/views/DocumentDetail.vue'),
      meta: { title: '文档详情' },
    },
    {
      path: '/review/:uuid',
      name: 'ReviewWorkbench',
      component: () => import('@/views/ReviewWorkbench.vue'),
      meta: { title: '审核工作台' },
    },
    {
      path: '/batch-review',
      name: 'BatchReviewWorkbench',
      component: () => import('@/views/BatchReviewWorkbench.vue'),
      meta: { title: '批量审核工作台' },
    },
    {
      path: '/graph-review',
      name: 'GraphReview',
      component: () => import('@/views/GraphReview.vue'),
      meta: { title: '图谱审核' },
    },
    {
      path: '/entity-merge',
      name: 'EntityMerge',
      component: () => import('@/views/EntityMerge.vue'),
      meta: { title: '实体消歧' },
    },
    {
      path: '/review/merge-workbench',
      name: 'MergeWorkbench',
      component: () => import('@/views/MergeWorkbench.vue'),
      meta: { title: '实体身份合并' },
    },
    {
      path: '/review/field-conflict',
      name: 'FieldConflict',
      component: () => import('@/views/FieldConflict.vue'),
      meta: { title: '属性冲突处理' },
    },
    {
      path: '/review/identity-merge',
      name: 'IdentityMergeProposals',
      component: () => import('@/views/IdentityMergeProposals.vue'),
      meta: { title: '身份合并提案' },
    },
    {
      path: '/neo4j',
      name: 'Neo4j',
      component: () => import('@/views/Neo4jManager.vue'),
      meta: { title: 'Neo4j 图数据推送' },
    },
    {
      path: '/weknora',
      name: 'Weknora',
      component: () => import('@/views/WeknoraManager.vue'),
      meta: { title: 'WeKnora 知识库同步' },
    },
    {
      path: '/agent',
      name: 'AutoReview',
      component: () => import('@/views/AutoReviewDashboard.vue'),
      meta: { title: '自动审核' },
    },
    {
      path: '/agent/document-review',
      name: 'DocumentReview',
      component: () => import('@/views/DocumentReview.vue'),
      meta: { title: '文档审核' },
    },
  ],
})

router.afterEach((to) => {
  document.title = `${to.meta.title ?? ''} — ZK Data Fabric`
})

export default router
