"use client";

/**
 * 团队页：设备热力 + 能力矩阵。
 *
 * 设备接入（W04）未合并前只展示"设计能力矩阵"，并明确标注不是实时状态——
 * 让人误以为在线状态已可用，比空页面更糟。
 */

import { Badge, Card, CardHeader, DataTable, type DataTableColumn } from "@/components/ui";

interface MatrixRow {
  id: string;
  name: string;
  gui: boolean;
  orca: boolean;
  browser: boolean;
  docker: boolean;
  sleep: boolean;
  work: string;
}

const MATRIX: MatrixRow[] = [
  { id: "mac-host", name: "常用 Mac 主机", gui: true, orca: true, browser: true, docker: true, sleep: false, work: "开发、桌面自动化、Orca 宿主" },
  { id: "mac-mini", name: "远程 Mac mini", gui: true, orca: true, browser: true, docker: true, sleep: false, work: "常驻 Agent、夜间批处理" },
  { id: "laptop", name: "笔记本", gui: true, orca: true, browser: true, docker: false, sleep: true, work: "轻任务（绝不派 always_on）" },
  { id: "cloud", name: "云服务器", gui: false, orca: false, browser: true, docker: true, sleep: false, work: "抓取、计算、常驻" },
  { id: "3p", name: "第三方平台", gui: false, orca: false, browser: false, docker: false, sleep: false, work: "纯 API 能力（不接指令）" },
];

function Mark({ ok, label }: { ok: boolean; label: string }) {
  return ok ? <Badge tone="ok">{label}</Badge> : <span className="text-[11px] text-muted">—</span>;
}

const COLUMNS: DataTableColumn<MatrixRow>[] = [
  { key: "name", header: "设备", render: (row) => <span className="font-medium">{row.name}</span> },
  { key: "gui", header: "GUI", render: (row) => <Mark ok={row.gui} label="有" /> },
  { key: "orca", header: "Orca", render: (row) => <Mark ok={row.orca} label="有" /> },
  { key: "browser", header: "浏览器", render: (row) => <Mark ok={row.browser} label="有" /> },
  { key: "docker", header: "容器", render: (row) => <Mark ok={row.docker} label="有" /> },
  {
    key: "sleep",
    header: "可休眠",
    render: (row) => (row.sleep ? <Badge tone="warn">会</Badge> : <Badge tone="ok">不会</Badge>),
  },
  { key: "work", header: "适合派什么", render: (row) => <span className="text-muted">{row.work}</span> },
];

export function TeamView() {
  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-4">
        <h1 className="text-xl font-semibold tracking-tight">团队</h1>
        <p className="mt-1 text-[12px] text-muted">
          谁在干、谁该走。设备状态与 Agent 绩效需要 W04/W06 的数据；下面先给派活规则本身。
        </p>
      </header>

      <Card>
        <CardHeader
          title="能力矩阵（设计值）"
          hint="任务声明 requires；不匹配就不派 —— 尤其 always_on 绝不派给会休眠的机器"
          right={<Badge tone="warn">非实时</Badge>}
        />
        <DataTable columns={COLUMNS} rows={MATRIX} />
      </Card>

      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Card className="p-4">
          <div className="text-sm font-medium">可疑状态怎么判</div>
          <p className="mt-2 text-[12px] leading-relaxed text-muted">
            连续丢 3 次心跳 → <span className="text-warn">suspect</span>（立即停止派新活）；
            5 分钟无心跳 → <span className="text-danger">offline</span>（回收租约）。
            停派比回收更早，是为了避免把任务派给一台正在休眠的机器。
          </p>
        </Card>
        <Card className="p-4">
          <div className="text-sm font-medium">为什么现在没有实时设备格</div>
          <p className="mt-2 text-[12px] leading-relaxed text-muted">
            设备接入（W04：注册/心跳/增量/快照）尚未合并。这里刻意不显示假在线状态——
            错误的"在线"比没有数据更危险。
          </p>
        </Card>
      </div>
    </div>
  );
}
