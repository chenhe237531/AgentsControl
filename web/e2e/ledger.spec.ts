import { expect, test } from "@playwright/test";

import { mockApi } from "./helpers";

test("账本页：双币种同屏 + 导出本期", async ({ page }) => {
  await mockApi(page);
  await page.goto("/ledger");

  await expect(page.getByRole("heading", { name: "账本" })).toBeVisible();
  // 钱：六分项（合计出现在 StatTile 与卡片头徽标两处）
  await expect(page.getByText("钱 · 六分项")).toBeVisible();
  await expect(page.getByText("¥128.50").first()).toBeVisible();
  // 注意力：min/outcome 同屏可见
  await expect(page.getByText("min / outcome")).toBeVisible();

  // 导出：点击产生下载 + Toast 反馈
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出本期" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^ledger-\d{4}-\d{2}\.csv$/);
  await expect(page.getByText("已导出本期 CSV")).toBeVisible();
});
