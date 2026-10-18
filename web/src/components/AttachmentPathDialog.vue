<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { PathReference } from '../store/drafts'
import Icon from './Icon.vue'
const props = defineProps<{ kind: 'file' | 'image' }>()
const emit = defineEmits<{ close: []; selected: [paths: PathReference[]] }>()
const input = ref<HTMLTextAreaElement | null>(null)
const value = ref('')
function add(): void {
  const paths = value.value.split(/\r?\n/).map(path => path.trim()).filter(Boolean)
  if (paths.length) emit('selected', [...new Set(paths)].map(path => ({ path, kind: props.kind })))
}
function escape(event: KeyboardEvent): void { if (event.key === 'Escape') emit('close') }
onMounted(() => { input.value?.focus(); document.addEventListener('keydown', escape) })
onBeforeUnmount(() => document.removeEventListener('keydown', escape))
</script>
<template>
  <Teleport to="body">
    <div class="scrim" @click.self="emit('close')">
      <form class="dialog" role="dialog" aria-modal="true" aria-labelledby="attachment-path-title" @submit.prevent="add">
        <header class="dialog-head">
          <h2 id="attachment-path-title">{{ kind === 'image' ? '添加图片路径' : '添加文件路径' }}</h2>
          <button class="ico" type="button" aria-label="关闭附件路径" @click="emit('close')"><Icon name="close" :size="14" /></button>
        </header>
        <div class="dialog-body">
          <label for="attachment-path">{{ kind === 'image' ? '图片路径' : '文件路径' }}</label>
          <textarea id="attachment-path" ref="input" v-model="value" class="attachment-path-input" rows="4" placeholder="每行一个路径" spellcheck="false" />
          <p class="help">填写 Agent 可访问的绝对路径或相对 workspace 的路径。仅发送路径字符串，不上传文件内容。</p>
        </div>
        <footer class="dialog-foot">
          <button class="btn" type="button" @click="emit('close')">取消</button>
          <button class="btn is-primary" type="submit" :disabled="!value.trim()">添加</button>
        </footer>
      </form>
    </div>
  </Teleport>
</template>
