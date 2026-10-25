"use client";

/**
 * 缺席页：离开前交接 + 回来 5 分钟看到结果（INPUT-CHANNELS 通道 D 的前端半边）。
 *
 * 红线（W11 §3）：
 * - 回归简报决策区 ≤3 条，每条 20 秒能读完；有「全部知道了」一键归档；
 * - 缺席期内 L0/L1 一律 pending，绝不自动放行；
 * - 预算耗尽 → 停止派活只保留只读。
 *
 * 数据现状：W11 后端未排期，页面跑在冻结契约的演示数据上（顶部有横幅声明）；
 * 联调时只换 features/absence/mock.ts 的数据源，UI 不动。
 */

import { useState } from "react";

import { Badge, Button, Card, CardHeader, StatTile, useToast } from "@/components/ui";
import type { AbsenceMode, AbsenceWindow, ReturnBrief } from "@/features/absence/mock";
import { MOCK_BRIEF, MOCK_WINDOW } from "@/features/absence/mock";
import { shortDate, yuan } from "@/lib/format";

const MODE_LABEL: Record<AbsenceMode, string> = {
  autonomous: "自治（可派活，L0/L1 仍冻结待审）",
  readonly: "只读（只观察记录，不派任何活）",
};

function Brief({ brief, onAcknowledge }: { brief: ReturnBrief; onAcknowledge: () => void }) {
  return (
    <Card>
      <CardHeader
        title="回归简报"
        hint="离开期间发生了什么，压成 20 秒能读完的一段话"
        right={brief.acknowledged ? <Badge tone="ok">已归档</Badge> : <Badge tone="warn">待确认</Badge>}
      />
      <div className="space-y-4 p-4">
        {/* 头部一句话：N 件需要你定 / M 件已处理 / 成本与配额 */}
        <p className="text-[13px] leading-relaxed">
          离开期间：<b>{brief.pending_count} 件需要你定</b>，{brief.done_count} 件已处理完毕，
          花了 {yuan(brief.cost)}，预算用了 {brief.quota_used_pct}%。
        </p>

        {/* 异常不藏：有就显示 */}
        {brief.anomalies.length > 0 ? (
          <div className="rounded-card border border-warn/30 bg-warn/5 px-3 py-2 text-[12px] text-warn">
            {brief.anomalies.map((a) => (
              <p key={a}>⚠ {a}</p>
            ))}
          </div>
        ) : null}

        {brief.acknowledged ? (
          <p className="text-[12px] text-muted">简报已归档。决策区收起，需要时可从历史窗口回看。</p>
        ) : (
          <>
            {/* 决策区：选项 + 推荐 + 不做的后果 */}
            <div className="space-y-3">
              {brief.decisions.map((decision) => (
                <div key={decision.id} className="rounded-card border border-line p-3">
                  <div className="flex items-center gap-2">
                    <Badge tone={decision.level === "L0" ? "danger" : "warn"}>{decision.level}</Badge>
                    <span className="text-[13px] font-medium leading-snug">{decision.title}</span>
                  </div>
                  <div className="mt-2 space-y-1.5">
                    {decision.options.map((opt) => (
                      <div key={opt.label} className="flex items-start justify-between gap-3 text-[12px]">
                        <span>
                          {opt.recommended ? <Badge tone="brand">推荐</Badge> : null} {opt.label}
                          {opt.consequence ? <span className="ml-2 text-[11px] text-muted">{opt.consequence}</span> : null}
                        </span>
                        <div className="flex shrink-0 gap-1.5">
                          <Button variant={opt.recommended ? "primary" : "ghost"}>{opt.recommended ? "就这么办" : "选择"}</Button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {/* 已完成折叠为一行，可展开 */}
            <details className="rounded-card border border-line px-3 py-2">
              <summary className="cursor-pointer text-[12px] text-muted">
                已完成 {brief.done_count} 件（点击展开）
              </summary>
              <ul className="mt-2 space-y-1 text-[12px]">
                {brief.completed.map((item) => (
                  <li key={item.id} className="flex items-center justify-between">
                    <span>{item.title}</span>
                    <span className="tabular-nums text-muted">{item.cost ? yuan(item.cost) : "无消耗"}</span>
                  </li>
                ))}
              </ul>
            </details>

            <Button variant="primary" size="md" className="w-full" onClick={onAcknowledge}>
              全部知道了，归档
            </Button>
          </>
        )}
      </div>
    </Card>
  );
}

function WindowHandover({ window: win }: { window: AbsenceWindow }) {
  const toast = useToast();
  const [mode, setMode] = useState<AbsenceMode>(win.mode);
  const [budget, setBudget] = useState(win.budget);
  const [planned, setPlanned] = useState(win.planned);
  const estTotal = planned.reduce((sum, item) => sum + item.est_cost, 0);

  return (
    <Card>
      <CardHeader
        title="缺席窗口 · 离开前交接"
        hint="确认后系统按此执行；L0/L1 决策一律冻结到你回来"
        right={<Badge tone={mode === "autonomous" ? "warn" : "neutral"}>{mode === "autonomous" ? "自治" : "只读"}</Badge>}
      />
      <div className="space-y-4 p-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <StatTile label="模式" value={mode === "autonomous" ? "自治" : "只读"} hint={MODE_LABEL[mode]} />
          <StatTile label="将做" value={`${planned.length} 件`} hint={`预计 ${yuan(estTotal)}`} />
          <StatTile label="预算" value={yuan(budget)} hint={`${shortDate(win.starts_at)} → ${shortDate(win.ends_at)}`} />
        </div>

        <ul className="space-y-1.5 text-[12px]">
          {planned.map((item) => (
            <li key={item.title} className="flex items-center justify-between gap-3">
              <span className="min-w-0 truncate">{item.title}</span>
              <span className="shrink-0 tabular-nums text-muted">{yuan(item.est_cost)}</span>
            </li>
          ))}
        </ul>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            onClick={() => toast.push({ title: "窗口已确认", detail: `自治期间预计消耗 ${yuan(estTotal)}，L0/L1 冻结待审` })}
          >
            确认，开始缺席
          </Button>
          <Button onClick={() => { setBudget((b) => Math.max(5, b - 5)); toast.push({ title: "预算已下调", detail: "派活量会相应收紧" }); }}>
            改预算 −¥5
          </Button>
          <Button onClick={() => { setPlanned(planned.slice(0, 1)); toast.push({ title: "只保留第 1 项", detail: "其余任务转入明日队列" }); }}>
            只做第 1 项
          </Button>
          <Button
            variant={mode === "autonomous" ? "danger" : "ghost"}
            onClick={() => { setMode(mode === "autonomous" ? "readonly" : "autonomous"); toast.push({ title: `已切换为${mode === "autonomous" ? "只读" : "自治"}模式` }); }}
          >
            {mode === "autonomous" ? "切只读" : "切自治"}
          </Button>
        </div>

        {estTotal > budget ? (
          <p className="text-[12px] text-danger">预计消耗超出预算——窗口会先停派活、只保留只读，不会烧穿。</p>
        ) : null}
      </div>
    </Card>
  );
}

export function AbsenceView() {
  const [brief, setBrief] = useState<ReturnBrief>(MOCK_BRIEF);

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-4 rounded-card border border-warn/30 bg-warn/5 px-4 py-2.5 text-[12px] text-warn">
        演示数据——W11（输入通道与缺席契约）尚未排期；接口契约已按 W11-I01/I02 冻结，联调时只替换数据源。
      </div>

      <header className="mb-4">
        <h1 className="text-xl font-semibold tracking-tight">缺席</h1>
        <p className="mt-1 text-[12px] text-muted">回来 5 分钟看到结果；无人值守绝不放行高风险动作。</p>
      </header>

      <div className="space-y-5">
        <Brief brief={brief} onAcknowledge={() => setBrief({ ...brief, acknowledged: true })} />
        <WindowHandover window={MOCK_WINDOW} />
      </div>
    </div>
  );
}
