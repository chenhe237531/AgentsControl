"use client";

/**
 * 数据表：账本、资产这类"行式数据"的统一表达。
 * - 列定义 sortable 才出排序按钮，默认不排；
 * - loading 走骨架行，empty 走 EmptyState，不在页面各写一套；
 * - 行渲染交给 columns[].render，本组件不管业务。
 */

import { useMemo, useState, type ReactNode } from "react";

import { EmptyState, Skeleton } from "@/components/ui/index";

export interface DataTableColumn<T> {
  key: string;
  header: string;
  align?: "left" | "right";
  sortable?: boolean;
  /** 数值列排序取值；不填则按 render 文本 */
  sortValue?: (row: T) => number | string;
  render?: (row: T) => ReactNode;
}

export function DataTable<T extends { id: string }>({
  columns,
  rows,
  loading = false,
  emptyTitle = "暂无数据",
  emptyHint,
  skeletonRows = 5,
}: {
  columns: DataTableColumn<T>[];
  rows: T[];
  loading?: boolean;
  emptyTitle?: string;
  emptyHint?: string;
  skeletonRows?: number;
}) {
  const [sort, setSort] = useState<{ key: string; dir: "asc" | "desc" } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col) return rows;
    const value = (row: T): number | string => col.sortValue?.(row) ?? String(col.render?.(row) ?? "");
    return [...rows].sort((a, b) => {
      const va = value(a);
      const vb = value(b);
      const cmp = typeof va === "number" && typeof vb === "number"
        ? va - vb
        : String(va).localeCompare(String(vb), "zh-CN");
      return sort.dir === "asc" ? cmp : -cmp;
    });
  }, [rows, sort, columns]);

  const toggleSort = (key: string): void => {
    setSort((prev) =>
      prev?.key === key ? (prev.dir === "asc" ? { key, dir: "desc" } : null) : { key, dir: "asc" },
    );
  };

  if (loading) {
    return (
      <div className="p-4">
        <Skeleton rows={skeletonRows} />
      </div>
    );
  }

  if (rows.length === 0) {
    return <EmptyState title={emptyTitle} hint={emptyHint} />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[12px]">
        <thead>
          <tr className="border-b border-line text-muted">
            {columns.map((col) => (
              <th
                key={col.key}
                scope="col"
                className={`px-3 py-2 font-medium ${col.align === "right" ? "text-right" : "text-left"}`}
                aria-sort={sort?.key === col.key ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
              >
                {col.sortable ? (
                  <button
                    type="button"
                    onClick={() => toggleSort(col.key)}
                    className="inline-flex items-center gap-0.5 hover:text-ink"
                  >
                    {col.header}
                    <span aria-hidden className="text-[9px]">
                      {sort?.key === col.key ? (sort.dir === "asc" ? "▲" : "▼") : "↕"}
                    </span>
                  </button>
                ) : (
                  col.header
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={row.id} className="border-b border-line/60 last:border-0 hover:bg-canvas/60">
              {columns.map((col) => (
                <td
                  key={col.key}
                  className={`px-3 py-2 align-top ${col.align === "right" ? "text-right tabular-nums" : ""}`}
                >
                  {col.render ? col.render(row) : String((row as Record<string, unknown>)[col.key] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
