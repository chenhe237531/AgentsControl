# ADR 0002 · 认证、限流与暴露面模型

- 状态：Accepted（W01-I04 落地）
- 日期：2026-10-18
- 相关：`docs/adr/0003-session-revocation.md`、`modules/identity/*`、`core/ratelimit.py`、`core/middleware.py`
- 决策者：W01（身份与工程内核）

## 1. 问题

其它 9 个窗口都要复用同一套「你是谁、能干什么、能被刷多少」的判定。
若各窗口自己写鉴权/限流，最终会出现三套语义（尤其「被限流时返回什么」）。
本 ADR 固定四件事：**令牌、授权、限流、暴露面**；改其中之一都要新开 ADR。

## 2. 令牌模型

| 项 | 选择 | 理由 |
|---|---|---|
| 访问令牌 | 无状态 JWT，15 min，`sub`/`role`/`scopes`/`sid` 四个必需 claim | 校验不查库，避免每个请求一次 PG 往返 |
| 刷新令牌 | 不透明随机串，14 天，**只存 sha256**，每次刷新轮换 | 库泄露也拿不到可用令牌；轮换让「旧 token 重放」可被检出 |
| 重放 | 旧刷新令牌再次出现即吊销**整条会话链** | 重放意味着泄露，继续放行等于给攻击者续命 |
| 登出 | 只吊销刷新令牌 | 无状态 access token 无法撤回（残留 ≤15 min），见 ADR 0003 |

`POST /auth/login` 与 `/auth/refresh` 的响应结构对 W10 是契约，字段见 `schemas.TokenResponse`。

## 3. 授权模型

- 单用户白名单（`OWNER_OPENIDS`），不开放注册；
- 角色三档 `owner` / `operator` / `viewer`，角色 → scope 的映射**只在 `rbac.py`**；
- 每个写端点必须挂 `require_scope()`；`backend/scripts/audit_scopes.py` 扫描全部路由，
  输出非空即 CI 失败（当前 30 个端点、15 个公开端点，零缺口）；
- 越权统一 `403` + `error.code=forbidden`，**不**区分「不存在」与「无权限」以外的细节，
  但也不把 403 伪装成 404——本项目是单用户系统，枚举风险低于排障成本。

## 4. 限流模型

分两层，职责不同，**不可互相替代**：

| 层 | 文件 | 计数对象 | 触发条件 |
|---|---|---|---|
| 业务层 | `modules/identity/throttle.py` | 登录**失败**次数 | 5 次 / 15 min（按邮箱 + IP 双键） |
| 传输层 | `core/ratelimit.py` | 请求**次数** | 见下表，按 IP + 主体双维度 |

只做传输层会漏「密码猜对了但被爆破」之外的账号锁定诉求；只做业务层会漏
「刷新令牌被拿去狂刷」「已登录会话被当成肉鸡」这类非登录流量。

| 桶 | IP 维度 | 主体维度 | 窗口 |
|---|---|---|---|
| `login` | 20 | 10（邮箱指纹） | 60 s |
| `refresh` | 60 | 30（刷新令牌指纹） | 60 s |
| `logout`/`sessions`/`revoke-all`/`me`/`scopes` | 120 | 60（`sub` 指纹） | 60 s |

- 计数落 Redis（`INCR` + 首次 `EXPIRE NX`），**绝不当队列**；
- `app_env in (staging, prod)` 用 Redis，`dev`/`test` 用进程内计数——CI 不该为一条单测起 Redis；
- Redis 键只放指纹（sha256 前 16 位）与 IP，**不放邮箱明文**；
- 超限返回 `429` + `error.code=rate_limited` + `Retry-After`/`X-RateLimit-*` 头；
- 未知主体（伪造令牌）不计数，交给认证依赖返回 401——限流不该先于认证泄露「令牌真假」。

## 5. 暴露面

- `APP_ENV=prod` 时 `/docs`、`/redoc`、`/openapi.json` 全部 404（`main.create_app` 传 `None`），
  单测 `test_prod_disables_docs_and_openapi` 守住；
- 错误响应形状固定为 `{error:{code,message,request_id,details?}}`，客户端只认 `code`；
- `X-Request-Id` 贯穿访问日志与错误体，便于按一次请求排障。

## 6. 安全响应头的边界（应用 vs Caddy）

| 头 | 由谁发 | 原因 |
|---|---|---|
| `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` / `Cache-Control: no-store`(/api) | 应用（`SecurityHeadersMiddleware`） | 防止绕过反代直连容器时裸奔 |
| `Strict-Transport-Security` | **仅 Caddy** | 应用跑在 HTTP 后面；若应用也发，本地 HTTP 开发会被 HSTS 粘死 |
| `Content-Security-Policy` | **仅 Caddy** | 页面由 Next.js 出，CSP 需要按前端实际资源收口，放应用层会对不上 |

`deploy/Caddyfile` 属 W01 **禁止触碰**（边界表），故只给建议 patch，走 shared-registry 变更请求：

```diff
 {$APP_DOMAIN:app.pcxx.dev} {
 	encode zstd gzip
 
 	header {
 		Strict-Transport-Security "max-age=31536000; includeSubDomains"
 		X-Content-Type-Options "nosniff"
 		Referrer-Policy "no-referrer"
+		Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
 		-Server
 	}
 
 	handle /api/* {
-		reverse_proxy api:8000
+		reverse_proxy api:8000 {
+			# 覆盖而不是追加：应用按「最左」取客户端 IP，追加会让伪造值排在最左
+			header_up X-Forwarded-For {remote_host}
+		}
 	}
```

`api.` 站点同样需要 `header_up X-Forwarded-For {remote_host}`，否则 IP 维度限流可被绕过。

## 7. 已知取舍

1. **XFF 左值信任**：应用取最左侧 `X-Forwarded-For`，安全前提是反代**覆盖**该头。
   Caddy 默认追加，因此第 6 节的 patch 是安全前提，不是优化项。
2. **进程内限流**：`dev` 用内存计数，多 worker 下额度会按 worker 数放大——只在本地成立。
3. **429 先于 401**：路由级守卫在认证之前，被限流时拿不到「令牌是否有效」的信号，这是刻意的。
4. **限流不区分读写的精细度**：当前按端点分组，不做单端点独立额度；过细会让运维无法解释「为什么我被限」。

## 8. 验证

- `tests/test_ratelimit.py`：IP 桶 429 + `Retry-After`、主体桶跨 IP 拦截、跨账号不串桶、
  键无邮箱明文、桶隔离、窗口滚动、后端选择、生产文档关闭、CSP/HSTS 不由应用发；
- `tests/test_identity.py`：轮换、重放、过期、白名单外主体、业务层 5 次锁；
- `scripts/audit_scopes.py`：30 端点零缺口。
