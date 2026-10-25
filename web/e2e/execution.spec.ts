import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("执行页：默认只看异常，正常任务不占屏", async ({ page }) => {
  await mockApi(page);
  await page.goto("/execution");

  await expect(page.getByRole("heading", { name: "执行" })).toBeVisible();
  await expect(page.getByText("异常任务")).toBeVisible();

  await expect(page.getByText("抓取竞品价格卡住 20 分钟")).toBeVisible();
  await expect(page.getByText("生成周报失败：模型超时")).toBeVisible();
  await expect(page.getByText("已完成任务 0")).toHaveCount(0);

  // 异常分档：卡住是 warn（可自愈），失败是 danger（要人处理）
  const main = page.locator("main");
  await expect(main.getByText("卡住", { exact: true })).toBeVisible();
  await expect(main.getByText("失败", { exact: true })).toBeVisible();

  // 正常任务折叠为底部一行统计
  await expect(page.getByText(/正常任务 5 个已折叠/)).toBeVisible();
});

test("执行页：点异常行打开 Drawer 详情", async ({ page }) => {
  await mockApi(page);
  await page.goto("/execution");
  await expect(page.getByText("抓取竞品价格卡住 20 分钟")).toBeVisible();

  await page.getByRole("button", { name: /抓取竞品价格卡住 20 分钟/ }).click();
  const drawer = page.getByRole("dialog", { name: "任务详情" });
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("卡住");
  await expect(drawer).toContainText("t-stuck");

  // Esc 关闭
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
});
