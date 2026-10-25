import type { ReactNode } from "react";

import { MetricsTracker } from "@/components/MetricsTracker";
import { Sidebar } from "@/components/shell/Sidebar";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full min-h-screen">
      <Sidebar />
      <main className="min-w-0 flex-1 overflow-x-hidden px-4 py-5 sm:px-8 sm:py-6">{children}</main>
      <MetricsTracker />
    </div>
  );
}
