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
  ],
})

router.afterEach((to) => {
  document.title = `${to.meta.title ?? ''} — ZK Data Fabric`
})

export default router
