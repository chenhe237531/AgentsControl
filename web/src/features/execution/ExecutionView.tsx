"use client";

/**
 * 执行页：默认只看异常。
 * 正常任务折叠成一行统计——"47 个正常 / 3 个卡住"比 50 行列表有用。
 * 异常分两档：卡住（stuck，可能自己恢复）是 warn；失败/死信（需要人处理）是 danger。
 * 任务模块（W03）未合并时，这里不造假数据，直接说明"等哪个窗口"。
 */

import Link from "next/link";
import { useState } from "react";

import { AsyncBoundary } from "@/components/AsyncBoundary";
import { Badge, Card, CardHeader, Drawer, StatTile } from "@/components/ui";
import { useTasks } from "@/lib/api/hooks";
import type { Task } from "@/lib/api/types";
import { timeAgo } from "@/lib/format";

/** stuck 是"等"（可自愈），failed/dead 是"坏了"（要人）；queued/done 不算异常 */
function stateTone(state: Task["state"]): "warn" | "danger" {
  return state === "stuck" ? "warn" : "danger";
}

const STATE_LABEL: Record<Task["state"], string> = {
  queued: "排队中",
  running: "运行中",
  done: "已完成",
  stuck: "卡住",
  failed: "失败",
  dead: "死信",
};

export function ExecutionView() {
  const tasks = useTasks();
  const rows = tasks.data?.items ?? [];
  const abnormal = rows.filter((t) => !["done", "queued"].includes(t.state));
  const normal = rows.length - abnormal.length;
  const [detail, setDetail] = useState<Task | null>(null);

  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-4">
        <h1 className="text-xl font-semibold tracking-tight">执行</h1>
        <p className="mt-1 text-[12px] text-muted">只看异常。正常任务不该占用你的注意力。</p>
      </header>

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatTile label="正常" value={rows.length ? normal : "—"} hint="运行中 / 排队中" tone="ok" />
        <StatTile
          label="异常"
          value={rows.length ? abnormal.length : "—"}
          hint="卡住 / 失败 / 超预算"
          tone={abnormal.length ? "danger" : "neutral"}
        />
        <StatTile label="死信" value={rows.filter((t) => t.state === "dead").length} hint="三次回收仍失败" tone="danger" />
      </div>

      <Card>
        <CardHeader
          title="异常任务"
          hint="点开看任务详情与所属会话"
          right={<Badge tone={abnormal.length ? "danger" : "ok"}>{abnormal.length}</Badge>}
        />
        <AsyncBoundary
          isLoading={tasks.isLoading}
          error={tasks.error}
          isEmpty={abnormal.length === 0}
          onRetry={() => void tasks.refetch()}
          emptyTitle={tasks.error ? "任务模块尚未接入" : "现在没有异常任务"}
          emptyHint={
            tasks.error
              ? "调度面（W03 任务队列与租约）合并后，这里会显示卡住/失败/超预算的任务；在此之前不展示假数据。"
              : `正常任务 ${normal} 个已折叠。`
          }
        >
          <ul className="divide-y divide-line">
            {abnormal.slice(0, 20).map((task) => (
              <li key={task.id}>
                <button
                  type="button"
                  onClick={() => setDetail(task)}
                  className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left hover:bg-canvas/60"
                >
                  <div className="min-w-0">
                    <div className="truncate text-[13px]">{task.title}</div>
                    <div className="mt-0.5 text-[11px] text-muted">
                      {STATE_LABEL[task.state]} · 更新于 {timeAgo(task.updated_at)}
                      {task.state === "stuck" ? " · 可能自愈，先观察" : " · 需要你处理"}
                    </div>
                  </div>
                  <Badge tone={stateTone(task.state)}>{STATE_LABEL[task.state]}</Badge>
                </button>
              </li>
            ))}
          </ul>
          {normal > 0 ? (
            <div className="border-t border-line px-4 py-2.5 text-[12px] text-muted">
              正常任务 {normal} 个已折叠——它们不需要你看。
            </div>
          ) : null}
        </AsyncBoundary>
      </Card>

      <Drawer open={detail !== null} title="任务详情" onClose={() => setDetail(null)}>
        {detail ? (
          <div className="space-y-4 text-[12px]">
            <div>
              <div className="text-[13px] font-medium leading-snug">{detail.title}</div>
              <div className="mt-1 flex items-center gap-2">
                <Badge tone={stateTone(detail.state)}>{STATE_LABEL[detail.state]}</Badge>
                <span className="text-[11px] text-muted">更新于 {timeAgo(detail.updated_at)}</span>
              </div>
            </div>
            <dl className="grid grid-cols-2 gap-3">
              <div>
                <dt className="text-muted">任务 ID</dt>
                <dd className="mt-0.5 font-mono text-[11px]">{detail.id}</dd>
              </div>
              <div>
                <dt className="text-muted">所属目标</dt>
                <dd className="mt-0.5 font-mono text-[11px]">{detail.goal_id ?? "未关联"}</dd>
              </div>
              <div>
                <dt className="text-muted">执行设备</dt>
                <dd className="mt-0.5 font-mono text-[11px]">{detail.device_id ?? "未分配"}</dd>
              </div>
            </dl>
            <p className="rounded-md border border-line bg-canvas px-3 py-2 leading-relaxed text-muted">
              最后事件流与会话 trace 由 W06 harvest 归集：可按 task_id 在 Langfuse
              中深链查看完整过程；项目视图里能看到它占用了哪段时间。
            </p>
            {detail.state === "stuck" ? (
              <p className="leading-relaxed text-warn">
                卡住任务先等一轮租约回收（默认 5 分钟）；回收两次仍卡死再转人工，不要每次都手动干预。
              </p>
            ) : null}
          </div>
        ) : null}
      </Drawer>

      <p className="mt-4 text-[11px] text-muted">
        会话视图由 Langfuse 提供（按 task_id 深链）；<Link className="text-brand" href="/projects">项目视图</Link>{" "}
        里能看到它占用了哪段时间。
      </p>
    </div>
  );
}
