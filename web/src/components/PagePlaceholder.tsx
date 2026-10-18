import type { ReactNode } from "react";

export function PagePlaceholder({
  title,
  question,
  owner,
  children,
}: {
  title: string;
  question: string;
  owner: string;
  children?: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-6">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        <p className="mt-1 text-sm text-muted">{question}</p>
      </header>

      <section className="placeholder-card p-6">
        <div className="text-sm font-medium">骨架占位页</div>
        <p className="mt-2 max-w-2xl text-[13px] leading-relaxed text-muted">
          本页由 <span className="font-mono text-ink">{owner}</span> 按{" "}
          <span className="font-mono text-ink">plan/{owner}.md</span> 的顺序实现。骨架阶段只保证：
          路由可达、导航一致、API 客户端可用、错误态有统一映射。
        </p>
        {children ? <div className="mt-4">{children}</div> : null}
      </section>
    </div>
  );
}
