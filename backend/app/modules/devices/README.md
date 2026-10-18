# devices · 设备接入

- **归属窗口**：W04
- **路由前缀**：`/devices`
- **职责**：注册与 token 哈希、心跳/增量/快照三通道、(node_id,seq) 幂等、suspect/offline 状态机、能力标签。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `POST /register`
- `POST /heartbeat`
- `POST /events`
- `POST /snapshot`
- `GET /`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
