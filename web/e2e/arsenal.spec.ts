import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("兵工厂：跑评测必须先过成本确认", async ({ page }) => {
  const { calls } = await mockApi(page);
  await page.goto("/arsenal");

  await expect(page.getByRole("heading", { name: "兵工厂" })).toBeVisible();
  // 无评测数据时给引导而非空屏
  await expect(page.getByText("还没有评测数据")).toBeVisible();

  // 直接点"跑一轮评测"不发出请求，先弹成本确认
  await page.getByRole("button", { name: "跑一轮评测" }).click();
  const dialog = page.getByRole("alertdialog", { name: "跑一轮评测？" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("成本上限 ¥20");
  await expect.poll(() => calls.some((c) => c.path === "/evals/runs")).toBe(false);

  // 确认后发出请求，批次提示条出现（id 展示为前 8 位）
  await dialog.getByRole("button", { name: "确认启动" }).click();
  await expect.poll(() => calls.some((c) => c.path === "/evals/runs")).toBe(true);
  await expect(page.getByText(/批次 run-e2e- 已创建/)).toBeVisible();
});
