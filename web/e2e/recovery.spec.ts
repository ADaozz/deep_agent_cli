import { test, expect, type Page } from '@playwright/test';
import path from 'node:path';
const origin = 'http://127.0.0.1:5173';
async function fresh(page: Page) { await page.goto('/'); await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible(); await page.getByRole('button', { name: '新建会话' }).click(); await expect(page.getByLabel('向 Agent 输入指令')).toBeEnabled(); }
async function send(page: Page, text: string) { await page.getByLabel('向 Agent 输入指令').fill(text); await page.getByRole('button', { name: '发送指令', exact: true }).click(); }
async function human(page: Page, name = 'valid task') { await page.getByLabel('任务名称 *').fill(name); await page.getByLabel('修改范围 *').selectOption('tests'); await page.getByLabel('允许继续 *').selectOption('false'); }
async function post(page: Page, route: string, body: unknown) { return page.request.post(origin + '/api/agent' + route, { data: body, headers: { Origin: origin, 'Content-Type': 'application/json' } }); }
test.afterEach(async ({ page }) => {
    for (const project of ['app', 'worker', 'third']) {
        const response = await page.request.get(origin + '/api/agent/projects/' + project + '/active-run');
        if (response.ok()) {
            const current = (await response.json()).run;
            if (current) {
                await post(page, `/runs/${current.id}/cancel`, undefined);
                await expect.poll(async () => { const r = await page.request.get(origin + '/api/agent/projects/' + project + '/active-run'); return (await r.json()).run; }).toBeNull();
            }
        }
    }
});
test('409 clears every old decision and a subsequent approval starts unselected', async ({ page }) => {
    await fresh(page);
    await send(page, 'two-approvals');
    await expect(page.locator('.approval-tool')).toHaveCount(2);
    let mutated = false;
    await page.route('**/api/agent/runs/*/approvals', async (route) => { const body = route.request().postDataJSON(); if (!mutated) {
        mutated = true;
        await route.continue({ postData: JSON.stringify({ ...body, interrupt_id: 'stale' }) });
    }
    else
        await route.continue(); });
    await page.getByRole('button', { name: '拒绝 execute', exact: true }).click();
    await page.getByRole('button', { name: '拒绝 write_file', exact: true }).click();
    await page.getByRole('button', { name: '提交决定' }).click();
    await expect(page.getByRole('alert')).toContainText('409');
    await expect(page.getByRole('button', { name: '提交决定' })).toBeDisabled();
    await page.getByRole('button', { name: '拒绝 execute', exact: true }).click();
    await page.getByRole('button', { name: '拒绝 write_file', exact: true }).click();
    await page.getByRole('button', { name: '提交决定' }).click();
    await expect(page.locator('.approval-tool')).toHaveCount(1);
    await expect(page.getByRole('button', { name: '提交决定' })).toBeDisabled();
    await page.getByRole('button', { name: '允许执行 execute', exact: true }).click();
    await page.getByRole('button', { name: '提交决定' }).click();
    await expect(page.locator('.answer')).toHaveText('second approval done');
});
test('422 retains form values; asynchronous rejection returns to a usable waiting form', async ({ page }) => {
    await fresh(page);
    await send(page, 'human');
    await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    await human(page);
    let mutated = false;
    await page.route('**/api/agent/runs/*/human-input', async (route) => { const body = route.request().postDataJSON(); if (!mutated) {
        mutated = true;
        await route.continue({ postData: JSON.stringify({ ...body, values: { ...body.values, name: '  ' } }) });
    }
    else
        await route.continue(); });
    await page.getByRole('button', { name: '提交回答' }).click();
    await expect(page.getByRole('alert')).toContainText('422');
    await expect(page.getByLabel('任务名称 *')).toHaveValue('valid task');
    await page.getByLabel('任务名称 *').fill('reject-once');
    await page.getByRole('button', { name: '提交回答' }).click();
    await expect(page.getByRole('status').filter({ hasText: '回答被拒绝' })).toBeVisible();
    await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    await human(page, 'corrected task');
    await page.getByRole('button', { name: '提交回答' }).click();
    await expect(page.locator('.answer')).toHaveText('回答已接收');
});
test('BFF disconnect reconnects from cursor without reposting or cancelling the task', async ({ page, request }) => {
    const posts: string[] = [];
    const cursors: string[] = [];
    page.on('request', r => { if (r.method() === 'POST')
        posts.push(r.url()); if (r.url().includes('/events?'))
        cursors.push(r.url()); });
    await fresh(page);
    await send(page, 'approval');
    await expect(page.getByRole('region', { name: '工具审批' })).toBeVisible();
    await expect(page.getByText('执行细节 · 2 个工具调用', { exact: true })).toBeVisible();
    const activeId = await page.getByLabel('选择任务').inputValue();
    const relevant = () => cursors.filter(url => url.includes('/runs/' + activeId + '/events'));
    await request.post('http://127.0.0.1:8766/restart/gateway');
    await expect(page.getByRole('region', { name: '工具审批' })).toBeVisible();
    await expect.poll(() => relevant().length).toBeGreaterThan(1);
    expect(relevant().some(url => Number(new URL(url).searchParams.get('after')) > 0)).toBe(true);
    expect(posts.filter(url => url.endsWith('/runs'))).toHaveLength(1);
    expect(posts.filter(url => url.endsWith('/cancel'))).toHaveLength(0);
    await page.getByRole('button', { name: '允许执行 execute', exact: true }).click();
    await page.getByRole('button', { name: '拒绝 write_file', exact: true }).click();
    await page.getByRole('button', { name: '提交决定' }).click();
    await expect(page.locator('.answer')).toHaveText('批量决定已执行');
    await page.reload();
    await expect(page.locator('.answer')).toHaveText('批量决定已执行');
    await expect(page.locator('.answer')).toHaveCount(1);
});
test('service crash produces interrupted; explicit resume restores checkpoint into a new Run', async ({ page, request }) => {
    await fresh(page);
    await send(page, 'human');
    await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    const old = await page.getByLabel('选择任务').inputValue();
    await request.post('http://127.0.0.1:8766/restart/service');
    await expect(page.getByRole('status').filter({ hasText: '服务重启中断' })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: '恢复会话' })).toBeDisabled();
    await page.getByLabel('我已检查副作用，确认恢复会话').check();
    await page.getByRole('button', { name: '恢复会话' }).click();
    await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    const current = await page.getByLabel('选择任务').inputValue();
    expect(current).not.toBe(old);
    await human(page);
    await page.getByRole('button', { name: '提交回答' }).click();
    await expect(page.locator('.answer')).toHaveText('恢复完成');
    const previous = await page.request.get(origin + '/api/agent/runs/' + old);
    expect((await previous.json()).status).toBe('interrupted');
});
test('cancellation and pause use authoritative transitions', async ({ page }) => {
    await fresh(page);
    await send(page, 'pause');
    await expect(page.getByRole('button', { name: '继续运行' })).toBeVisible();
    await page.getByRole('button', { name: '继续运行' }).click();
    await expect(page.getByRole('status').filter({ hasText: '已完成' })).toBeVisible();
    await page.getByRole('button', { name: '新建会话' }).click();
    await send(page, 'slow');
    await expect(page.getByRole('region', { name: '工具审批' })).toBeVisible();
    await page.getByRole('button', { name: '允许执行 execute', exact: true }).click();
    await page.getByRole('button', { name: '提交决定' }).click();
    await expect(page.getByRole('region', { name: '工具审批' })).not.toBeVisible();
    const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/cancel'));
    await expect(page.locator('.detail-panel [data-stream=stdout] pre')).toHaveText('started\n');
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/running-before-cancel.png') });
    await page.getByRole('button', { name: '取消任务' }).click();
    expect((await (await response).json()).status).toBe('cancelling');
    await expect(page.getByRole('status').filter({ hasText: '已取消' })).toBeVisible();
});
test('two projects occupy capacity; third shows real 429; stale same-project submit shows real 409', async ({ page }) => {
    await fresh(page);
    const firstSession = await post(page, '/sessions', { project_id: 'app' });
    const first = await post(page, `/sessions/${(await firstSession.json()).id}/runs`, { text: 'human' });
    const a = await first.json();
    await send(page, 'will conflict');
    await expect(page.getByRole('alert')).toContainText('409');
    const secondSession = await post(page, '/sessions', { project_id: 'worker' });
    const second = await post(page, `/sessions/${(await secondSession.json()).id}/runs`, { text: 'human' });
    expect(second.status()).toBe(202);
    const b = await second.json();
    await page.getByRole('button', { name: 'third', exact: true }).click();
    await expect(page.getByLabel('向 Agent 输入指令')).toBeEnabled();
    await send(page, 'over capacity');
    await expect(page.getByRole('alert')).toContainText('429');
    await post(page, `/runs/${a.id}/cancel`, undefined);
    await post(page, `/runs/${b.id}/cancel`, undefined);
});
test('single approval, human, running, completed and failure actual screenshots', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1280 });
    await fresh(page);
    await send(page, 'single');
    await expect(page.getByRole('region', { name: '工具审批' })).toBeVisible();
    await expect(page.getByText('网络权限未提供', { exact: true })).toBeVisible();
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/approval-single-1600.png') });
    await page.getByRole('button', { name: '拒绝 execute', exact: true }).click();
    await page.getByRole('button', { name: '提交决定' }).click();
    await expect(page.locator('.answer')).toHaveText('single done');
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/completed-1600.png') });
    await page.getByRole('button', { name: '新建会话' }).click();
    await send(page, 'human');
    await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/human-1440.png') });
    await human(page);
    await page.getByRole('button', { name: '提交回答' }).click();
    await expect(page.locator('.answer')).toHaveText('回答已接收');
    await page.getByRole('button', { name: '新建会话' }).click();
    await send(page, 'fail');
    await expect(page.getByRole('status').filter({ hasText: '执行失败' })).toBeVisible();
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/failed-1440.png') });
});
test('real 15s keepalive traverses BFF without reconnect storm', async ({ page }) => {
    await fresh(page);
    await send(page, 'human');
    await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    const id = await page.getByLabel('选择任务').inputValue();
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 22000);
    try {
        const response = await fetch('http://127.0.0.1:8765/api/agent/runs/' + id + '/events', { signal: abort.signal });
        expect(response.status).toBe(200);
        const reader = response.body!.getReader();
        const decoder = new TextDecoder();
        let content = '';
        try {
            while (!content.includes(': keepalive')) {
                const part = await reader.read();
                expect(part.done).toBe(false);
                content += decoder.decode(part.value, { stream: true });
            }
            expect(content).toContain(': keepalive\n\n');
        }
        finally {
            await reader.cancel();
        }
        await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    }
    finally {
        clearTimeout(timer);
        abort.abort();
    }
});
test('leaving the page closes its stream without cancelling the Run', async ({ page }) => {
    await fresh(page);
    await send(page, 'human');
    await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    const id = await page.getByLabel('选择任务').inputValue();
    await page.close();
    const response = await page.request.get(origin + '/api/agent/projects/app/active-run');
    const live = (await response.json()).run;
    expect(live.id).toBe(id);
    expect(live.status).toBe('waiting_human');
});
