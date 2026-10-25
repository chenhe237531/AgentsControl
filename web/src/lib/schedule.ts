/**
 * 决策排班（PRODUCT-DESIGN-v0.2 §3.4 的前端本地规则）。
 *
 * 后端带宽裁决（/approvals/bandwidth 的 verdict.night_hold_l0）覆盖深夜熔断；
 * 这里补充客户端时段建议，供 UI 高亮与二次确认使用。
 * TODO(W07-I07): 后端决策排班接口就绪后，本文件的时段结论应改为消费接口值，
 * 本地规则只做接口不可用时的降级。
 */

export type DecisionSlot = "morning" | "afternoon" | "evening" | "night";

export function decisionSlot(now: Date = new Date()): DecisionSlot {
  const h = now.getHours();
  if (h >= 23 || h < 7) return "night";
  if (h < 12) return "morning";
  if (h < 18) return "afternoon";
  return "evening";
}

export const SLOT_LABEL: Record<DecisionSlot, string> = {
  morning: "上午 · 宜做 L0 战略决策",
  afternoon: "下午 · 宜处理例外审批",
  evening: "晚间 · 宜记体感，不宜做不可逆决策",
  night: "深夜 · L0 决策一律待明日确认",
};

/**
 * UI 行为裁决：
 * - night：L0 决策不出结论（与服务端 night_hold_l0 双保险）；
 * - afternoon/evening：L0 属不可逆操作，通过前需二次确认；
 * - morning：L0 入口高亮（"宜决策"）。
 */
export function l0Rule(slot: DecisionSlot): {
  blocked: boolean;
  needsConfirm: boolean;
  highlight: boolean;
} {
  switch (slot) {
    case "night":
      return { blocked: true, needsConfirm: false, highlight: false };
    case "morning":
      return { blocked: false, needsConfirm: false, highlight: true };
    default:
      return { blocked: false, needsConfirm: true, highlight: false };
  }
}
