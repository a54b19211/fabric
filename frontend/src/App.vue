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

      <!-- 导航菜单 -->
      <nav class="sidebar-nav">
        <router-link
          v-for="item in navItems"
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
      </nav>
    </aside>

    <!-- 主内容区：点击任意位置收起侧边栏 -->
    <div class="main-wrapper" @click="collapsed = true">
      <header class="top-header">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item :to="{ path: '/' }">首页</el-breadcrumb-item>
          <el-breadcrumb-item>{{ $route.meta.title }}</el-breadcrumb-item>
        </el-breadcrumb>
      </header>
      <main class="main-content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Document, Connection, Link, Setting, Cpu, Select } from '@element-plus/icons-vue'

// 默认收起，鼠标移入侧边栏展开
const collapsed = ref(true)

const navItems = [
  { path: '/documents',              label: '文档列表',     icon: Document },
  { path: '/graph-review',           label: '图谱审核',     icon: Connection },
  { path: '/entity-merge',           label: '实体消歧',     icon: Cpu },
  { path: '/review/merge-workbench', label: '实体身份合并', icon: Link },
  { path: '/review/field-conflict',  label: '属性冲突处理', icon: Setting },
  { path: '/review/identity-merge', label: '身份合并提案', icon: Link },
  { path: '/neo4j',                  label: 'Neo4j 图推送', icon: Connection },
  { path: '/weknora',                label: 'WeKnora 同步', icon: Connection },
  { path: '/agent',                  label: '自动审核',     icon: Cpu },
  { path: '/agent/document-review', label: '文档审核',     icon: Select },
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
  width: 200px;
  min-width: 200px;
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
  padding: 16px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow: hidden;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 13px 14px;
  border-radius: var(--dp-radius-md);
  color: var(--dp-sidebar-nav-inactive);
  text-decoration: none;
  font-size: 15px;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
  position: relative;
  cursor: pointer;
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
  font-size: 20px;
  flex-shrink: 0;
}
.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 15px;
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
  height: 56px;
  background: var(--dp-card);
  border-bottom: 1px solid var(--dp-border);
  display: flex;
  align-items: center;
  padding: 0 24px;
  flex-shrink: 0;
}

.main-content {
  flex: 1;
  background: var(--dp-bg);
  padding: 20px;
  overflow-y: auto;
}
</style>
