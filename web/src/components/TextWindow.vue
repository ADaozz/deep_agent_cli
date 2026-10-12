<script setup lang="ts">
import {computed,ref} from 'vue'
const props=defineProps<{text:string}>()
const size=20_000,offset=ref<number|null>(null)
const start=computed(()=>Math.max(0,Math.min(offset.value??props.text.length-size,Math.max(0,props.text.length-size))))
const visible=computed(()=>{let from=start.value,to=Math.min(props.text.length,from+size);if(from>0&&props.text.charCodeAt(from)>=0xDC00&&props.text.charCodeAt(from)<=0xDFFF)from--;if(to<props.text.length&&props.text.charCodeAt(to-1)>=0xD800&&props.text.charCodeAt(to-1)<=0xDBFF)to++;return props.text.slice(from,to)})
function previous(){offset.value=Math.max(0,start.value-size)}
function next(){const value=Math.min(Math.max(0,props.text.length-size),start.value+size);offset.value=value===Math.max(0,props.text.length-size)?null:value}
</script>
<template><div class="text-window"><pre>{{ visible }}</pre><div v-if="text.length>size" class="log-pages"><button type="button" class="quiet-button" :disabled="start===0" @click="previous">上一页日志</button><span class="dim">窗口显示，完整来源保留</span><button type="button" class="quiet-button" :disabled="start+size>=text.length" @click="next">下一页日志</button></div></div></template>
