import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("项目视图：甘特拓扑与三种信号图例", async ({ page }) => {
  await mockApi(page);
  await page.goto("/projects");

  await expect(page.getByRole("heading", { name: "项目视图" })).toBeVisible();
  await expect(page.getByText("三种信号")).toBeVisible();
  // 示例甘特条（W06 会话接入前为演示数据）
  await expect(page.getByText("W01 身份内核")).toBeVisible();
});
