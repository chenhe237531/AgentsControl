"""LiteLLM 客户端：把"AI 三问"封装成一次外部 HTTP 调用。

设计原则（对应 W02 计划约束）：
- LLM 调用走 `LITELLM_BASE_URL`，**不在本模块直连厂商**；
- 任何上游异常（HTTP 4xx/5xx、超时、解析失败）→ 抛 `UpstreamError`，
  上层 service **绝不**为这种情况编造三问答案；
- 字段名固定为 `acceptance` / `horizon` / `budget`，与 `ThreeQuestions` 一致。

输出 JSON 约束用 prompt 强约束 + 字段白名单；任何字段缺失视为该问题答不出。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx

from app.core.config import settings
from app.core.errors import UpstreamError

from .schemas import ThreeQuestions

logger = logging.getLogger("afc.intake.litellm")

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 30.0

_SYSTEM_PROMPT = (
    "你是一个体感结构化助手。给你一条用户记录的体感文本，"
    "用三个固定问题把它结构化。只输出 JSON，字段名固定为 "
    "`acceptance`、`horizon`、`budget`、`suggest_interest`。\n"
    "- acceptance: 做完之后什么变了（验收标准）。答不出则为 null，并把 suggest_interest 设 true。\n"
    "- horizon: 多久内想看到结果（TTL 表达，如 14 天、本月底）。\n"
    "- budget: 愿意花多少（赚钱目标填钱 ¥，进化目标填 小时数）。\n"
    "- suggest_interest: 布尔，true 表示第一问答不出，建议放入兴趣池。\n"
    "绝不要编造验收标准。绝不要输出 JSON 之外的内容。"
)

_USER_PROMPT_TEMPLATE = "体感文本：{text}\n\n请输出 JSON。"


class LitellmClient(Protocol):
    async def structure(self, text: str, *, model: str | None = None) -> tuple[ThreeQuestions, str, float]:
        """返回 (questions, model_used, cost_usd)。失败抛 UpstreamError。"""
        ...


# ---------------------------------------------------------------------------
# 粗略价目表（与 LiteLLM 实际计费一致更好，但 MVP 阶段足够）。
# 字段名按 OpenAI 风格小写。未命中按均价 $1/M_in $3/M_out 估算并告警。
# ---------------------------------------------------------------------------
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),     # $ per 1M tokens
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "claude-3-5-haiku-latest": (0.8, 4.0),
    "claude-3-5-sonnet-latest": (3.0, 15.0),
    "deepseek-chat": (0.14, 0.28),
    "qwen-max": (2.8, 8.4),
}


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _estimate_cost(model: str, input_text: str, output_text: str) -> float:
    in_tok = _estimate_tokens(input_text)
    out_tok = _estimate_tokens(output_text)
    p_in, p_out = _PRICING.get(model, (1.0, 3.0))
    return round((in_tok * p_in + out_tok * p_out) / 1_000_000, 6)


def _parse_response(raw: str) -> ThreeQuestions:
    """从模型输出里抠出 JSON。容错若干常见包裹：Markdown 代码块、首尾冗余。"""
    s = raw.strip()
    if s.startswith("```"):
        # 去掉 ```json ... ``` 或 ``` ... ```
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    # 截取第一个 { 到最后一个 }
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    try:
        data: dict[str, Any] = json.loads(s)
    except json.JSONDecodeError as exc:
        raise UpstreamError("LiteLLM 返回的 JSON 解析失败") from exc

    if "suggest_interest" not in data:
        data["suggest_interest"] = False

    def _as_str(v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    return ThreeQuestions(
        acceptance=_as_str(data.get("acceptance")),
        horizon=_as_str(data.get("horizon")),
        budget=_as_str(data.get("budget")),
        suggest_interest=bool(data.get("suggest_interest", False)),
    )


class HttpLitellmClient:
    """生产实现。每实例持有一个 httpx.AsyncClient，外部可注入 transport 便于测试。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or settings.litellm_base_url).rstrip("/")
        self._api_key = api_key or ""
        self._model = model
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def structure(self, text: str, *, model: str | None = None) -> tuple[ThreeQuestions, str, float]:
        used_model = model or self._model
        if not self._base_url:
            raise UpstreamError("LiteLLM_BASE_URL 未配置")
        payload = {
            "model": used_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _USER_PROMPT_TEMPLATE.format(text=text)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError("LiteLLM 网络错误") from exc
        if resp.status_code >= 500:
            raise UpstreamError(f"LiteLLM 5xx: {resp.status_code}")
        if resp.status_code >= 400:
            raise UpstreamError(f"LiteLLM {resp.status_code}: {resp.text[:200]}")

        body = resp.json()
        raw_content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        questions = _parse_response(raw_content)
        cost = _estimate_cost(used_model, text, raw_content)
        return questions, used_model, cost


__all__ = [
    "LitellmClient",
    "HttpLitellmClient",
    "DEFAULT_MODEL",
]