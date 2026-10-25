"use client";

/**
 * 页面级度量挂点：随路由变化上报进入/停留事件（W12 #6 前端半边）。
 * 上报失败静默（见 lib/metrics.ts），不影响任何页面行为。
 */

import { usePathname } from "next/navigation";
import { useEffect } from "react";

import { initMetrics, trackPageEnter } from "@/lib/metrics";

export function MetricsTracker() {
  const pathname = usePathname();

  useEffect(() => initMetrics(), []);

  useEffect(() => {
    if (pathname) trackPageEnter(pathname);
  }, [pathname]);

  return null;
}
