"use client";

import { useEffect, useState } from "react";

/**
 * 首帧 false、mount 后 true。
 *
 * 一切依赖本地时钟的 UI（当天日期、决策时段、月份列表）都必须先走它：
 * 预渲染 HTML 固化的是构建时刻，而且构建容器 TZ=UTC、浏览器 TZ=Asia/Shanghai，
 * render 期直接计算会让两端差出一天/一个时段——hydration 必然 mismatch
 * （React #418）。先例：今日页日期与排班、账本页月份下拉。
 */
export function useMounted(): boolean {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted;
}
