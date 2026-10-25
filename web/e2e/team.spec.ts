import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("团队页：能力矩阵（设计值）与派活规则", async ({ page }) => {
  await mockApi(page);
  await page.goto("/team");

  await expect(page.getByRole("heading", { name: "团队" })).toBeVisible();
  await expect(page.getByText("能力矩阵（设计值）")).toBeVisible();
  await expect(page.getByText("非实时")).toBeVisible();

  // 矩阵行
  await expect(page.getByText("常用 Mac 主机")).toBeVisible();
  await expect(page.getByText("第三方平台")).toBeVisible();
  // always_on 纪律说明
  await expect(page.getByText(/可疑状态怎么判/)).toBeVisible();
});
