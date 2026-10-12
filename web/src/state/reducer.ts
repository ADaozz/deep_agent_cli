import { dictionary } from './dictionary';
import type { AgentEvent, Run, RunSseFrame } from '../api/types';
export const terminal = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
export interface ToolView {
    id: string;
    name: string;
    arguments: Record<string, unknown>;
    status: string;
    stdout: string;
    stderr: string;
    result: string;
    artifact?: unknown;
}
export interface RunView {
    snapshot: Run | null;
    cursor: number;
    answer: string;
    tools: Record<string, ToolView>;
    diagnostic: string;
    connected: boolean;
    todos: unknown[];
    usage: Record<string, unknown>;
}
export const newRunView = (): RunView => ({ snapshot: null, cursor: 0, answer: '', tools: dictionary<ToolView>(), diagnostic: '', connected: false, todos: [], usage: {} });
export function snapshot(view: RunView, run: Run) {
    if (view.snapshot && view.snapshot.id !== run.id)
        throw new Error('Run mismatch');
    if (view.snapshot && (view.snapshot.updated_at > run.updated_at || (terminal.has(view.snapshot.status) && !terminal.has(run.status))))
        return;
    view.snapshot = run;
    if (terminal.has(run.status))
        view.answer = run.result.output ?? '';
}
export function applyFrame(view: RunView, runId: string, frame: RunSseFrame) {
    if (!Number.isSafeInteger(frame.id) || frame.id <= view.cursor)
        return false;
    if (['run_started', 'interaction_requested', 'run_finished'].includes(frame.event)) {
        const run = frame.data as Run;
        if (run.id !== runId || typeof run.status !== 'string')
            throw new Error('Invalid Run event');
        snapshot(view, run);
    }
    else if (frame.event === 'interaction_submitted') {
        const data = frame.data as {
            interrupt_id: string;
        };
        if (view.snapshot?.interrupt_id === data.interrupt_id && !terminal.has(view.snapshot.status))
            view.snapshot = { ...view.snapshot, status: 'running', interrupt_id: null, result: {} };
    }
    else if (frame.event === 'delta') {
        const data = frame.data as {
            kind: string;
            text: string;
        };
        if (typeof data.text !== 'string')
            throw new Error('Invalid delta');
        if (!view.snapshot || !terminal.has(view.snapshot.status)) {
            if (data.kind === 'assistant')
                view.answer = data.text; // actual Runner callback is cumulative
            else if (data.kind === 'answer')
                view.answer += data.text; // documented incremental fixture
        }
    }
    else if (frame.event === 'agent_event') {
        const data = frame.data as AgentEvent;
        if (data.type === 'todos_updated' && Array.isArray(data.result))
            view.todos = data.result;
        if (data.type === 'usage' && data.result && typeof data.result === 'object' && !Array.isArray(data.result))
            view.usage = data.result as Record<string, unknown>;
        if (data.tool_call_id && ['tool_started', 'tool_completed', 'tool_output_delta'].includes(data.type)) {
            const tool = view.tools[data.tool_call_id] ?? { id: data.tool_call_id, name: data.name || 'tool', arguments: {}, status: 'running', stdout: '', stderr: '', result: '' };
            if (data.name)
                tool.name = data.name;
            if (data.type === 'tool_started')
                tool.arguments = data.arguments || {};
            if (data.type === 'tool_output_delta') {
                if (data.stream === 'stderr')
                    tool.stderr += data.content || '';
                else
                    tool.stdout += data.content || '';
            }
            if (data.type === 'tool_completed') {
                tool.status = data.is_error ? 'failed' : 'completed';
                tool.result = data.content || '';
                tool.artifact = data.artifact;
            }
            view.tools[data.tool_call_id] = tool;
        }
    }
    view.cursor = frame.id;
    return true;
}
