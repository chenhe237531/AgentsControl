"use client";

/**
 * Drawer：右侧滑出详情面板。用于"列表里点一条看细节"，
 * 不做路由跳转——看完关掉，注意力不离开原列表。
 */

import { useEffect, type ReactNode } from "react";

export function Drawer({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-ink/40" onClick={onClose} role="presentation">
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="flex h-full w-full max-w-md flex-col border-l border-line bg-surface shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            className="rounded px-1.5 text-[13px] text-muted hover:text-ink"
          >
            ×
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-4 py-3">{children}</div>
      </aside>
    </div>
  );
}
