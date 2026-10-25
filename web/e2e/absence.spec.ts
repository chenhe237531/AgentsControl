import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("缺席页：回归简报 ≤3 条决策 + 一键归档 + 交接操作", async ({ page }) => {
  const { calls } = await mockApi(page);
  await page.goto("/absence");

  // 演示数据声明必须显式可见（不造假在线状态）
  await expect(page.getByText(/演示数据/)).toBeVisible();

  // 头部一句话
  await expect(page.getByText(/2 件需要你定/)).toBeVisible();

  // 决策区 ≤3 条，带推荐与后果
  const main = page.locator("main");
  await expect(main.getByText("推荐", { exact: true })).toHaveCount(2);
  await expect(main.getByText(/不做：省下 ¥600 预算与 30 天观察期/)).toBeVisible();

  // 已完成折叠可展开
  await page.getByText(/已完成 5 件/).click();
  await expect(page.getByText("竞品价格抓取（每日一轮）").first()).toBeVisible();

  // 一键归档：决策区收起
  await page.getByRole("button", { name: "全部知道了，归档" }).click();
  await expect(page.getByText("简报已归档")).toBeVisible();

  // 交接区四操作之一：切只读
  await page.getByRole("button", { name: "切只读" }).click();
  await expect(page.getByText("已切换为只读模式")).toBeVisible();

  // 缺席页不依赖任何 API（W11 未排期）：零请求
  expect(calls).toEqual([]);
});
