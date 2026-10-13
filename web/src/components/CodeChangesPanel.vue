<script setup lang="ts">
import { computed } from 'vue';
import type { ToolView } from '../state/reducer';
import { fileChanges } from '../state/fileChanges';
import Icon from './Icon.vue';
import TextWindow from './TextWindow.vue';

const props = defineProps<{ tools: Record<string, ToolView> }>();
const changes = computed(() => fileChanges(props.tools));
const fileCount = computed(() => new Set(changes.value.map(change => change.path)).size);
const labels = { create: '新建', overwrite: '覆盖', write: '写入', edit: '编辑', delete: '删除' };
</script>

<template>
  <div class="code-changes" aria-label="文件变更记录">
    <template v-if="changes.length">
      <p class="preview-note">{{ fileCount }} 个文件 · {{ changes.length }} 次成功操作</p>
      <p class="preview-note">来自本任务文件工具事件，按操作展示；不是工作区完整 Git Diff。</p>
      <details v-for="change in changes" :key="change.id" class="file-change" :data-tool-call-id="change.id">
        <summary class="file-change-title">
          <Icon name="file" /><span class="file-path">{{ change.path }}</span>
          <span class="change-operation" :data-operation="change.operation">{{ labels[change.operation] }}</span>
        </summary>
        <template v-if="change.operation === 'edit'">
          <p class="preview-note">{{ change.replaceAll ? '替换所有匹配片段' : '替换片段' }}，不包含整文件上下文或实际行号。</p>
          <div v-if="change.before !== undefined" class="change-side change-before" data-change-side="before">
            <h3>替换前</h3><TextWindow :text="change.before || '（空片段）'" />
          </div>
          <div v-if="change.after !== undefined" class="change-side change-after" data-change-side="after">
            <h3>替换后</h3><TextWindow :text="change.after || '（空片段）'" />
          </div>
          <p v-if="change.before === undefined || change.after === undefined" class="preview-note">部分内容未记录，不能还原完整替换。</p>
        </template>
        <template v-else-if="change.operation !== 'delete'">
          <p v-if="change.operation !== 'create'" class="preview-note">{{ change.operation === 'overwrite' ? '覆盖写入；旧内容未记录。' : '新建或覆盖未确认；旧内容未记录。' }}</p>
          <div v-if="change.after !== undefined" class="change-side change-after" data-change-side="after">
            <h3>写入内容</h3><TextWindow :text="change.after || '（空文件）'" />
          </div>
          <p v-else class="preview-note">写入内容未记录。</p>
        </template>
        <p v-else class="preview-note">文件已删除；删除前内容未记录。</p>
      </details>
    </template>
    <p v-else class="preview-note">暂无成功的文件工具变更记录。</p>
    <p class="preview-note">文件工具详情不含命令改动；轮次回退范围以快照列表为准。</p>
  </div>
</template>
