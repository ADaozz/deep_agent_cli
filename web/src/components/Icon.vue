<script setup lang="ts">
defineProps<{
    name: string;
}>();
</script>
<template><svg class="icon" aria-hidden="true"><use :href="'#i-'+name"></use></svg></template>
