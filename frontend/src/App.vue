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
import { Document, Share, Connection } from '@element-plus/icons-vue'

// 默认收起，鼠标移入侧边栏展开
const collapsed = ref(true)

const navItems = [
  { path: '/documents', label: '文档列表', icon: Document },
  { path: '/neo4j',     label: 'Neo4j 推送', icon: Share },
  { path: '/weknora',   label: 'WeKnora 同步', icon: Connection },
]
</script>

<style>
/* ── 全局主题色覆盖（深蓝 + 青色系） ── */
:root {
  --el-color-primary:           #1d6fa4;
  --el-color-primary-light-3:   #4a9bc7;
  --el-color-primary-light-5:   #6eb3d8;
  --el-color-primary-light-7:   #9dcce8;
  --el-color-primary-light-8:   #b8ddf0;
  --el-color-primary-light-9:   #d4edf8;
  --el-color-primary-dark-2:    #165a87;

  --el-color-success:           #0e9e8e;
  --el-color-success-light-3:   #3db8aa;
  --el-color-success-light-5:   #62ccc0;
  --el-color-success-light-7:   #93ddd7;
  --el-color-success-light-8:   #ade8e3;
  --el-color-success-light-9:   #d0f3f0;
  --el-color-success-dark-2:    #0b7f71;

  --el-color-warning:           #2b7eb0;
  --el-color-warning-light-3:   #5599c4;
  --el-color-warning-light-5:   #74afd3;
  --el-color-warning-light-7:   #9dc8e2;
  --el-color-warning-light-8:   #b5d6eb;
  --el-color-warning-light-9:   #d5eaf5;
  --el-color-warning-dark-2:    #22658e;

  --el-color-danger:            #c0392b;
  --el-color-danger-light-3:    #d4614f;
  --el-color-danger-light-5:    #e08070;
  --el-color-danger-light-7:    #eca89f;
  --el-color-danger-light-8:    #f2c0ba;
  --el-color-danger-light-9:    #f9ddd9;
  --el-color-danger-dark-2:     #9a2e22;

  --el-color-info:              #4a7fa5;
}

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
  background: #1e2235;
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
  padding: 0 18px;
  border-bottom: 1px solid rgba(255,255,255,0.07);
  flex-shrink: 0;
}
.logo-text {
  display: flex;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
  overflow: hidden;
}
.logo-icon {
  font-size: 22px;
  color: #4fc3c3;
  flex-shrink: 0;
  line-height: 1;
}
.logo-name {
  color: #fff;
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
  border-radius: 8px;
  color: rgba(255,255,255,0.55);
  text-decoration: none;
  font-size: 15px;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
  position: relative;
  cursor: pointer;
}
.nav-item:hover {
  background: rgba(255,255,255,0.07);
  color: rgba(255,255,255,0.9);
}
.nav-item.active {
  background: #1d6fa4;
  color: #fff;
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
  background: #2c3354;
  color: #fff;
  font-size: 13px;
  padding: 6px 12px;
  border-radius: 6px;
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
  border-right-color: #2c3354;
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
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  padding: 0 24px;
  flex-shrink: 0;
}

.main-content {
  flex: 1;
  background: #f0f2f8;
  padding: 20px;
  overflow-y: auto;
}
</style>
