"use client";

/**
 * 账本页：双币种同屏。
 *
 * 左钱（六分项）右注意力（min/outcome），必须在同一视线内可比较——
 * 否则"这个月花了多少"和"我搭进去多少时间"永远对不上。
 * 注意力无产出时显示"未开始记录"，不显示 0（0 会被读成"很高效"）。
 */

import { useMemo, useState } from "react";

import { AsyncBoundary } from "@/components/AsyncBoundary";
import { Badge, Button, Card, CardHeader, ProgressBar, Segmented, StatTile, useToast } from "@/components/ui";
import { useLedger } from "@/lib/api/hooks";
import { CATEGORY_LABEL, yuan } from "@/lib/format";
import { useMounted } from "@/lib/use-mounted";

const CATEGORIES = ["subscription", "tools", "device", "rework", "qc", "labor"] as const;

function recentPeriods(count = 4): { value: string; label: string }[] {
  const out: { value: string; label: string }[] = [];
  const now = new Date();
  for (let i = 0; i < count; i += 1) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    out.push({ value, label: i === 0 ? "本月" : `${d.getMonth() + 1} 月` });
  }
  return out;
}

/** 前端导出本期汇总 CSV（明细级对账导出待 W07-I06，这里先给汇总口径） */
function exportCsv(period: string, data: NonNullable<ReturnType<typeof useLedger>["data"]>): void {
  const rows: string[][] = [
    ["期间", period],
    ["币种", data.currency ?? "CNY"],
    [],
    ["分类", "金额"],
    ...(["subscription", "tools", "device", "rework", "qc", "labor"] as const).map((c) => [
      CATEGORY_LABEL[c] ?? c,
      (data.categories[c] ?? 0).toFixed(2),
    ]),
    ["合计", data.total.toFixed(2)],
    [],
    ["注意力投入(分钟)", String(data.attention?.minutes ?? "")],
    ["达标产出", String(data.attention?.outcomes ?? "")],
    ["min/outcome", data.attention?.min_per_outcome != null ? data.attention.min_per_outcome.toFixed(2) : ""],
  ];
  const csv = `\uFEFF${rows.map((r) => r.join(",")).join("\n")}`;
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `ledger-${period}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export function LedgerView() {
  // 月份列表依赖本地时钟（且构建容器 TZ=UTC）：mount 后再算，见 use-mounted.ts
  const mounted = useMounted();
  const periods = useMemo(() => (mounted ? recentPeriods() : []), [mounted]);
  const [selected, setSelected] = useState<string | undefined>(undefined);
  const period = selected ?? periods[0]?.value;
  const ledger = useLedger(period);
  const toast = useToast();
  const attention = ledger.data?.attention;
  const total = ledger.data?.total ?? 0;

  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">账本</h1>
          <p className="mt-1 text-[12px] text-muted">
            两个币种：钱可以再赚，你的下午不能。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={() => {
              if (!ledger.data) return;
              exportCsv(period ?? "", ledger.data);
              toast.push({ title: "已导出本期 CSV", detail: `ledger-${period}.csv（汇总口径）` });
            }}
            disabled={!ledger.data}
          >
            导出本期
          </Button>
          <Segmented options={periods} value={period ?? ""} onChange={setSelected} />
        </div>
      </header>

      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile label="总支出" value={yuan(total)} hint={ledger.data?.period ?? "—"} />
        <StatTile
          label="注意力"
          value={attention ? `${attention.minutes} min` : "—"}
          hint="期内投入"
        />
        <StatTile
          label="min / 产出"
          value={attention?.min_per_outcome != null ? attention.min_per_outcome.toFixed(1) : "未开始记录"}
          hint={attention?.outcomes != null ? `${attention.outcomes} 个达标产出` : "还没有达标产出"}
          tone={attention?.min_per_outcome != null && attention.min_per_outcome > 20 ? "warn" : "neutral"}
        />
        <StatTile label="返工占比" value={total ? `${Math.round(((ledger.data?.categories.rework ?? 0) / total) * 100)}%` : "—"} hint="越低越好" />
      </div>

      <AsyncBoundary
        isLoading={ledger.isLoading}
        error={ledger.error}
        isEmpty={!ledger.data}
        onRetry={() => void ledger.refetch()}
        emptyTitle="本期还没有账目"
        emptyHint="任务完成、订阅登记、评测跑批都会自动产生条目。"
      >
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          <Card>
            <CardHeader title="钱 · 六分项" hint="只追加不修改；冲正另记负数" right={<Badge tone="brand">{yuan(total)}</Badge>} />
            <div className="space-y-3 p-4">
              {CATEGORIES.map((category) => {
                const value = ledger.data?.categories[category] ?? 0;
                const share = total > 0 ? (value / total) * 100 : 0;
                return (
                  <div key={category}>
                    <div className="flex items-center justify-between text-[12px]">
                      <span>{CATEGORY_LABEL[category]}</span>
                      <span className="tabular-nums text-muted">
                        {yuan(value)} · {share.toFixed(0)}%
                      </span>
                    </div>
                    <div className="mt-1">
                      <ProgressBar value={value} max={total || 1} tone={category === "rework" ? "danger" : "brand"} />
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>

          <Card>
            <CardHeader title="注意力" hint="分钟 ÷ 达标产出；无产出不显示 0" />
            <div className="space-y-4 p-4">
              <div className="rounded-md border border-line bg-canvas px-3 py-4 text-center">
                <div className="text-[11px] text-muted">min / outcome</div>
                <div className="mt-1 text-3xl font-semibold tabular-nums">
                  {attention?.min_per_outcome != null ? attention.min_per_outcome.toFixed(1) : "—"}
                </div>
                <div className="mt-1 text-[11px] text-muted">
                  {attention?.min_per_outcome != null ? "目标 ≤ 8 min" : "未开始记录（需要达标产出做分母）"}
                </div>
              </div>
              <dl className="grid grid-cols-2 gap-3 text-[12px]">
                <div>
                  <dt className="text-muted">投入分钟</dt>
                  <dd className="mt-0.5 font-medium tabular-nums">{attention?.minutes ?? "—"}</dd>
                </div>
                <div>
                  <dt className="text-muted">达标产出</dt>
                  <dd className="mt-0.5 font-medium tabular-nums">
                    {attention?.outcomes != null ? attention.outcomes : "未开始记录"}
                  </dd>
                </div>
              </dl>
              <p className="text-[11px] leading-relaxed text-muted">
                这一栏的价值不在数字本身：它逼你承认某个目标"钱没花多少，但吃掉了我一整周"。
              </p>
            </div>
          </Card>
        </div>
      </AsyncBoundary>
    </div>
  );
}
