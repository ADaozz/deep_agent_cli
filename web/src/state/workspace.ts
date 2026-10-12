import { dictionary } from './dictionary';
import { reactive, computed } from 'vue';
import { api, ApiError, streamRun } from '../api/http';
import type { Run, Session, Project, RunSummary } from '../api/types';
import { applyFrame, newRunView, snapshot, terminal, type RunView } from './reducer';
export function useWorkspace() {
    const state = reactive({ projects: [] as Project[], sessions: [] as Session[], history: [] as RunSummary[], nextCursor: null as string | null, project: '', session: '', runId: '', views: dictionary<RunView>(), prompts: dictionary<string>(), busy: false, error: '', health: false, interactionRevision: 0, projectActive: null as RunSummary | null, blocked: false });
    let generation = 0;
    let controller: AbortController | null = null;
    const view = computed(() => state.views[state.runId]);
    const run = computed(() => view.value?.snapshot);
    const active = computed(() => !!run.value && !terminal.has(run.value.status));
    const occupied = computed(() => active.value || state.projectActive !== null || state.blocked);
    function stop() { generation++; controller?.abort(); controller = null; }
    function remember() { localStorage.setItem('deep-agent.selection', JSON.stringify({ project: state.project, session: state.session, run: state.runId })); }
    function error(e: unknown) { state.error = e instanceof ApiError ? `${e.status} · ${e.message}` : e instanceof Error ? e.message : '连接失败'; }
    async function discovery(project = state.project) {
        if (!project)
            return;
        const version = generation;
        const result = await api.activeRun(project);
        if (version === generation && state.project === project) {
            state.projectActive = result.run;
            state.blocked = !!result.blocked;
        }
    }
    async function follow(initial: Run) {
        stop();
        const version = generation;
        state.runId = initial.id;
        if (!state.views[initial.id])
            state.views[initial.id] = newRunView();
        const target = state.views[initial.id]!;
        snapshot(target, initial);
        remember();
        controller = new AbortController();
        const signal = controller.signal;
        void (async () => {
            let delay = 500;
            while (!signal.aborted && version === generation) {
                try {
                    const current = await api.getRun(initial.id);
                    if (signal.aborted)
                        return;
                    snapshot(target, current);
                    await streamRun(initial.id, target.cursor, signal, frame => {
                        applyFrame(target, initial.id, frame);
                        if (frame.event === 'interaction_rejected') {
                            target.diagnostic = '回答被拒绝，请检查后重新提交';
                            void api.getRun(initial.id).then(r => snapshot(target, r)).catch(e => { if (version === generation)
                                error(e); });
                        }
                        if (target.snapshot && terminal.has(target.snapshot.status) && state.projectActive?.id === initial.id)
                            state.projectActive = null;
                    }, () => { target.connected = true; if (!target.diagnostic.startsWith('回答被拒绝'))
                        target.diagnostic = ''; delay = 500; });
                    target.connected = false;
                    if (target.snapshot && terminal.has(target.snapshot.status)) {
                        void discovery().catch(error);
                        return;
                    }
                    target.diagnostic = '连接已关闭，任务可能仍在运行；正在重连';
                }
                catch (e) {
                    target.connected = false;
                    if (signal.aborted)
                        return;
                    target.diagnostic = e instanceof Error ? `连接中断：${e.message}` : '连接中断';
                    if (e instanceof ApiError && [401, 404].includes(e.status)) {
                        if (version === generation)
                            error(e);
                        return;
                    }
                }
                await new Promise<void>(resolve => { const timer = setTimeout(resolve, delay); signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true }); });
                delay = Math.min(delay * 2, 8000);
            }
        })();
    }
    async function selectSession(session: Session, preferred?: string) {
        stop();
        const version = generation;
        state.session = session.id;
        state.runId = '';
        state.history = [];
        state.nextCursor = null;
        state.error = '';
        remember();
        try {
            const page = await api.history(session.id);
            if (version !== generation)
                return;
            state.history = page.runs;
            state.nextCursor = page.next_cursor;
            const selected = preferred ?? page.runs[0]?.id;
            if (selected) {
                const item = await api.getRun(selected);
                if (version !== generation)
                    return;
                if (item.session_id !== session.id || item.project_id !== state.project)
                    throw new Error('任务归属不匹配');
                await follow(item);
            }
        }
        catch (e) {
            if (version === generation)
                error(e);
        }
    }
    async function selectProject(project: string) {
        stop();
        const version = generation;
        state.project = project;
        state.session = '';
        state.runId = '';
        state.sessions = [];
        state.history = [];
        state.projectActive = null;
        state.blocked = false;
        state.error = '';
        try {
            const [sessions, live] = await Promise.all([api.sessions(project), api.activeRun(project)]);
            if (version !== generation)
                return;
            state.sessions = sessions.sessions;
            state.projectActive = live.run;
            state.blocked = !!live.blocked;
            remember();
            if (live.run) {
                let session = state.sessions.find(s => s.id === live.run!.session_id);
                if (!session) {
                    session = await api.session(live.run.session_id);
                    if (version !== generation)
                        return;
                    state.sessions = [session, ...state.sessions].slice(0, 100);
                }
                await selectSession(session, live.run.id);
            }
            else if (state.sessions[0])
                await selectSession(state.sessions[0]);
        }
        catch (e) {
            if (version === generation)
                error(e);
        }
    }
    async function createSession() {
        if (state.busy || !state.project)
            return;
        state.busy = true;
        state.error = '';
        const project = state.project, version = generation;
        try {
            const session = await api.createSession(project);
            if (version !== generation)
                return;
            state.sessions.unshift(session);
            await selectSession(session);
        }
        catch (e) {
            if (project === state.project)
                error(e);
        }
        finally {
            state.busy = false;
        }
    }
    async function send(text: string) {
        if (state.busy || occupied.value || !text.trim() || !state.project)
            return;
        state.busy = true;
        state.error = '';
        const project = state.project, version = generation;
        let sessionId = state.session;
        try {
            if (!sessionId) {
                const session = await api.createSession(project);
                if (version !== generation)
                    return;
                state.sessions.unshift(session);
                state.session = session.id;
                sessionId = session.id;
            }
            const result = await api.submit(sessionId, text);
            state.prompts[result.id] = text;
            if (version !== generation)
                return;
            state.projectActive = result;
            state.history.unshift(result);
            await follow(result);
        }
        catch (e) {
            if (project === state.project) {
                error(e);
                void discovery().catch(error);
            }
        }
        finally {
            state.busy = false;
        }
    }
    async function act(request: (current: Run) => Promise<Run>) {
        if (state.busy || !run.value)
            return;
        const current = run.value, version = generation;
        state.busy = true;
        state.error = '';
        try {
            snapshot(state.views[current.id]!, await request(current));
            state.views[current.id]!.diagnostic = '';
        }
        catch (e) {
            if (version === generation) {
                error(e);
                if (e instanceof ApiError && e.status === 409) {
                    state.interactionRevision++;
                    try {
                        snapshot(state.views[current.id]!, await api.getRun(current.id));
                    }
                    catch (refresh) {
                        error(refresh);
                    }
                }
            }
        }
        finally {
            state.busy = false;
        }
    }
    async function resume() { if (state.busy || !state.session)
        return; state.busy = true; state.error = ''; const id = state.session, version = generation; try {
        const item = await api.resume(id);
        if (version === generation) {
            state.projectActive = item;
            state.history.unshift(item);
            await follow(item);
        }
    }
    catch (e) {
        if (version === generation)
            error(e);
    }
    finally {
        state.busy = false;
    } }
    async function selectRun(id: string) { const version = generation; try {
        const item = await api.getRun(id);
        if (version === generation && item.session_id === state.session)
            await follow(item);
    }
    catch (e) {
        if (version === generation)
            error(e);
    } }
    async function moreHistory() { if (!state.nextCursor)
        return; const version = generation; try {
        const page = await api.history(state.session, state.nextCursor);
        if (version === generation) {
            state.history.push(...page.runs);
            state.nextCursor = page.next_cursor;
        }
    }
    catch (e) {
        error(e);
    } }
    async function reconnect() { try {
        await discovery();
        if (run.value)
            await follow(await api.getRun(run.value.id));
        else
            await init();
    }
    catch (e) {
        error(e);
    } }
    async function openActive() { const item = state.projectActive; if (!item)
        return; const session = state.sessions.find(s => s.id === item.session_id); if (session)
        await selectSession(session, item.id); }
    async function init() {
        try {
            const [projects, health] = await Promise.all([api.projects(), api.health()]);
            state.projects = projects.projects;
            state.health = health.status === 'ok';
            let saved: {
                project?: string;
                session?: string;
                run?: string;
            } = {};
            try {
                saved = JSON.parse(localStorage.getItem('deep-agent.selection') || '{}') as typeof saved;
            }
            catch { /* IDs are optional; never store secrets here. */ }
            const project = state.projects.find(p => p.id === saved.project)?.id ?? state.projects[0]?.id;
            if (project) {
                await selectProject(project);
                const selected = state.sessions.find(s => s.id === saved.session);
                if (selected)
                    await selectSession(selected, saved.run);
            }
        }
        catch (e) {
            error(e);
        }
    }
    return { state, view, run, active, occupied, init, stop, selectProject, selectSession, selectRun, moreHistory, openActive, createSession, send, act, resume, reconnect };
}
