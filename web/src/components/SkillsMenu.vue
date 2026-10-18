<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { Capabilities, SkillReference } from '../api/types'
import { apiConfig } from '../store/settings'
import Icon from './Icon.vue'
const props = defineProps<{ projectId: string; disabled: boolean }>()
const emit = defineEmits<{ back: []; selected: [SkillReference] }>()
const catalog = ref<Capabilities>()
const loading = ref(false)
const error = ref('')
let generation = 0
watch(() => props.projectId, async project => {
  const version = ++generation
  catalog.value = undefined; error.value = ''
  if (!project) return
  loading.value = true
  try {
    const result = await api.capabilities(apiConfig(), project)
    if (version === generation) catalog.value = result
  } catch (e) { if (version === generation) error.value = e instanceof Error ? e.message : '无法加载 Skills' }
  finally { if (version === generation) loading.value = false }
}, { immediate: true })
onBeforeUnmount(() => { generation++ })
</script>
<template>
  <button class="pop-item" type="button" @click="emit('back')"><Icon name="chevronLeft" :size="11" /><span>返回</span></button>
  <div class="pop-head">Skills</div>
  <p v-if="loading" class="pop-note">正在加载 Skills…</p>
  <button v-for="skill in catalog?.skills || []" :key="skill.path" class="pop-item" type="button" :title="skill.description" :disabled="disabled" @click="emit('selected', skill)"><span class="grow">{{ skill.name }}</span></button>
  <p v-if="catalog && !catalog.skills.length" class="pop-note">没有可用的 Skill</p>
  <p v-for="warning in catalog?.warnings || []" :key="warning" class="pop-note">{{ warning }}</p>
  <p v-if="error" class="pop-note model-error" role="alert">{{ error }}</p>
</template>
