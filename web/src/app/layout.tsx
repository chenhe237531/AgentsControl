import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Providers } from "@/lib/query/providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Fleet Control",
  description: "用 500 个 Agent 放大一个人的带宽 —— 控制面",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
