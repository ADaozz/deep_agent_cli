import { test, expect } from '@playwright/test';
import path from 'node:path';
test('genuine service + BFF + Runner batch approval and human input', async ({ page }) => {
    const posts: string[] = [];
    page.on('request', request => { if (request.method() === 'POST')
        posts.push(request.url()); });
    await page.goto('/');
    await expect(page.getByText('app', { exact: true })).toBeVisible();
    await page.getByLabel('向 Agent 输入指令').fill('approval');
    await page.getByRole('button', { name: '发送指令' }).click();
    await expect(page.getByRole('region', { name: '工具审批' })).toBeVisible();
    await expect(page.locator('.approval-tool')).toHaveCount(2);
    await page.setViewportSize({ width: 1600, height: 1280 });
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/approval-multiple-1600.png') });
    await page.getByRole('button', { name: '允许执行 execute', exact: true }).click();
    await page.getByRole('button', { name: '拒绝 write_file', exact: true }).click();
    await page.getByRole('button', { name: '提交决定', exact: true }).click();
    await expect(page.locator('.answer')).toHaveText('批量决定已执行');
    expect(posts.filter(url => url.endsWith('/approvals'))).toHaveLength(1);
    await expect(page.getByRole('status').filter({ hasText: '已完成' })).toBeVisible();
    await page.getByLabel('向 Agent 输入指令').fill('human');
    await page.getByRole('button', { name: '发送指令' }).click();
    await expect(page.getByRole('form', { name: '补充回答' })).toBeVisible();
    await page.getByRole('button', { name: '提交回答' }).click();
    await expect(page.getByRole('alert')).toContainText('必填');
    await page.getByLabel('任务名称 *').fill('integration task');
    await page.getByLabel('修改范围 *').selectOption('tests');
    await page.getByLabel('允许继续 *').selectOption('false');
    await page.getByRole('button', { name: '提交回答' }).click();
    await expect(page.locator('.answer')).toHaveText('回答已接收');
});
test('fresh browser discovers current approval and history without local storage', async ({ page, browser }) => {
    await page.goto('/');
    await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible();
    await page.getByRole('button', { name: '新建会话' }).click();
    await page.getByLabel('向 Agent 输入指令').fill('approval');
    await page.getByRole('button', { name: '发送指令' }).click();
    await expect(page.getByRole('region', { name: '工具审批' })).toBeVisible();
    const second = await browser.newContext();
    const other = await second.newPage();
    try {
        await other.goto('http://127.0.0.1:5173/');
        await expect(other.getByRole('region', { name: '工具审批' })).toBeVisible();
        await expect(other.getByText('原始指令不可用（服务未持久化提交文本）', { exact: true })).toBeVisible();
        await expect(other.getByLabel('选择任务')).toBeVisible();
        await other.getByRole('button', { name: '拒绝 execute', exact: true }).click();
        await other.getByRole('button', { name: '拒绝 write_file', exact: true }).click();
        await other.getByRole('button', { name: '提交决定', exact: true }).click();
        await expect(other.locator('.answer')).toHaveText('批量决定已执行');
        await expect(page.getByRole('region', { name: '工具审批' })).not.toBeVisible();
        await other.reload();
        await expect(other.locator('.answer')).toHaveText('批量决定已执行');
        await expect(other.getByLabel('选择任务')).toBeVisible();
    }
    finally {
        await second.close();
    }
});
test('actual private gateway token is absent from browser traffic, HTML, storage and build', async ({ page, request }) => {
    const traffic: Promise<unknown>[] = [];
    page.on('request', r => { if (r.url().includes('/api/agent'))
        traffic.push(r.allHeaders().then(headers => ({ headers, body: r.postData() }))); });
    await page.goto('/');
    await expect(page.getByText('app', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '新建会话' }).click();
    await page.getByLabel('向 Agent 输入指令').fill('security check');
    await page.getByRole('button', { name: '发送指令' }).click();
    await expect(page.locator('.answer')).toHaveText('任务完成：security check');
    const traces = await Promise.all(traffic);
    expect(traces.every(t => !('authorization' in (t as {
        headers: Record<string, string>;
    }).headers))).toBe(true);
    const storage = await page.evaluate(() => ({ local: { ...localStorage }, session: { ...sessionStorage } }));
    const audit = await request.post('http://127.0.0.1:8766/audit', { data: { traffic: traces, html: await page.content(), storage } });
    expect((await audit.json()).matches).toBe(0);
});
test('desktop session filter is a real focused control, not a hidden sidebar', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await expect(page.getByText('app', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '新建会话' }).click();
    await page.getByRole('button', { name: '筛选会话', exact: true }).click();
    await expect(page.getByLabel('筛选最近会话 ID')).toBeVisible();
    await expect(page.getByLabel('筛选最近会话 ID')).toBeFocused();
    await page.getByLabel('筛选最近会话 ID').fill('not-a-real-session');
    await expect(page.getByText('没有匹配的会话', { exact: true })).toBeVisible();
});
