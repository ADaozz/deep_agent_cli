import { test, expect } from '@playwright/test';
import path from 'node:path';
const stamp = '2026-10-13T00:00:00+00:00';
test('fixture visual baseline and real narrow drawers', async ({ page }) => {
    await page.route('**/api/agent/**', async (route) => {
        const url = new URL(route.request().url());
        let data: unknown = {};
        if (url.pathname.endsWith('/projects'))
            data = { projects: [{ id: 'workspace-test' }] };
        else if (url.pathname.endsWith('/health'))
            data = { status: 'ok' };
        else if (url.pathname.endsWith('/sessions'))
            data = { sessions: [] };
        await route.fulfill({ json: data });
    });
    await page.setViewportSize({ width: 1600, height: 1280 });
    await page.goto('/');
    await expect(page.getByText('workspace-test', { exact: true })).toBeVisible();
    await expect(page.locator('.main')).toHaveCSS('background-color', 'rgb(0, 0, 0)');
    await expect(page.locator('.sidebar')).toHaveCSS('background-color', 'rgb(34, 34, 36)');
    await expect(page.locator('.send-btn')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/m1-empty-1600.png') });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.screenshot({ path: path.resolve('../docs/frontend-evidence/m1-empty-1440.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '项目导航', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '项目与会话' })).toBeVisible();
    await page.keyboard.press('Shift+Tab');
    await expect(page.getByRole('dialog', { name: '项目与会话' })).toContainText('workspace-test');
    expect(await page.evaluate(() => document.activeElement?.closest('.sidebar') !== null)).toBe(true);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('button', { name: '项目导航', exact: true })).toBeFocused();
    await expect(page.locator('.sidebar')).not.toBeVisible();
    await page.getByRole('button', { name: '任务产出', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '任务产出' })).toBeVisible();
    await page.getByRole('button', { name: '关闭任务产出', exact: true }).click();
    await expect(page.locator('.inspector')).not.toBeVisible();
});
