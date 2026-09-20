<template>
  <div class="app-layout">
    <!-- 侧边栏：鼠标移入展开，移出收起；点击主内容区也收起 -->
    <aside
      class="sidebar"
      :class="{ collapsed }"
      @mouseenter="collapsed = false"
      @mouseleave="collapsed = true"
    >
      <!-- Logo 区域 -->
      <div class="sidebar-logo">
        <span class="logo-text">
          <span class="logo-icon">⬡</span>
          <span v-if="!collapsed" class="logo-name">DataPulse</span>
        </span>
      </div>

      <!-- 导航菜单（按工作流分组） -->
      <nav class="sidebar-nav">
        <template v-for="group in navGroups" :key="group.title">
          <div v-if="!collapsed" class="nav-group-title">{{ group.title }}</div>
          <div v-else class="nav-group-divider" />
          <router-link
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="nav-item"
            :class="{ active: $route.path === item.path }"
          >
            <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
            <span v-if="!collapsed" class="nav-label">{{ item.label }}</span>
            <!-- 收起时悬浮 tooltip -->
            <div v-if="collapsed" class="nav-tooltip">{{ item.label }}</div>
          </router-link>
        </template>
      </nav>
    </aside>

    <!-- 主内容区：点击任意位置收起侧边栏 -->
    <div class="main-wrapper" @click="collapsed = true">
      <header class="top-header">
        <div class="header-title-row">
          <h1 class="page-title">{{ $route.meta.title }}</h1>
          <p class="page-desc">{{ $route.meta.description }}</p>
        </div>
      </header>
      <main class="main-content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import {
  Document,
  Connection,
  Link,
  Setting,
  Cpu,
  Select,
  Aim,
  Switch,
  FolderOpened,
  Share,
  DataAnalysis,
} from '@element-plus/icons-vue'

// 默认收起，鼠标移入侧边栏展开
const collapsed = ref(true)

// 导航按工作流分组：总览 → 采集 → 审核 → 消歧 → 归并 → 同步
const navGroups = [
  {
    title: '总览',
    items: [
      { path: '/overview', label: '数据面板', icon: DataAnalysis },
    ],
  },
  {
    title: '数据采集',
    items: [
      { path: '/documents', label: '文档列表', icon: Document },
    ],
  },
  {
    title: '实体审核',
    items: [
      { path: '/agent', label: '自动审核', icon: Cpu },
      { path: '/agent/document-review', label: '文档审核', icon: Select },
      { path: '/graph-review', label: '图谱审核', icon: Connection },
    ],
  },
  {
    title: '消歧链接',
    items: [
      { path: '/entity-merge', label: '实体消歧', icon: Aim },
      { path: '/review/identity-merge', label: '身份合并提案', icon: Link },
    ],
  },
  {
    title: '实体归并',
    items: [
      { path: '/review/merge-workbench', label: '实体身份合并', icon: Switch },
      { path: '/review/field-conflict', label: '属性冲突处理', icon: Setting },
    ],
  },
  {
    title: '同步',
    items: [
      { path: '/weknora', label: 'WeKnora 同步', icon: FolderOpened },
      { path: '/neo4j', label: 'Neo4j 图推送', icon: Share },
    ],
  },
]
</script>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }

/* ── 整体布局 ── */
.app-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

/* ── 侧边栏 ── */
.sidebar {
  width: 210px;
  min-width: 210px;
  background: var(--dp-sidebar-bg);
  display: flex;
  flex-direction: column;
  transition: width 0.22s ease, min-width 0.22s ease;
  overflow: hidden;
  position: relative;
  z-index: 20;
  flex-shrink: 0;
}
.sidebar.collapsed {
  width: 64px;
  min-width: 64px;
}

/* Logo */
.sidebar-logo {
  height: 64px;
  display: flex;
  align-items: center;
  padding: 0 14px 0 20px;
  border-bottom: 1px solid var(--dp-sidebar-border);
  flex-shrink: 0;
}
.logo-text {
  display: flex;
  align-items: center;
  gap: 12px;
  white-space: nowrap;
  overflow: hidden;
}
.logo-icon {
  font-size: 20px;
  color: var(--dp-sidebar-ring);
  flex-shrink: 0;
  line-height: 1;
  width: 20px;
  text-align: center;
}
.logo-name {
  color: var(--dp-sidebar-fg);
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

/* 导航 */
.sidebar-nav {
  flex: 1;
  padding: 14px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow-y: auto;
  overflow-x: hidden;
}

.nav-group-title {
  font-size: 11px;
  color: var(--dp-sidebar-nav-inactive);
  opacity: 0.7;
  padding: 10px 12px 4px;
  letter-spacing: 0.5px;
  white-space: nowrap;
  flex-shrink: 0;
}
.nav-group-divider {
  height: 1px;
  margin: 6px 12px;
  background: var(--dp-sidebar-border);
  flex-shrink: 0;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 14px;
  border-radius: var(--dp-radius-md);
  color: var(--dp-sidebar-nav-inactive);
  text-decoration: none;
  font-size: 14px;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
  position: relative;
  cursor: pointer;
  flex-shrink: 0;
}
.nav-item:hover {
  background: var(--dp-sidebar-nav-hover);
  color: var(--dp-sidebar-nav-hover-fg);
}
.nav-item.active {
  background: var(--dp-sidebar-primary);
  color: var(--dp-sidebar-fg);
}

.nav-icon {
  font-size: 18px;
  flex-shrink: 0;
}
.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 14px;
}

/* 收起时的悬浮 tooltip */
.nav-tooltip {
  display: none;
  position: absolute;
  left: calc(100% + 10px);
  top: 50%;
  transform: translateY(-50%);
  background: var(--dp-primary);
  color: var(--dp-sidebar-fg);
  font-size: 13px;
  padding: 6px 12px;
  border-radius: var(--dp-radius-sm);
  white-space: nowrap;
  pointer-events: none;
  box-shadow: 0 2px 8px rgba(0,0,0,0.3);
  z-index: 100;
}
.nav-tooltip::before {
  content: '';
  position: absolute;
  right: 100%;
  top: 50%;
  transform: translateY(-50%);
  border: 5px solid transparent;
  border-right-color: var(--dp-sidebar-bg);
}
.sidebar.collapsed .nav-item:hover .nav-tooltip {
  display: block;
}

/* ── 主内容区 ── */
.main-wrapper {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.top-header {
  height: auto;
  min-height: 68px;
  background: var(--dp-card);
  border-bottom: 1px solid var(--dp-border);
  display: flex;
  align-items: center;
  padding: 12px 24px;
  flex-shrink: 0;
}
.header-title-row {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.page-title {
  font-size: 20px;
  font-weight: 700;
  color: var(--dp-text-regular);
  line-height: 1.2;
}
.page-desc {
  font-size: 13px;
  color: var(--dp-text-secondary);
  line-height: 1.3;
}

.main-content {
  flex: 1;
  background: var(--dp-bg);
  padding: 20px;
  overflow-y: auto;
}
</style>
