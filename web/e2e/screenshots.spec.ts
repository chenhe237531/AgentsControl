import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

/**
 * 截图基线：11 页全页截图，首次 --update-snapshots 生成基线，
 * 之后每次运行逐像素对比（容差 2% 像素）。改动 UI 后有意更新：
 *   npx playwright test screenshots --update-snapshots
 * 时钟固定 + mock 数据固定 + 禁动画，保证可复现。
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

const NOW = new Date("2026-10-19T05:00:00Z");

for (const path of PAGES) {
  test(`截图基线：${path}`, async ({ page }) => {
    await mockApi(page);
    await page.clock.setFixedTime(NOW);
    await page.goto(path);
    await expect(page.locator("main")).toBeVisible();
    await page.waitForLoadState("networkidle");
    // 项目页：等懒加载画布真正渲染完，避免截到骨架
    if (path === "/projects") {
      await expect(page.getByText("W01 身份内核")).toBeVisible();
    }
    await page.waitForTimeout(500);
    await expect(page)
      .toHaveScreenshot(`${path.replace(/\//g, "_") || "root"}.png`, {
        animations: "disabled",
        caret: "hide",
        fullPage: true,
        maxDiffPixelRatio: 0.02,
      });
  });
}
