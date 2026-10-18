# ADR 0003 · 会话吊销与 `revoked_at` 约定

- 状态：已采纳（W01-I03）
- 日期：2026-10-18
- 相关：W01-I01（令牌轮换）、W01-I02（RBAC）、W04（设备令牌）

## 背景

系统是单主体（owner）多设备的形态：笔记本、VPS、手机可能同时在线。需要回答三个问题：

1. 某个设备丢失后，能不能只在服务端把它的续期能力掐掉？
2. 吊销之后，攻击者手上那枚已经签发的访问令牌怎么办？
3. 事后能不能查清「这个会话是怎么没的」——是用户主动登出、令牌重放、还是设备被吊销？

## 决策

### 1. 吊销只作用于刷新令牌（`refresh_tokens`）

会话的身份就是 `refresh_tokens.session_id`。吊销 = 把该 `session_id` 下所有未吊销行的
`revoked_at` 置为当前时间，并写 `revoked_reason`。**不删行**：删除会丢失审计轨迹，
而保留一行已吊销记录的存储代价可以忽略。

访问令牌是无状态 JWT，**吊销后仍然有效，最多 `access_token_ttl_seconds`（15 分钟）**。

这是刻意的取舍，不是遗漏：

- 让 access token 立刻失效，就必须每个请求查一次库或查一次黑名单（Redis），
  等于把「无状态」这个唯一的好处换掉，换来的是 15 分钟的收敛窗口；
- 15 分钟的窗口对「笔记本丢失」这个主要场景是可接受的：攻击者最多再读 15 分钟，
  而真正的破坏性操作都有 `approvals:write` / `ledger:write` 这类 owner 专属 scope 兜着；
- 需要更强保证时的演进路径（**未实现，别以为已经有了**）：网关侧短 TTL 黑名单，
  键为 `jti`，TTL 等于访问令牌剩余寿命。届时本 ADR 需要改写。

**因此：任何「吊销后立刻 401」的验收都是错的。** 单测
`test_access_token_survives_logout_until_expiry` 就是用来钉住这个语义的。

### 2. `revoked_reason` 是枚举，不是自由文本

| 值 | 谁写 | 含义 |
|---|---|---|
| `logout` | W01 `POST /auth/logout` | 用户在这个设备上主动登出 |
| `revoke_all` | W01 `POST /auth/sessions/revoke-all` | 用户踢掉其它设备（或全部） |
| `refresh_replayed` | W01 刷新链路 | 旧刷新令牌被重放，整链吊销 |
| `device_revoked` | **W04** 设备侧 | 设备被吊销，其名下会话一并失效 |

W04 的约定（只读依赖，W01 不实现设备逻辑）：

```
UPDATE refresh_tokens
   SET revoked_at = now(), revoked_reason = 'device_revoked'
 WHERE session_id = ANY(:device_session_ids) AND revoked_at IS NULL;
-- 并为每个 session 追加一条 auth_events(event='device_revoked')
```

写完即生效：身份侧的刷新链路只看 `revoked_at`，不需要为设备吊销加任何分支
（`test_device_revoked_session_blocks_refresh_and_shows_reason` 覆盖）。

### 3. `auth_events`：认证域的唯一事件流水

只追加。字段：`subject`、`session_id`（可空）、`event`、`detail`、`client_ip`、
`user_agent`、`created_at`。**不存令牌原文或哈希**。

事件名白名单在 `models.AUTH_EVENT_KINDS`（`login` / `refresh` / `refresh_replayed` /
`logout` / `session_revoked` / `device_revoked`），写入处校验，拼错直接失败——
否则半年后会出现 `logout` / `logged_out` / `Logout` 三种拼法，审计查询就没法写了。

刻意不加 DB CHECK 约束：事件种类会随需求增长，约束会让每次新增都要迁移一次。

### 4. 主体只能吊销自己的会话

`POST /auth/sessions/revoke-all` 的 `subject` 取自**已验签的令牌**，不接受请求体传入；
`keep_current` 由 claims 里的 `sid` 决定。令牌缺 `sid` 时若要保留当前会话，直接 401
（`invalid_token`），而不是「保留失败就全吊销」——后者会把用户自己踢下线还不告诉他。

## 后果

- 正向：吊销逻辑集中在一处（`revoke_session` / `revoke_subject_sessions`），
  设备侧只需写 `revoked_at`，不需要与身份模块联调；
- 正向：`GET /auth/sessions` 能直接回答「我在哪些设备上登录过、哪个被踢了」；
- 代价：15 分钟的访问令牌残留窗口（见上，需要更强保证时走网关黑名单）；
- 代价：`auth_events` 只增不减，需要定期归档（未实现，写入量级为「登录级」，
  单用户系统预计每年几千行，短期不构成问题）。
