<script setup lang="ts">
/**
 * Two-screen shell, modelled on the real agent page:
 *   home   → centered composer + recent agents
 *   thread → the run log with a sticky composer and an optional changes panel
 * The sidebar and the connection dialog sit outside both screens.
 */
import { onMounted, onUnmounted, ref } from 'vue'
import AgentSidebar from './components/AgentSidebar.vue'
import HomeScreen from './components/HomeScreen.vue'
import Icon from './components/Icon.vue'
import SettingsDialog from './components/SettingsDialog.vue'
import ThreadScreen from './components/ThreadScreen.vue'
import { app, connect, goHome, initApp, resetRecords, stopStreams } from './store/app'

const sidebarOpen = ref(!window.matchMedia('(max-width: 860px)').matches)
const panelOpen = ref(false)

onMounted(() => {
  initApp()
})

onUnmounted(() => {
  stopStreams()
})

function closeSidebarOnNarrow(): void {
  sidebarOpen.value = false
}

function clearLocalData(): void {
  resetRecords()
  goHome()
  app.settingsOpen = false
}
</script>

<template>
  <div class="app">
    <button
      v-if="!sidebarOpen"
      class="sidebar-reveal"
      type="button"
      title="显示侧栏"
      @click="sidebarOpen = true"
    >
      <Icon name="panelLeft" :size="15" />
    </button>

    <button v-if="sidebarOpen" class="side-backdrop" aria-label="关闭侧栏" @click="sidebarOpen = false" />
    <AgentSidebar v-if="sidebarOpen" @collapse="closeSidebarOnNarrow" />

    <main class="main" :class="{ 'is-side-hidden': !sidebarOpen }">
      <HomeScreen v-if="app.view === 'home'" />
      <ThreadScreen v-else :panel-open="panelOpen" @toggle-panel="panelOpen = !panelOpen" />
    </main>

    <SettingsDialog
      :open="app.settingsOpen"
      :projects="app.projects"
      @close="app.settingsOpen = false"
      @saved="connect()"
      @clear-records="clearLocalData"
    />
  </div>
</template>
