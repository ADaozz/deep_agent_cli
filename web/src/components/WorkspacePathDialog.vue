<script setup lang="ts">
import { onMounted, ref, onBeforeUnmount } from 'vue'
import { api, ApiError } from '../api/client'
import type { DirectoryListing, Workspace } from '../api/types'
import { apiConfig } from '../store/settings'
import Icon from './Icon.vue'

const props = defineProps<{ initialPath?: string }>()
const emit = defineEmits<{ close: []; selected: [workspace: Workspace] }>()
const path = ref(props.initialPath || '')
const listing = ref<DirectoryListing>()
const loading = ref(false)
const saving = ref(false)
const error = ref('')
let generation = 0
async function browse(value = path.value): Promise<void> {
  const version = ++generation
  loading.value = true; error.value = ''
  try {
    const result = await api.directories(apiConfig(), value.trim() || undefined)
    if (version === generation) { listing.value = result; path.value = result.path }
  } catch (e) { if (version === generation) error.value = e instanceof Error ? e.message : '无法读取目录' }
  finally { if (version === generation) loading.value = false }
}
async function select(): Promise<void> {
  if (saving.value || loading.value || !path.value.trim()) return
  saving.value = true; error.value = ''
  try { emit('selected', await api.selectWorkspace(apiConfig(), path.value.trim())) }
  catch (e) { error.value = e instanceof ApiError ? e.message : '无法选择该 workspace' }
  finally { saving.value = false }
}
function onKey(event: KeyboardEvent): void { if (event.key === 'Escape' && !saving.value) emit('close') }
onMounted(() => { document.addEventListener('keydown', onKey); void browse() })
onBeforeUnmount(() => { generation++; document.removeEventListener('keydown', onKey) })
</script>

<template>
  <div class="scrim" @click.self="!saving && emit('close')">
    <section class="dialog" role="dialog" aria-modal="true" aria-labelledby="workspace-path-title">
      <header class="dialog-head">
        <h2 id="workspace-path-title">选择 workspace 路径</h2>
        <button class="ico" aria-label="关闭路径选择" :disabled="saving" @click="emit('close')"><Icon name="close" :size="14" /></button>
      </header>
      <div class="dialog-body">
        <form class="workspace-path-form" @submit.prevent="browse()">
          <label for="workspace-path">目录路径</label>
          <div class="field-row field">
            <input id="workspace-path" v-model="path" :disabled="saving || loading" autocomplete="off" spellcheck="false" />
            <button class="btn" :disabled="loading || saving" type="submit">打开</button>
          </div>
        </form>
        <div class="workspace-folders">
          <button v-if="listing?.parent" class="pop-item" :disabled="loading || saving" @click="browse(listing!.parent!)"><Icon name="chevronLeft" :size="13" />上一级</button>
          <button v-for="directory in listing?.directories || []" :key="directory.path" class="pop-item" :disabled="loading || saving" @click="browse(directory.path)"><Icon name="folder" :size="14" /><span class="grow">{{ directory.name }}</span><Icon name="chevronRight" :size="11" /></button>
          <p v-if="loading" class="help">正在读取目录…</p>
          <p v-else-if="listing && !listing.directories.length" class="help">此目录没有可显示的子文件夹</p>
          <p v-if="listing?.truncated" class="help">目录较多，可输入完整路径直接打开。</p>
        </div>
        <p v-if="error" role="alert" class="model-error">{{ error }}</p>
      </div>
      <footer class="dialog-foot">
        <span class="grow">{{ listing?.path }}</span>
        <button class="btn" :disabled="saving" @click="emit('close')">取消</button>
        <button class="btn is-primary" :disabled="loading || saving || !path.trim()" @click="select">{{ saving ? '正在选择…' : '选择此目录' }}</button>
      </footer>
    </section>
  </div>
</template>
