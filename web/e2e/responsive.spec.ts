import { expect, test, type Page } from "@playwright/test";

import { mockApi } from "./helpers";

/**
 * 响应式冒烟（计划阶段 3 出口标准）：
 * 375px（手机）与 768px（平板竖屏）下，任何页面不得出现文档级横向滚动。
 * 组件内部的 overflow-x-auto（表格/甘特平移）不算溢出——那是交互。
 */

const PAGES = [
  "/today",
  "/inbox",
  "/goals",
  "/execution",
  "/team",
  "/arsenal",
  "/assets",
  "/ledger",
  "/overview",
  "/projects",
  "/absence",
];

async function assertNoHorizontalOverflow(page: Page, path: string, width: number): Promise<void> {
  await page.setViewportSize({ width, height: 800 });
  await mockApi(page);
  await page.goto(path);
  await expect(page.locator("main")).toBeVisible();

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, `${path} @${width}px 横向溢出 ${overflow}px`).toBeLessThanOrEqual(1);
}

for (const path of PAGES) {
  test(`375px 无横向溢出：${path}`, async ({ page }) => {
    await assertNoHorizontalOverflow(page, path, 375);
  });
}

for (const path of PAGES.slice(0, 4)) {
  test(`768px 无横向溢出：${path}`, async ({ page }) => {
    await assertNoHorizontalOverflow(page, path, 768);
  });
}
