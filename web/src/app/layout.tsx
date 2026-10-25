import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Providers } from "@/lib/query/providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Fleet Control",
  description: "用 500 个 Agent 放大一个人的带宽 —— 控制面",
};

/**
 * 主题预置：在任何样式应用前读 localStorage，退回系统偏好。
 * 内联在 <head> 里执行，避免暗色用户首屏白闪。
 */
const THEME_INIT = `(function(){try{var t=localStorage.getItem("af-theme");if(t==="dark"||(!t&&window.matchMedia("(prefers-color-scheme: dark)").matches)){document.documentElement.classList.add("dark")}}catch(e){}})()`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
