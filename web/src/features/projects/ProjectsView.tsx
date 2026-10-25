"use client";

/**
 * 项目视图：横轴时间、纵轴 Agent 的甘特图。
 *
 * 这不是"看它在跑"，是发现三件事：
 *   撞车（同文件被两个 Agent 改）/ 阻塞（等依赖或审批超时）/ 偏离（改了无关文件）。
 *
 * 数据来源：W06 的 execution_sessions 尚未合并，当前用明确标注的示例数据；
 * 三种标记的判定逻辑与颜色语义已固定，接入后只换数据源，不改视图。
 *
 * 对齐约定：不开 fitView（缩放会破坏刻度与坐标的 1:1 对应），
 * 刻度、泳道、节点三者都从 PX_PER_HOUR / LANE_H 两个常量推导。
 * 画布本体（@xyflow/react）是动态 chunk：非首屏关键内容，不占首屏 JS 预算。
 */

import dynamic from "next/dynamic";

import { Badge, Card, CardHeader } from "@/components/ui";

const PX_PER_HOUR = 56;
const LANE_H = 64;
const AXIS_H = 24;
const START_HOUR = 9;
const END_HOUR = 17;
const CANVAS_H = 236;

const LANES = ["Agent A", "Agent B", "Agent C"];

const GanttCanvas = dynamic(() => import("./GanttCanvas"), {
  ssr: false,
  loading: () => (
    <div
      className="flex items-center justify-center rounded border border-dashed border-line bg-canvas text-[11px] text-muted"
      style={{ height: CANVAS_H }}
      role="status"
    >
      甘特画布加载中…
    </div>
  ),
});

/** 刻度：每 2 小时一格，宽度 = 2 × PX_PER_HOUR，与画布坐标 1:1 */
const TICKS = Array.from({ length: (END_HOUR - START_HOUR) / 2 + 1 }, (_, i) => ({
  label: `${String(START_HOUR + i * 2).padStart(2, "0")}:00`,
}));

export function ProjectsView() {
  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">项目视图</h1>
          <p className="mt-1 text-[12px] text-muted">
            横轴时间、纵轴 Agent。要发现的是撞车、阻塞、偏离，不是"看着热闹"。
          </p>
        </div>
        <Badge tone="warn">示例数据 · W06 接入后替换</Badge>
      </header>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1.35fr_0.65fr]">
        <Card className="overflow-hidden">
          <CardHeader title="甘特" hint="每根条 = 一个 Issue 的占用区间；滚轮平移查看" />
          <div className="flex">
            <div className="w-16 shrink-0 border-r border-line" style={{ paddingTop: AXIS_H }}>
              {LANES.map((lane) => (
                <div key={lane} className="flex items-center justify-end pr-2 text-[10px] text-muted" style={{ height: LANE_H }}>
                  {lane}
                </div>
              ))}
            </div>
            <div className="min-w-0 flex-1">
              <div className="ml-1 flex text-[10px] text-muted" style={{ height: AXIS_H, alignItems: "flex-end" }}>
                {TICKS.map((tick) => (
                  <span key={tick.label} className="shrink-0" style={{ width: 2 * PX_PER_HOUR }}>
                    {tick.label}
                  </span>
                ))}
              </div>
              <GanttCanvas />
            </div>
          </div>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader title="三种信号" hint="颜色只表达一件事，避免误读" />
            <ul className="space-y-3 p-4 text-[12px]">
              <li>
                <span className="mr-2 inline-block h-2.5 w-2.5 rounded-sm" style={{ background: "rgb(var(--danger))" }} />
                <span className="font-medium">撞车</span>
                <p className="mt-1 text-[11px] text-muted">同一文件被两个 Agent 改。要做的不是"协调"，是改边界表。</p>
              </li>
              <li>
                <span className="mr-2 inline-block h-2.5 w-2.5 rounded-sm" style={{ background: "rgb(var(--warn))" }} />
                <span className="font-medium">阻塞</span>
                <p className="mt-1 text-[11px] text-muted">等依赖或等审批超时。条上标红并显示等了多久。</p>
              </li>
              <li>
                <span className="mr-2 inline-block h-2.5 w-2.5 rounded-sm" style={{ background: "var(--caution)" }} />
                <span className="font-medium">偏离</span>
                <p className="mt-1 text-[11px] text-muted">改了 Issue 之外的无关文件。通常是 Agent 在"顺手优化"。</p>
              </li>
            </ul>
          </Card>

          <Card className="p-4">
            <div className="text-[12px] font-medium">为什么现在只有示例</div>
            <p className="mt-2 text-[11px] leading-relaxed text-muted">
              会话数据来自 W06 的 Worker/Orca 会话表，任务占用区间来自 W03。两个模块尚未合并，
              这里先固定视图与判定规则，接入时只替换画布组件里的示例数据源。
            </p>
          </Card>
        </div>
      </div>
    </div>
  );
}
