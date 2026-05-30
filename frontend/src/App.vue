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
/* ── 全局主题色覆盖（绿 + 橙红系） ── */
:root {
  --el-color-primary:           #28aa86;
  --el-color-primary-light-3:   #4dc7a4;
  --el-color-primary-light-5:   #78ddb9;
  --el-color-primary-light-7:   #a6f3d0;
  --el-color-primary-light-8:   #c5fae3;
  --el-color-primary-light-9:   #e2fdf2;
  --el-color-primary-dark-2:    #1e8c6f;

  --el-color-success:           #28aa86;
  --el-color-success-light-3:   #4dc7a4;
  --el-color-success-light-5:   #78ddb9;
  --el-color-success-light-7:   #a6f3d0;
  --el-color-success-light-8:   #c5fae3;
  --el-color-success-light-9:   #e2fdf2;
  --el-color-success-dark-2:    #1e8c6f;

  --el-color-warning:           #f08a3f;
  --el-color-warning-light-3:   #f6a96e;
  --el-color-warning-light-5:   #fac59c;
  --el-color-warning-light-7:   #fde1c7;
  --el-color-warning-light-8:   #fef0dc;
  --el-color-warning-light-9:   #fff7ed;
  --el-color-warning-dark-2:    #cc7130;

  --el-color-danger:            #ff5a3d;
  --el-color-danger-light-3:    #ff8064;
  --el-color-danger-light-5:    #ffaa91;
  --el-color-danger-light-7:    #ffd1c6;
  --el-color-danger-light-8:    #ffe7da;
  --el-color-danger-light-9:    #fff1ee;
  --el-color-danger-dark-2:     #d3432e;

  --el-color-info:              #4bc5a8;
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
  background: #178582;
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
  border-bottom: 1px solid rgba(255,255,255,0.07);
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
  color: #c5f9e7;
  flex-shrink: 0;
  line-height: 1;
  width: 20px;
  text-align: center;
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
  background: #1a9e93;
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
  background: #28aa86;
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
  border-right-color: #178582;
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
