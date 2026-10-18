<script setup lang="ts">
/**
 * Anchored popover: renders inside a `.anchor` wrapper (position: relative) and
 * closes itself on outside click or Escape. Styling lives in styles.css so the
 * menus stay identical to the reference menus (white, 8px radius, light shadow).
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

defineProps<{ align?: 'down' | 'up' | 'right'; end?: boolean }>()
const emit = defineEmits<{ close: [] }>()

const root = ref<HTMLElement | null>(null)

function onPointerDown(event: MouseEvent): void {
  const target = event.target
  if (!(target instanceof Node)) return
  if (root.value?.contains(target)) return
  emit('close')
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
}

onMounted(() => {
  document.addEventListener('mousedown', onPointerDown, true)
  document.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onPointerDown, true)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="root" class="popover" :class="[align ?? 'down', { 'is-end': end }]" @mousedown.stop>
    <slot />
  </div>
</template>
