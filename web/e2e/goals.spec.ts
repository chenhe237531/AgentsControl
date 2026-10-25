import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("目标页：双轨分栏，目标卡三要素可见", async ({ page }) => {
  await mockApi(page);
  await page.goto("/goals");

  await expect(page.getByRole("heading", { name: "目标" })).toBeVisible();
  await expect(page.getByText("赚钱", { exact: true })).toBeVisible();
  await expect(page.getByText("进化", { exact: true })).toBeVisible();

  // mock 的进化轨目标卡：标题 + 验收 + 预算（限 main，避开侧边栏同词）
  const main = page.locator("main");
  await expect(main.getByText("把迁移合并头的处理写成脚本")).toBeVisible();
  await expect(main.getByText(/下一次并行窗口合并时不再手写 merge revision/)).toBeVisible();
  await expect(main.getByText("预算")).toBeVisible();
  await expect(main.getByText("剩余天数")).toBeVisible();
});
