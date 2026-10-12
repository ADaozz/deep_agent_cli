import { test, expect } from '@playwright/test';
import path from 'node:path';

test('real file changes and inspector outputs survive replay and stay scoped to a Run', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1280 });
    await page.goto('/');
    await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible();
    await page.getByRole('button', { name: '新建会话' }).click();
    await page.getByLabel('向 Agent 输入指令').fill('file-changes');
    await page.getByRole('button', { name: '发送指令' }).click();
    for (const name of ['write_file', 'edit_file', 'write_file', 'edit_file', 'execute', 'delete']) {
        await page.getByRole('button', { name: `允许执行 ${name}`, exact: true }).click();
        await page.getByRole('button', { name: '提交决定', exact: true }).click();
    }
    await expect(page.getByRole('status').filter({ hasText: '已完成' })).toBeVisible();
    const inspector = page.getByRole('complementary', { name: '任务产出', exact: true });
    await inspector.getByText('代码变更', { exact: true }).click();
    await expect(inspector.locator('.file-change')).toHaveCount(4);
    await expect(inspector).toContainText('1 个文件 · 4 次成功操作');
    await expect(inspector.locator('[data-tool-call-id="change-failed"]')).toHaveCount(0);
    const edit = inspector.locator('[data-tool-call-id="change-edit"]');
    await edit.locator('summary').click();
    await expect(edit.locator('[data-change-side="before"] pre')).toHaveText('before');
    await expect(edit.locator('[data-change-side="after"] pre')).toHaveText('after');
    const create = inspector.locator('[data-tool-call-id="change-create"]');
    await create.locator('summary').click();
    await expect(create.locator('[data-operation]')).toHaveText('新建');
    await expect(create.locator('pre')).toHaveText('const label = "before";');
    const overwrite = inspector.locator('[data-tool-call-id="change-overwrite"]');
    await overwrite.locator('summary').click();
    await expect(overwrite.locator('[data-operation]')).toHaveText('覆盖');
    await expect(overwrite.locator('pre')).toHaveText('const label = "overwritten";');
    const deleted = inspector.locator('[data-tool-call-id="change-delete"]');
    await deleted.locator('summary').click();
    await expect(deleted).toContainText('文件已删除');
    await inspector.getByText('命令输出', { exact: true }).click();
    const output = inspector.locator('.tool-detail').filter({ has: page.locator('summary', { hasText: 'execute · completed' }) });
    await output.locator(':scope > summary').click();
    await output.locator('[data-stream="stdout"] > summary').click();
    // The current backend forwards merged command output under stream=stdout.
    await expect(output.locator('[data-stream="stdout"] pre')).toContainText('artifact stdout');
    await expect(output.locator('[data-stream="stdout"] pre')).toContainText('artifact stderr');
    await expect(output.locator('[data-stream="stdout"] pre')).toContainText('const label = "overwritten";');
    await expect(output.locator('[data-stream="stderr"]')).toHaveCount(0);
    for (const name of ['测试结果', '预览 / 截图'])
        await inspector.getByText(name, { exact: true }).click();
    await expect(inspector).toContainText('暂无结构化测试统计');
    await expect(inspector).toContainText('当前服务未提供二进制附件下载');
    await inspector.evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/code-changes-1600.png') });
    await page.reload();
    await expect(inspector.locator('.file-change')).toHaveCount(4);
    await inspector.getByText('代码变更', { exact: true }).click();
    await expect(inspector).toContainText('1 个文件 · 4 次成功操作');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '任务产出', exact: true }).click();
    const drawer = page.getByRole('dialog', { name: '任务产出' });
    await expect(drawer.locator('.file-change')).toHaveCount(4);
    await drawer.locator('[data-tool-call-id="change-edit"] > summary').click();
    await expect(drawer.locator('[data-tool-call-id="change-edit"] [data-change-side="after"] pre')).toHaveText('after');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('button', { name: '任务产出', exact: true })).toBeFocused();
    await page.setViewportSize({ width: 1600, height: 1280 });
    await page.getByRole('button', { name: 'worker', exact: true }).click();
    await expect(inspector.locator('.file-change')).toHaveCount(0);
    await expect(inspector).toContainText('暂无成功的文件工具变更记录');
});
