/**
 * 类型化 API 客户端（骨架）。
 *
 * 硬约定（与 docs/DEV-PLAN-v1.md §7 一致）：
 * 1. Base URL 只来自 `NEXT_PUBLIC_API_BASE_URL`，禁止硬编码；
 * 2. 访问令牌**不放 localStorage / 不放 URL**：短期令牌驻内存，长期刷新令牌走 httpOnly Cookie；
 * 3. 4xx 不重试（可重试的只有 408/429），5xx 交给上层 Query 重试（≤3 次）；
 * 4. 后端错误统一映射为 `ApiError`，UI 只读 `code` 做文案映射，不解析 message。
 */

export interface ApiErrorBody {
  error?: { code?: string; message?: string; request_id?: string; details?: unknown };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly details?: unknown;

  constructor(status: number, code: string, message: string, requestId?: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }

  get retryable(): boolean {
    return this.status >= 500 || this.status === 408 || this.status === 429;
  }
}

export function apiBaseUrl(): string {
  const base = process.env.NEXT_PUBLIC_API_BASE_URL;
  if (!base) {
    // 开发期兜底；生产必须由构建环境注入，避免"上线打到 localhost"
    return "http://localhost:8000/api/v1";
  }
  return base.replace(/\/$/, "");
}

// 访问令牌只驻内存：刷新页面即重新换取，减少 XSS 的持久化面
let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

type FetchOptions = Omit<RequestInit, "body"> & { body?: unknown; json?: boolean };

let refreshInFlight: Promise<boolean> | null = null;

async function refreshOnce(): Promise<boolean> {
  refreshInFlight ??= (async () => {
    try {
      const res = await fetch(`${apiBaseUrl()}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) return false;
      const body = (await res.json()) as { access_token?: string };
      if (body.access_token) {
        setAccessToken(body.access_token);
        return true;
      }
      return false;
    } catch {
      return false;
    } finally {
      setTimeout(() => {
        refreshInFlight = null;
      }, 0);
    }
  })();
  return refreshInFlight;
}

export async function apiFetch<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const { body, json = true, headers, ...rest } = options;

  const send = async (): Promise<Response> =>
    fetch(`${apiBaseUrl()}${path}`, {
      ...rest,
      credentials: "include",
      headers: {
        Accept: "application/json",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

  let res = await send();

  // 401 只重试一次：先尝试静默刷新，再重放原请求
  if (res.status === 401 && !path.startsWith("/auth/")) {
    if (await refreshOnce()) {
      res = await send();
    }
  }

  if (!res.ok) {
    let parsed: ApiErrorBody = {};
    try {
      parsed = (await res.json()) as ApiErrorBody;
    } catch {
      // 上游返回非 JSON（如 Caddy 502 页面）
    }
    throw new ApiError(
      res.status,
      parsed.error?.code ?? `http_${res.status}`,
      parsed.error?.message ?? `请求失败（HTTP ${res.status}）`,
      parsed.error?.request_id,
      parsed.error?.details,
    );
  }

  if (res.status === 204) return undefined as T;
  return (json ? await res.json() : (await res.text())) as T;
}

export const api = {
  get: <T>(path: string) => apiFetch<T>(path),
  post: <T>(path: string, body?: unknown) => apiFetch<T>(path, { method: "POST", body }),
  patch: <T>(path: string, body?: unknown) => apiFetch<T>(path, { method: "PATCH", body }),
  delete: <T>(path: string) => apiFetch<T>(path, { method: "DELETE" }),
};
