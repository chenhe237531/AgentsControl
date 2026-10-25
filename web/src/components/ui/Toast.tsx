"use client";

/**
 * Toast：操作成功的轻反馈。只用于"做成了"这类不需要用户立即处理的消息；
 * 需要用户处理的事走页面内状态，不走 Toast——告警不是装饰。
 * Provider 挂在 Providers 树根部，页面用 useToast() 取 push。
 */

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

type ToastTone = "ok" | "danger";

interface ToastItem {
  id: number;
  title: string;
  detail?: string;
  tone: ToastTone;
}

interface ToastContextValue {
  push: (toast: { title: string; detail?: string; tone?: ToastTone }) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const TONE_CLASS: Record<ToastTone, string> = {
  ok: "border-ok/30 bg-surface",
  danger: "border-danger/30 bg-surface",
};

const DOT_CLASS: Record<ToastTone, string> = {
  ok: "bg-ok",
  danger: "bg-danger",
};

const AUTO_DISMISS_MS = 4000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback<ToastContextValue["push"]>(
    ({ title, detail, tone = "ok" }) => {
      const id = ++seq.current;
      setItems((prev) => [...prev.slice(-2), { id, title, detail, tone }]);
      setTimeout(() => dismiss(id), AUTO_DISMISS_MS);
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-72 flex-col gap-2"
      >
        {items.map((toast) => (
          <div
            key={toast.id}
            className={`pointer-events-auto flex items-start gap-2 rounded-card border px-3 py-2 shadow-sm ${TONE_CLASS[toast.tone]}`}
          >
            <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${DOT_CLASS[toast.tone]}`} />
            <div className="min-w-0">
              <div className="text-[12px] font-medium text-ink">{toast.title}</div>
              {toast.detail ? <div className="mt-0.5 text-[11px] text-muted">{toast.detail}</div> : null}
            </div>
            <button
              type="button"
              aria-label="关闭提示"
              onClick={() => dismiss(toast.id)}
              className="ml-auto shrink-0 rounded px-1 text-[11px] text-muted hover:text-ink"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 ToastProvider 内使用");
  return ctx;
}
