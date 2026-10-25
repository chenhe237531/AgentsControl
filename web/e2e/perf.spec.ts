import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

/**
 * LCP 实测：生产模式（next start，Playwright webServer 已负责）+ mock 数据，
 * PerformanceObserver(buffered) 从文档脚本起收集全部 LCP 候选，
 * 取最大值断言 <= 2500ms（计划 §4.5，本地口径；staging 复测另记）。
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

const LCP_BUDGET_MS = 2500;

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const w = window as unknown as { __lcps?: number[] };
    w.__lcps = [];
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) w.__lcps?.push(entry.startTime);
    }).observe({ type: "largest-contentful-paint", buffered: true });
  });
});

for (const path of PAGES) {
  test(`首屏 LCP ≤ ${LCP_BUDGET_MS}ms：${path}`, async ({ page }) => {
    await mockApi(page);
    await page.clock.setFixedTime(new Date("2026-10-19T05:00:00Z"));
    await page.goto(path, { waitUntil: "load" });
    // 留出静默窗口：懒加载画布 / 数据渲染可能更新 LCP 候选
    await page.waitForTimeout(700);

    const lcp = await page.evaluate(() => {
      const w = window as unknown as { __lcps?: number[] };
      return Math.max(...(w.__lcps ?? [0]));
    });
    expect(lcp, `${path} LCP ${lcp.toFixed(0)}ms 超预算`).toBeLessThanOrEqual(LCP_BUDGET_MS);
  });
}
