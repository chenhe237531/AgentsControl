import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("总视图：关键指标与项目墙", async ({ page }) => {
  await mockApi(page);
  await page.goto("/overview");

  await expect(page.getByRole("heading", { name: "总视图" })).toBeVisible();
  await expect(page.getByText("待决策", { exact: true }).first()).toBeVisible();
  // mock 的目标在项目墙出现
  await expect(page.getByText("把迁移合并头的处理写成脚本")).toBeVisible();
});
