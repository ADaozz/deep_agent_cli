<script setup lang="ts">
/**
 * Assistant answer renderer. `renderMarkdown` escapes all source text and only
 * emits a fixed set of tags, which is what makes `v-html` acceptable here.
 */
import { computed } from 'vue'
import { renderMarkdown } from '../lib/markdown'

const props = defineProps<{ text: string }>()
const html = computed(() => renderMarkdown(props.text))
</script>

<template>
  <!-- Content is escaped by renderMarkdown before HTML is generated. -->
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div class="md" v-html="html" />
</template>
