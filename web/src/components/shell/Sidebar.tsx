"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { NAV_GROUPS, navByGroup } from "@/config/nav";

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-surface px-3 py-4">
      <div className="px-2 pb-4">
        <div className="text-sm font-semibold tracking-tight">Agent Fleet Control</div>
        <div className="mt-1 text-[11px] text-muted">控制面 · 任务 / 预算 / 验收</div>
      </div>

      <nav className="flex flex-1 flex-col gap-4">
        {NAV_GROUPS.map((group) => (
          <div key={group}>
            <div className="px-2 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted">
              {group}
            </div>
            <ul className="flex flex-col gap-0.5">
              {navByGroup(group).map((item) => {
                const active = pathname === item.href;
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      title={item.question}
                      className={`flex items-center justify-between rounded-md px-2 py-1.5 text-[13px] transition-colors ${
                        active ? "bg-brand/10 font-medium text-brand" : "text-ink hover:bg-canvas"
                      }`}
                    >
                      <span>{item.label}</span>
                      {item.stub ? (
                        <span className="rounded bg-canvas px-1 text-[10px] text-muted">占位</span>
                      ) : null}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="px-2 pt-4 text-[10px] text-muted">
        v{process.env.NEXT_PUBLIC_APP_VERSION ?? "0.1.0"}
      </div>
    </aside>
  );
}
