"use client";

/**
 * 确认弹窗：不可逆操作（放行 L0、降级目标）的第二道闸。
 * - danger 变体用于"做了就回不去"的操作，按钮与边框一律 danger；
 * - Esc / 点击遮罩 = 取消；打开时焦点落在取消按钮上——
 *   危险操作默认选项必须是"不做"。
 */

import { useEffect, useRef } from "react";

import { Button } from "@/components/ui/index";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "brand" | "danger";
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "确认",
  cancelLabel = "取消",
  tone = "brand",
  onConfirm,
  onCancel,
  busy = false,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    // 异步聚焦：若由 Enter 键打开，同步聚焦会让本次按键的默认 click
    // 落在"取消"上，弹窗瞬间自我关闭
    const focusTimer = setTimeout(() => cancelRef.current?.focus(), 0);
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      clearTimeout(focusTimer);
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-ink/40 p-4"
      onClick={onCancel}
      role="presentation"
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        className="w-full max-w-sm rounded-card border border-line bg-surface p-4 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className={`text-[14px] font-semibold ${tone === "danger" ? "text-danger" : "text-ink"}`}>{title}</h2>
        <p className="mt-2 text-[12px] leading-relaxed text-muted">{body}</p>
        <div className="mt-4 flex justify-end gap-2">
          <Button ref={cancelRef} onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button variant={tone === "danger" ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>
            {busy ? "处理中…" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
