<script setup lang="ts">
/**
 * Tiny stroke-only icon set (16x16 grid). Every glyph is a list of `path d`
 * strings, so nothing is ever injected as HTML; unknown names render nothing.
 */
const props = defineProps<{ name: string; size?: number; strokeWidth?: number }>()

const ICONS: Record<string, string[]> = {
  // chrome ---------------------------------------------------------------
  panelLeft: [
    'M3 2.5h10a.6.6 0 0 1 .6.6v9.8a.6.6 0 0 1-.6.6H3a.6.6 0 0 1-.6-.6V3.1a.6.6 0 0 1 .6-.6Z',
    'M6.4 2.5v11',
  ],
  panelRight: [
    'M3 2.5h10a.6.6 0 0 1 .6.6v9.8a.6.6 0 0 1-.6.6H3a.6.6 0 0 1-.6-.6V3.1a.6.6 0 0 1 .6-.6Z',
    'M9.6 2.5v11',
  ],
  search: ['M7.1 12.3a5.2 5.2 0 1 0 0-10.4 5.2 5.2 0 0 0 0 10.4Z', 'M10.9 11l2.3 2.3'],
  appearance: ['M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12Z', 'M8 2v12', 'M8 4h2', 'M8 6h4', 'M8 8h4', 'M8 10h4', 'M8 12h2'],
  dots: ['M3.1 8h.02', 'M8 8h.02', 'M12.9 8h.02'],
  close: ['M4.2 4.2l7.6 7.6', 'M11.8 4.2l-7.6 7.6'],
  maximize: ['M9.4 2.8h3.8v3.8', 'M6.6 13.2H2.8V9.4', 'M13.2 2.8 9.2 6.8', 'M2.8 13.2l4-4'],
  plus: ['M8 3.2v9.6', 'M3.2 8h9.6'],
  check: ['M3.2 8.4l3.1 3.1L12.8 4.9'],
  chevronDown: ['M4.6 6.4 8 9.8l3.4-3.4'],
  chevronUp: ['M4.6 9.6 8 6.2l3.4 3.4'],
  chevronRight: ['M6.2 4.2 10 8l-3.8 3.8'],
  chevronLeft: ['M9.8 4.2 6 8l3.8 3.8'],
  arrowUp: ['M8 12.8V3.4', 'M4.1 7.3 8 3.4l3.9 3.9'],
  refresh: ['M13 8a5 5 0 1 1-1.6-3.7', 'M13 2.6V5h-2.4'],
  gear: [
    'M8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z',
    'M3.4 8h.9M11.7 8h.9M8 3.4v.9M8 11.7v.9M4.9 4.9l.6.6M10.5 10.5l.6.6M11.1 4.9l-.6.6M5.5 10.5l-.6.6',
  ],
  // product --------------------------------------------------------------
  wand: ['M10.6 2.6l2.8 2.8', 'M12 5.4 4.5 12.9l-1.9.5.5-1.9 7.5-7.5z', 'M3.2 3.4l1 1', 'M2.6 8.4h1.4', 'M8.4 2.6v1.4'],
  code: ['M6.2 4.9 3 8l3.2 3.1', 'M9.8 4.9 13 8l-3.2 3.1'],
  terminal: ['M2.6 3.4h10.8v9.2H2.6z', 'M5.2 7l1.9 1.5-1.9 1.5', 'M8.8 10.6h2.4'],
  folder: ['M2 4.2h4.5l1.2 1.3H14v7.3H2z', 'M2 4.2V3h4.4l1.2 1.2'],
  image: ['M2.5 2.5h11v11h-11z', 'M2.5 10l3.2-3.2 3 3 2-2 2.8 2.8', 'M10.5 5h.01'],
  file: ['M4.2 2.4h4.9l2.7 2.7v8.5H4.2z', 'M9.1 2.4v2.7h2.7'],
  branch: [
    'M5 3.6a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2Z',
    'M11 9.2a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2Z',
    'M5 6.8v2.6a2 2 0 0 0 2 2h2.1',
  ],
  commit: ['M11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z', 'M1 8h4', 'M11 8h4'],
  stop: ['M5 5h6v6H5z'],
  copy: ['M5.6 5.6h7.2v7.2H5.6z', 'M10.4 5.6V3.2H3.2v7.2h2.4'],
  skill: ['M8 2.4l1.4 3.4 3.4 1.4-3.4 1.4L8 12l-1.4-3.4L3.2 7.2l3.4-1.4L8 2.4Z'],
  user: [
    'M8 8.1a2.3 2.3 0 1 0 0-4.6 2.3 2.3 0 0 0 0 4.6Z',
    'M3.6 13.3c.6-2.2 2.4-3.4 4.4-3.4s3.8 1.2 4.4 3.4',
  ],
  alert: ['M8 2.6 14 13H2L8 2.6Z', 'M8 6.6v3', 'M8 11.5h.01'],
  archive: ['M2.6 3.4h10.8v2.4H2.6z', 'M3.8 5.8v7.4h8.4V5.8', 'M6.6 8.4h2.8'],
}

const paths = () => ICONS[props.name] ?? []
</script>

<template>
  <svg
    class="icon"
    :width="size ?? 14"
    :height="size ?? 14"
    viewBox="0 0 16 16"
    fill="none"
    aria-hidden="true"
    focusable="false"
  >
    <path
      v-for="(d, index) in paths()"
      :key="index"
      :d="d"
      stroke="currentColor"
      :stroke-width="strokeWidth ?? 1.3"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
  </svg>
</template>

<style scoped>
.icon {
  display: block;
  flex: none;
}
</style>
