# identity · 身份与权限

- **归属窗口**：W01
- **路由前缀**：`/auth`
- **职责**：登录/刷新/登出、RBAC 判定、会话与令牌吊销；单用户白名单，不开放注册。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `POST /login`
- `POST /refresh`
- `POST /logout`
- `GET /me`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
