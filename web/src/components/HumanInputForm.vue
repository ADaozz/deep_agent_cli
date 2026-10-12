<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import Icon from './Icon.vue';
import type { HumanInputRequest, HumanInputBody, HumanField } from '../api/types';
import { dictionary } from '../state/dictionary';
import { humanValues } from '../state/forms';
const props = defineProps<{
    request: HumanInputRequest;
    interruptId: string;
    busy: boolean;
}>();
const emit = defineEmits<{
    submit: [
        body: HumanInputBody
    ];
}>();
const values = ref(dictionary<unknown>()), custom = ref(dictionary<string>()), error = ref('');
const unsupported = computed(() => props.request.fields.some(f => !['text', 'textarea', 'single_select', 'multi_select', 'boolean'].includes(f.type)));
watch(() => props.interruptId, () => { values.value = dictionary<unknown>(); custom.value = dictionary<string>(); error.value = ''; });
function textInput(field: HumanField, event: Event) { values.value[field.id] = (event.target as HTMLInputElement).value; }
function choose(field: HumanField, event: Event) { const value = (event.target as HTMLSelectElement).value; values.value[field.id] = field.type === 'boolean' ? (value === '' ? undefined : value === 'true') : value; }
function checked(field: HumanField, value: string) { return Array.isArray(values.value[field.id]) && (values.value[field.id] as string[]).includes(value); }
function toggle(field: HumanField, value: string, event: Event) { const list = Array.isArray(values.value[field.id]) ? [...(values.value[field.id] as string[])] : []; values.value[field.id] = (event.target as HTMLInputElement).checked ? [...list, value] : list.filter(v => v !== value); }
function needsCustom(field: HumanField) { return field.options?.some(o => o.allowsCustomText && (values.value[field.id] === o.value || checked(field, o.value))); }
function submit() { if (props.busy)
    return; error.value = ''; try {
    emit('submit', { interrupt_id: props.interruptId, values: humanValues(props.request, values.value, custom.value) });
}
catch (e) {
    error.value = (e as Error).message;
} }
</script>
<template><form class="approval-card human-form" aria-label="补充回答" @submit.prevent="submit"><h2><Icon name="alert" />{{ request.title||'Agent 需要补充信息' }}</h2><p>{{ request.question }}</p><p v-if="request.reason" class="muted">{{ request.reason }}</p><fieldset :disabled="busy"><div v-for="field in request.fields" :key="field.id" class="human-field"><label :for="'human-'+field.id">{{ field.label }}{{ field.required?' *':'' }}</label><input v-if="field.type==='text'" :id="'human-'+field.id" :value="String(values[field.id]??'')" :placeholder="field.placeholder" type="text" @input="textInput(field,$event)"><textarea v-else-if="field.type==='textarea'" :id="'human-'+field.id" :value="String(values[field.id]??'')" :placeholder="field.placeholder" @input="textInput(field,$event)"></textarea><select v-else-if="field.type==='single_select'||field.type==='boolean'" :id="'human-'+field.id" :value="values[field.id]===undefined?'':String(values[field.id])" @change="choose(field,$event)"><option value="">请选择</option><template v-if="field.type==='boolean'"><option value="true">是</option><option value="false">否</option></template><template v-else><option v-for="option in field.options" :key="option.value" :value="option.value">{{ option.label }}</option></template></select><fieldset v-else-if="field.type==='multi_select'" :id="'human-'+field.id" :aria-label="field.label"><label v-for="option in field.options" :key="option.value" class="check-option"><input type="checkbox" :value="option.value" :checked="checked(field,option.value)" @change="toggle(field,option.value,$event)">{{ option.label }}</label></fieldset><p v-else class="error">暂不支持此字段类型，无法提交</p><label v-if="needsCustom(field)">其他回答<input v-model="custom[field.id]" type="text" :aria-label="field.label+'的其他回答'"></label></div></fieldset><p v-if="error" role="alert" class="error">{{ error }}</p><div class="approval-footer"><button class="decision approve" :disabled="busy||unsupported" type="submit">提交回答</button></div></form></template>
