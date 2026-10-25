"use client";

/**
 * 甘特画布：独立成动态导入 chunk（ssr:false）。
 *
 * 为什么拆出来：@xyflow/react 整包 ~53KB gzip，静态引入会把项目页的
 * 首屏 JS 增量顶穿 30KB 预算（docs/UI-OPTIMIZATION-PLAN-v1.html §4.5）。
 * 画布非首屏关键内容，懒加载 + 同高度骨架即可；刻度与泳道标签留在
 * ProjectsView（纯 div），不依赖本 chunk，1:1 对齐不受影响。
 */

import { memo, useMemo } from "react";
import {
  Background,
  Controls,
  ReactFlow,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

type Kind = "normal" | "collision" | "blocked" | "deviation";

export interface GanttData extends Record<string, unknown> {
  label: string;
  issue: string;
  kind: Kind;
  hours: number;
  note: string;
}

type GanttNode = Node<GanttData, "gantt">;

const KIND_STYLE: Record<Kind, { bg: string; border: string; text: string }> = {
  normal: { bg: "rgb(var(--brand))", border: "rgb(var(--brand))", text: "var(--on-strong)" },
  collision: { bg: "rgb(var(--danger))", border: "rgb(var(--danger))", text: "var(--on-strong)" },
  blocked: { bg: "rgb(var(--warn))", border: "rgb(var(--warn))", text: "var(--on-strong)" },
  deviation: { bg: "var(--caution)", border: "var(--caution)", text: "var(--caution-ink)" },
};

const LANES = ["Agent A", "Agent B", "Agent C"];
const PX_PER_HOUR = 56;
const LANE_H = 64;
const START_HOUR = 9;

// 示例数据（09:00 起算）：接入 W06 后由 sessions API 生成
const DEMO: GanttData[] = [
  { label: "W01 身份内核", issue: "#1", kind: "normal", hours: 3, note: "正常交付" },
  { label: "W03 任务队列", issue: "#8", kind: "normal", hours: 2.5, note: "正常交付" },
  { label: "W02 目标面", issue: "#2", kind: "collision", hours: 2, note: "与 W02 同时改了 goals/service.py" },
  { label: "W02 目标面", issue: "#2", kind: "collision", hours: 1.5, note: "同上（另一条分支）" },
  { label: "W07 账本", issue: "#2", kind: "blocked", hours: 3, note: "等迁移合并头，等了 2h10m" },
  { label: "W05 Go Agent", issue: "#7", kind: "deviation", hours: 2, note: "改了 deploy/ 下的无关文件" },
];

const GanttBar = memo(function GanttBar({ data }: NodeProps<GanttNode>) {
  const style = KIND_STYLE[data.kind];
  return (
    <div
      className="flex h-7 items-center gap-1 overflow-hidden rounded px-2 text-[10px] shadow-sm"
      style={{ background: style.bg, border: `1px solid ${style.border}`, color: style.text, width: data.hours * PX_PER_HOUR }}
      title={`${data.label} ${data.issue} · ${data.note}`}
    >
      <span className="truncate font-medium">{data.label}</span>
      <span className="shrink-0 opacity-70">{data.issue}</span>
    </div>
  );
});

const nodeTypes: NodeTypes = { gantt: GanttBar };

export default function GanttCanvas() {
  const nodes = useMemo<GanttNode[]>(
    () =>
      DEMO.map((item, index) => {
        const lane = index % LANES.length;
        const startHour = START_HOUR + Math.floor(index / LANES.length) * 2 + (index % 2);
        return {
          id: `demo-${index}`,
          type: "gantt",
          position: { x: (startHour - START_HOUR) * PX_PER_HOUR, y: lane * LANE_H },
          data: item,
          draggable: false,
          selectable: false,
        };
      }),
    [],
  );

  return (
    <div style={{ height: 236, marginTop: 0 }}>
      <ReactFlow
        nodes={nodes}
        edges={[]}
        nodeTypes={nodeTypes}
        onlyRenderVisibleElements
        fitView={false}
        defaultViewport={{ x: 0, y: 0, zoom: 1 }}
        minZoom={0.5}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnScroll
        zoomOnDoubleClick={false}
      >
        <Background gap={16} size={1} color="rgb(var(--line))" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
