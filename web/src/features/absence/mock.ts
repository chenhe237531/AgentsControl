/**
 * 缺席窗口与回归简报 —— 类型按 plan/W11.md I01/I02 契约冻结，数据为演示值。
 *
 * TODO(W11): 后端 /absence/* 接口排期落地后，删除本文件的 MOCK_*，
 * 页面改读 lib/api/hooks（GET /absence/briefs/latest 等）。UI 与交互不再变。
 */

export type AbsenceMode = "autonomous" | "readonly";

/** W11-I01 · 缺席窗口契约 */
export interface AbsenceWindow {
  id: string;
  mode: AbsenceMode;
  /** 将做的 N 件事 + 预计成本 */
  planned: { title: string; est_cost: number }[];
  /** 预算（¥） */
  budget: number;
  starts_at: string;
  ends_at: string;
  status: "active" | "ended" | "cancelled";
}

/** W11-I02 · 回归简报（决策区 ≤3 条，超出即系统没做压缩） */
export interface ReturnBrief {
  id: string;
  window_id: string;
  pending_count: number;
  done_count: number;
  cost: number;
  quota_used_pct: number;
  decisions: {
    id: string;
    level: "L0" | "L1";
    title: string;
    options: { label: string; recommended?: boolean; consequence?: string }[];
  }[];
  completed: { id: string; title: string; cost: number }[];
  /** 异常不藏：有就显示，没有则该区块不出现 */
  anomalies: string[];
  acknowledged: boolean;
}

export const MOCK_WINDOW: AbsenceWindow = {
  id: "aw-demo-001",
  mode: "autonomous",
  planned: [
    { title: "订阅审计：拉取账单并交叉比对", est_cost: 3.5 },
    { title: "竞品价格抓取（每日一轮）", est_cost: 2.0 },
    { title: "整理认知卡库索引", est_cost: 1.2 },
  ],
  budget: 20,
  starts_at: "2026-10-24T22:00:00+08:00",
  ends_at: "2026-10-25T08:00:00+08:00",
  status: "active",
};

export const MOCK_BRIEF: ReturnBrief = {
  id: "rb-demo-001",
  window_id: "aw-demo-001",
  pending_count: 2,
  done_count: 5,
  cost: 6.7,
  quota_used_pct: 34,
  decisions: [
    {
      id: "d1",
      level: "L0",
      title: "发现「视频号引流」类目标当前无模型可做（能力上限 62 分）",
      options: [
        { label: "暂缓立项", recommended: true, consequence: "不做：省下 ¥600 预算与 30 天观察期" },
        { label: "仍要立项", consequence: "需人工兜底产出，失败概率高" },
      ],
    },
    {
      id: "d2",
      level: "L1",
      title: "订阅审计发现 3 个重复订阅（¥47/月），取消需确认",
      options: [
        { label: "全部取消", recommended: true, consequence: "不做：每月多付 ¥47" },
        { label: "保留网易云", consequence: "月省 ¥31" },
      ],
    },
  ],
  completed: [
    { id: "c1", title: "竞品价格抓取（每日一轮）", cost: 2.0 },
    { id: "c2", title: "整理认知卡库索引", cost: 1.2 },
    { id: "c3", title: "心跳与快照巡检", cost: 0 },
  ],
  anomalies: ["设备「笔记本」在窗口内休眠 2 次，已自动避开派活"],
  acknowledged: false,
};
