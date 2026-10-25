import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("资产页：认知卡 / skill / harness 三库分 Tab", async ({ page }) => {
  await mockApi(page);
  await page.goto("/assets");

  await expect(page.getByRole("heading", { name: "资产" })).toBeVisible();

  // 默认 Tab：认知卡
  await expect(page.getByText("还没有认知卡")).toBeVisible();

  // skill Tab
  await page.getByRole("button", { name: "skill", exact: true }).click();
  await expect(page.getByText("还没有 skill")).toBeVisible();

  // harness Tab
  await page.getByRole("button", { name: "harness", exact: true }).click();
  await expect(page.getByText("还没有 harness 模板")).toBeVisible();
});
