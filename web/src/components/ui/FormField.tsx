"use client";

/**
 * 表单字段组：label + 控件 + 错误，一行式（控制台高密度）。
 * 控件作为 children 传入，本组件只管排布与无障碍关联。
 */

import { useId, type ReactNode } from "react";

export function FormField({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  children: (ids: { fieldId: string; describedBy: string | undefined }) => ReactNode;
}) {
  const fieldId = useId();
  const hintId = useId();
  const errorId = useId();
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;

  return (
    <div className="text-[12px]">
      <label htmlFor={fieldId} className="text-muted">
        {label}
      </label>
      <div className="mt-1">{children({ fieldId, describedBy })}</div>
      {hint && !error ? (
        <p id={hintId} className="mt-1 text-[11px] text-muted">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} role="alert" className="mt-1 text-[11px] text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
