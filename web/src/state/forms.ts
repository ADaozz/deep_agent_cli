import { dictionary } from './dictionary';
import type { HumanInputRequest, PendingToolCall, ToolDecision } from '../api/types';
export function approvalBody(pending: PendingToolCall[], choices: Record<string, 'approve' | 'reject'>): ToolDecision[] {
    if (!pending.length || pending.some(call => !['approve', 'reject'].includes(choices[call.toolCallId] ?? '')))
        throw new Error('请对全部工具明确作出决定');
    return pending.map(call => ({ tool_call_id: call.toolCallId, decision: choices[call.toolCallId]! }));
}
export function humanValues(request: HumanInputRequest, source: Record<string, unknown>, custom: Record<string, string>) {
    const values = dictionary<unknown>();
    for (const field of request.fields) {
        if (!['text', 'textarea', 'single_select', 'multi_select', 'boolean'].includes(field.type))
            throw new Error('暂不支持此字段类型');
        let value = source[field.id];
        const other = field.options?.find(option => option.allowsCustomText)?.value;
        if (other && (value === other || (Array.isArray(value) && value.includes(other)))) {
            const text = custom[field.id]?.trim();
            if (!text)
                throw new Error(`请填写 ${field.label} 的其他回答`);
            value = Array.isArray(value) ? value.map(v => v === other ? text : v) : text;
        }
        if (field.required && (value === undefined || value === null || (typeof value === 'string' && !value.trim()) || (Array.isArray(value) && !value.length)))
            throw new Error(`请填写必填项：${field.label}`);
        if (value !== undefined)
            values[field.id] = value;
    }
    return values;
}
