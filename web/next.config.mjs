/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Docker 运行态只需要 standalone 产物（见 web/Dockerfile 的 runner 阶段）
  output: "standalone",
  poweredByHeader: false,
  // 容器部署需要 standalone 产物（web/Dockerfile 依赖它）
  output: "standalone",
  // 前端独立部署（Vercel Preview / 腾讯云同源反代）时，API 地址一律走环境变量
  env: {
    NEXT_PUBLIC_APP_VERSION: process.env.npm_package_version ?? "0.1.0",
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
        ],
      },
    ];
  },
};

export default nextConfig;
