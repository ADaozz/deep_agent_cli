import type { Project, Session, Run, ApprovalBody, HumanInputBody, RunSseFrame, PaginatedRuns, ActiveRunResponse, CodeCheckpoint } from './types';
import { SseParser } from './sse';
export class ApiError extends Error {
    constructor(public status: number, public detail: unknown) { super(typeof detail === 'string' ? detail : JSON.stringify(detail)); }
}
export async function json<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
    const response = await fetch('/api/agent' + path, { method, credentials: 'same-origin', headers: method === 'POST' ? { 'Content-Type': 'application/json' } : {}, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json() as {
        detail?: unknown;
    };
    if (!response.ok)
        throw new ApiError(response.status, data.detail ?? '请求失败');
    return data as T;
}
const id = encodeURIComponent;
export const api = {
    health: () => json<{
        status: string;
    }>('/health'),
    projects: () => json<{
        projects: Project[];
    }>('/projects'),
    session: (session: string) => json<Session>('/sessions/' + id(session)),
    sessions: (project: string) => json<{
        sessions: Session[];
    }>('/sessions?project_id=' + id(project)),
    history: (session: string, cursor?: string) => json<PaginatedRuns>('/sessions/' + id(session) + '/runs' + (cursor ? '?cursor=' + id(cursor) : '')),
    activeRun: (project: string) => json<ActiveRunResponse>('/projects/' + id(project) + '/active-run'),
    createSession: (project_id: string) => json<Session>('/sessions', 'POST', { project_id }),
    getRun: (run: string) => json<Run>('/runs/' + id(run)),
    codeCheckpoint: (run: string) => json<CodeCheckpoint>('/runs/' + id(run) + '/code-checkpoint'),
    restoreCode: (run: string, action: 'undo' | 'redo', revision: string) => json<CodeCheckpoint>('/runs/' + id(run) + '/code-restore', 'POST', { action, revision }),
    submit: (session: string, text: string) => json<Run>('/sessions/' + id(session) + '/runs', 'POST', { text }),
    resume: (session: string) => json<Run>('/sessions/' + id(session) + '/runs', 'POST', { resume: true }),
    approvals: (run: string, body: ApprovalBody) => json<Run>('/runs/' + id(run) + '/approvals', 'POST', body),
    human: (run: string, body: HumanInputBody) => json<Run>('/runs/' + id(run) + '/human-input', 'POST', body),
    continue: (run: string, interrupt_id: string) => json<Run>('/runs/' + id(run) + '/continue', 'POST', { interrupt_id }),
    cancel: (run: string) => json<Run>('/runs/' + id(run) + '/cancel', 'POST'),
};
export async function streamRun(run: string, after: number, signal: AbortSignal, receive: (frame: RunSseFrame) => void, opened: () => void, idleTimeoutMs = 40000) {
    const response = await fetch(`/api/agent/runs/${id(run)}/events?after=${after}`, { signal, headers: { Accept: 'text/event-stream' }, credentials: 'same-origin' });
    if (!response.ok) {
        const error = await response.json() as {
            detail: unknown;
        };
        throw new ApiError(response.status, error.detail);
    }
    if (!response.body)
        throw new Error('浏览器不支持流式响应');
    opened();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser(receive);
    try {
        while (true) {
            let timer: ReturnType<typeof setTimeout> | undefined;
            const result = await Promise.race([reader.read(), new Promise<never>((_resolve, reject) => { timer = setTimeout(() => { reject(new Error('事件连接长期无响应，正在重新对账')); void reader.cancel(); }, idleTimeoutMs); })]).finally(() => clearTimeout(timer));
            const { done, value } = result;
            if (done) {
                parser.push(decoder.decode());
                break;
            }
            parser.push(decoder.decode(value, { stream: true }));
        }
    }
    finally {
        await reader.cancel().catch(() => { });
        reader.releaseLock();
    }
}
