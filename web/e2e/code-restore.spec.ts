import { test, expect, type Page } from '@playwright/test';
import path from 'node:path';

async function executeRound(page: Page, text: string) {
    await page.getByLabel('向 Agent 输入指令').fill(text);
    await page.getByRole('button', { name: '发送指令', exact: true }).click();
    await page.getByRole('button', { name: '允许执行 execute', exact: true }).click();
    await page.getByRole('button', { name: '提交决定', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: '已完成' })).toBeVisible();
    return page.getByLabel('选择任务').inputValue();
}

test('per-dialog-round undo/redo restores real shell files, survives restart and rejects later edits', async ({ page, request }) => {
    await page.setViewportSize({ width: 1600, height: 1280 });
    await page.goto('/');
    await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible();
    await page.getByRole('button', { name: '新建会话' }).click();
    await executeRound(page, 'undo-seed');
    const one = await executeRound(page, 'undo-one');
    const two = await executeRound(page, 'undo-two');
    const inspector = page.getByRole('complementary', { name: '任务产出', exact: true });
    await inspector.getByText('代码变更', { exact: true }).click();
    const controls = page.getByRole('region', { name: '轮次代码回退' });
    await page.getByLabel('选择任务').selectOption(one);
    await controls.getByRole('button', { name: '回退本轮代码', exact: true }).click();
    await controls.getByRole('button', { name: '确认回退本轮', exact: true }).click();
    await expect(controls.getByRole('alert')).toContainText('409');
    expect((await (await request.get('http://127.0.0.1:8766/undo-evidence')).json()).code).toBe('two');
    await page.getByLabel('选择任务').selectOption(two);
    await controls.getByRole('button', { name: '回退本轮代码', exact: true }).click();
    await controls.getByRole('button', { name: '确认回退本轮', exact: true }).click();
    await expect(controls.getByRole('status')).toHaveText('本轮代码已回退');
    expect((await (await request.get('http://127.0.0.1:8766/undo-evidence')).json()).code).toBe('one');
    await page.getByLabel('选择任务').selectOption(one);
    await controls.getByRole('button', { name: '回退本轮代码', exact: true }).click();
    await controls.getByRole('button', { name: '确认回退本轮', exact: true }).click();
    await expect(controls.getByRole('status')).toHaveText('本轮代码已回退');
    expect(await (await request.get('http://127.0.0.1:8766/undo-evidence')).json()).toEqual({ code: 'initial', deleted: 'keep', binary: null });
    await request.post('http://127.0.0.1:8766/restart/service');
    await page.reload();
    await inspector.getByText('代码变更', { exact: true }).click();
    await expect(controls.getByRole('status')).toHaveText('本轮代码已回退');
    await controls.getByRole('button', { name: '撤销回退', exact: true }).click();
    await controls.getByRole('button', { name: '确认撤销回退', exact: true }).click();
    await expect(controls.getByRole('button', { name: '回退本轮代码', exact: true })).toBeVisible();
    expect(await (await request.get('http://127.0.0.1:8766/undo-evidence')).json()).toEqual({ code: 'one', deleted: null, binary: [0,255,1] });
    await controls.getByText(/本轮快照/).click();
    await expect(controls).toContainText('undo-created/binary');
    await inspector.evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/code-restore-1600.png') });
    await page.getByLabel('选择任务').selectOption(two);
    await controls.getByRole('button', { name: '撤销回退', exact: true }).click();
    await controls.getByRole('button', { name: '确认撤销回退', exact: true }).click();
    expect((await (await request.get('http://127.0.0.1:8766/undo-evidence')).json()).code).toBe('two');
    expect(await page.getByLabel('选择任务').inputValue()).toBe(two);
});
