<template>
  <el-container class="app-container">
    <el-aside width="200px" class="sidebar">
      <div class="logo">ZK Data Fabric</div>
      <el-menu
        :default-active="$route.path"
        router
        background-color="#1a1a2e"
        text-color="#ccc"
        active-text-color="#409eff"
      >
        <el-menu-item index="/documents">
          <el-icon><Document /></el-icon>
          <span>文档列表</span>
        </el-menu-item>
        <el-menu-item index="/neo4j">
          <el-icon><Share /></el-icon>
          <span>Neo4j 推送</span>
        </el-menu-item>
        <el-menu-item index="/weknora">
          <el-icon><Connection /></el-icon>
          <span>WeKnora 同步</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item :to="{ path: '/' }">首页</el-breadcrumb-item>
          <el-breadcrumb-item>{{ $route.meta.title }}</el-breadcrumb-item>
        </el-breadcrumb>
      </el-header>
      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }

.app-container { height: 100vh; }

.sidebar {
  background: #1a1a2e;
  display: flex;
  flex-direction: column;
}

.logo {
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  padding: 20px 16px;
  border-bottom: 1px solid #2a2a4a;
  letter-spacing: 0.5px;
}

.header {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  padding: 0 20px;
}

.main-content {
  background: #f5f7fa;
  padding: 20px;
  overflow-y: auto;
}

.el-menu { border-right: none !important; }
</style>
