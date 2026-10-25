/**
 * 使用度量（plan/W12.md §7.1 的前端半边）。
 *
 * 纪律：
 * - 只采页面级事件（进入/停留/决策耗时），不采点击流，不含 PII；
 * - 上报失败一律静默（catch 后丢弃），绝不影响主流程、绝不写 console；
 * - 后端聚合接口（W12 #6 后半）未上线前，POST /metrics/events 会 404——
 *   静默失败是设计行为，不是 bug。
 */

import { getAccessToken } from "@/lib/api/client";

const ENDPOINT = "/metrics/events";
const FLUSH_INTERVAL_MS = 20_000;
const MAX_BUFFER = 20;

export interface MetricEvent {
  /** 事件名：page_enter / page_leave / decision */
  name: "page_enter" | "page_leave" | "decision";
  /** 页面路径（page_* 事件） */
  path?: string;
  /** 停留毫秒（page_leave）或决策耗时毫秒（decision） */
  duration_ms?: number;
  /** 客户端时间（ISO），仅用于服务端排序 */
  at: string;
}

let buffer: MetricEvent[] = [];
let flushTimer: ReturnType<typeof setInterval> | null = null;
let currentPath: string | null = null;
let enteredAt = 0;
let flushing = false;

function post(events: MetricEvent[]): void {
  // 不走 apiFetch：度量失败不该触发 401 刷新或错误映射
  const token = getAccessToken();
  try {
    void fetch(`${process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1"}${ENDPOINT}`, {
      method: "POST",
      credentials: "include",
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ events }),
    }).catch(() => undefined);
  } catch {
    // 静默
  }
}

function flush(): void {
  if (flushing || buffer.length === 0) return;
  flushing = true;
  const batch = buffer.splice(0, MAX_BUFFER);
  post(batch);
  flushing = false;
}

/** 离开当前页：算停留并立即上报（keepalive 保证 pagehide 时也能发出） */
function leaveCurrentPage(): void {
  if (currentPath === null) return;
  buffer.push({
    name: "page_leave",
    path: currentPath,
    duration_ms: Math.min(Date.now() - enteredAt, 30 * 60_000), // 异常值截断
    at: new Date().toISOString(),
  });
  currentPath = null;
  flush();
}

/** 路由变化时由 MetricsTracker 调用 */
export function trackPageEnter(path: string): void {
  if (typeof window === "undefined") return;
  leaveCurrentPage();
  currentPath = path;
  enteredAt = Date.now();
  buffer.push({ name: "page_enter", path, at: new Date().toISOString() });
  if (buffer.length >= MAX_BUFFER) flush();
}

/** 决策耗时：从队列可交互到做出决定 */
export function trackDecision(durationMs: number): void {
  if (typeof window === "undefined" || durationMs < 0) return;
  buffer.push({ name: "decision", duration_ms: Math.round(durationMs), at: new Date().toISOString() });
  flush();
}

/** 在 console 布局挂载时调用一次：装定时刷新与页面隐藏钩子 */
export function initMetrics(): () => void {
  if (typeof window === "undefined") return () => undefined;
  flushTimer ??= setInterval(flush, FLUSH_INTERVAL_MS);
  const onHide = (): void => {
    if (document.visibilityState === "hidden") leaveCurrentPage();
  };
  document.addEventListener("visibilitychange", onHide);
  window.addEventListener("pagehide", leaveCurrentPage);
  return () => {
    document.removeEventListener("visibilitychange", onHide);
    window.removeEventListener("pagehide", leaveCurrentPage);
  };
}
