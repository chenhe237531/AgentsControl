/**
 * 首屏 JS 预算校验（docs/UI-OPTIMIZATION-PLAN-v1.html §4.5，预算为 Assumption）。
 *
 * 口径：预渲染 HTML（.next/server/app/*.html）里同步 <script src> 引用的
 * _next 静态 JS，逐文件 gzip 求和 = 该页真实首屏 JS。
 * next/dynamic(ssr:false) 的 chunk 不在预渲染 HTML 的同步标签里，
 * 懒加载画布不计入首屏——这正是预算想约束的东西。
 *
 * 断言：每页相对全站最小基线的增量 ≤ 30KB gzip。
 * 用法：先 npm run build，再 node scripts/perf-budget.mjs
 */
import { readFileSync, readdirSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { join } from "node:path";

const BUDGET_EXTRA_KB = 30;
const appDir = join(process.cwd(), ".next", "server", "app");

const htmlFiles = readdirSync(appDir).filter((f) => f.endsWith(".html"));
if (htmlFiles.length === 0) {
  console.error("未找到预渲染 HTML：请先 npm run build");
  process.exit(2);
}

const rows = [];
for (const file of htmlFiles) {
  const html = readFileSync(join(appDir, file), "utf8");
  const srcs = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)]
    .map((m) => m[1].split("?")[0])
    .filter((src) => src.startsWith("/_next/static/"));

  let total = 0;
  const seen = new Set();
  for (const src of srcs) {
    if (seen.has(src)) continue;
    seen.add(src);
    const rel = src.replace(/^\/_next\//, "");
    try {
      total += gzipSync(readFileSync(join(process.cwd(), ".next", rel))).length;
    } catch {
      console.error(`  找不到 chunk 文件：${src}（构建产物不完整？）`);
      process.exit(2);
    }
  }
  const route = file === "index.html" ? "/" : `/${file.replace(/\.html$/, "")}`;
  rows.push({ route, kb: total / 1024 });
}

rows.sort((a, b) => a.kb - b.kb);
const baseline = rows[0].kb;

console.log("\n首屏 JS（gzip，预渲染 HTML 同步标签口径）");
console.log("route         total      增量(相对最小页)");
let fail = false;
for (const r of rows) {
  const extra = r.kb - baseline;
  const flag = extra > BUDGET_EXTRA_KB ? "  <- 超预算" : "";
  if (extra > BUDGET_EXTRA_KB) fail = true;
  console.log(
    `${r.route.padEnd(14)}${r.kb.toFixed(1).padStart(6)} kB  +${extra.toFixed(1).padStart(6)} kB${flag}`,
  );
}
console.log(`\n预算：单页首屏增量 <= ${BUDGET_EXTRA_KB} kB gzip；最小基线 ${baseline.toFixed(1)} kB（框架共享）`);

if (fail) {
  console.error("结果：失败，存在超预算页面");
  process.exit(1);
}
console.log("结果：全部达标");
