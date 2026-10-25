import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("今日页：3 个数字、8 张决策卡、J/K/Enter 可用", async ({ page }) => {
  const { calls } = await mockApi(page);
  await page.goto("/today");

  await expect(page.getByRole("heading", { name: "今日" })).toBeVisible();
  await expect(page.getByText("决策负载")).toBeVisible();
  await expect(page.getByText("3/12")).toBeVisible();

  // 后端返回 10 条，页面只渲染 8 条，其余折叠成一句话
  await expect(page.getByRole("button", { name: "通过" })).toHaveCount(8);
  await expect(page.getByText("还有 2 条，明天再说。")).toBeVisible();

  // J 移到第二张（L1），Enter 通过 → 直接发出 decide 请求
  await page.keyboard.press("j");
  await page.keyboard.press("Enter");
  await expect.poll(() => calls.filter((c) => c.method === "POST").length).toBe(1);
  const decide = calls.find((c) => c.method === "POST");
  expect(decide?.path).toBe("/approvals/a2/decide");
  expect((decide?.body as { decision: string }).decision).toBe("approved");
});

test("今日页：下午放行 L0 必须二次确认", async ({ page }) => {
  const { calls } = await mockApi(page);
  await page.clock.setFixedTime(new Date("2026-10-19T14:00:00"));
  await page.goto("/today");

  // 等队列加载完成再操作
  await expect(page.getByText("3/12")).toBeVisible();

  // 第一张是 L0，下午直接 Enter 不发请求，先弹确认
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("alertdialog", { name: "放行这条 L0 决策？" });
  await expect(dialog).toBeVisible();
  await expect.poll(() => calls.filter((c) => c.method === "POST").length).toBe(0);

  // 确认后才放行
  await dialog.getByRole("button", { name: "确认放行" }).click();
  await expect.poll(() => calls.filter((c) => c.method === "POST").length).toBe(1);
  const decide = calls.find((c) => c.method === "POST");
  expect(decide?.path).toBe("/approvals/a1/decide");
});

test("今日页：深夜 L0 冻结，不出结论", async ({ page }) => {
  const { calls } = await mockApi(page);
  await page.clock.setFixedTime(new Date("2026-10-19T00:30:00"));
  await page.goto("/today");

  // 等队列加载完成再断言
  await expect(page.getByText("3/12")).toBeVisible();

  // L0 卡片的操作按钮被禁用，页面出现深夜提示
  await expect(page.getByText(/深夜/).first()).toBeVisible();
  const firstCard = page.locator("main ul li").first();
  await expect(firstCard).toContainText("深夜不出结论");

  // 键盘 Enter 也不生效：a1 无 decide 请求
  await page.keyboard.press("Enter");
  await page.waitForTimeout(300);
  expect(calls.filter((c) => c.method === "POST" && c.path.includes("/decide"))).toEqual([]);
});
