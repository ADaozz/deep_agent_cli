<script setup lang="ts">
import { fileReferenceNotice } from '../store/fileReferences'
import Icon from './Icon.vue'
</script>
<template>
  <Teleport to="body">
    <div v-if="fileReferenceNotice" class="file-reference-notice" role="alert">
      <span>{{ fileReferenceNotice }}</span>
      <button class="ico" type="button" aria-label="关闭文件提示" @click="fileReferenceNotice = ''"><Icon name="close" :size="13" /></button>
    </div>
  </Teleport>
</template>
