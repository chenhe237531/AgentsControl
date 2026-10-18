import type { Config } from "tailwindcss";

/**
 * 只有一套设计令牌：颜色集中在 globals.css 的 CSS 变量里，
 * Tailwind 只做映射，避免窗口各自硬编码色值。
 */
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "var(--canvas)",
        surface: "var(--surface)",
        line: "var(--line)",
        ink: "var(--ink)",
        muted: "var(--muted)",
        brand: "var(--brand)",
        ok: "var(--ok)",
        warn: "var(--warn)",
        danger: "var(--danger)",
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      borderRadius: {
        card: "10px",
      },
    },
  },
  plugins: [],
};

export default config;
