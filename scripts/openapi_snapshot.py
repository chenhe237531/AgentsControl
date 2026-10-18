#!/usr/bin/env python3
"""OpenAPI 快照：把「接口契约」变成可 diff 的文件。

为什么要快照而不是直接对比运行时：
- 前端生成的类型、其它窗口的 mock、文档里写的端点，全部依赖同一份契约；
- 契约一旦变化，评审必须在 PR 里明确看到，而不是"合并后前端突然 404"。

用法
    python scripts/openapi_snapshot.py --write     # 生成/更新 backend/openapi.snapshot.json
    python scripts/openapi_snapshot.py --check     # CI：有差异则退出 1

注意
- 只对比「路径 + 方法 + 参数名 + 响应码」这四个层面，不对比描述文字，
  否则每次改 docstring 都会触发"契约变更"，警告噪音会让人关闭这个检查。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "backend" / "openapi.snapshot.json"


def build_app():  # noqa: ANN201
    sys.path.insert(0, str(ROOT / "backend"))
    from app.main import create_app  # noqa: PLC0415

    return create_app()


def extract(spec: dict) -> dict:
    """只保留契约稳定的部分。"""
    out: dict[str, dict] = {}
    for path, methods in sorted((spec.get("paths") or {}).items()):
        for method, op in sorted(methods.items()):
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            params = sorted(
                f"{p.get('in')}:{p.get('name')}" for p in (op.get("parameters") or [])
            )
            body = op.get("requestBody")
            responses = sorted((op.get("responses") or {}).keys())
            out[f"{method.upper()} {path}"] = {
                "params": params,
                "has_body": bool(body),
                "responses": responses,
                "operation_id": op.get("operationId"),
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args()

    spec = build_app().openapi()
    current = extract(spec)

    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {SNAPSHOT.relative_to(ROOT)}（{len(current)} 个操作）")
        return 0

    if not SNAPSHOT.exists():
        print("缺少快照文件，先跑 --write 并提交", file=sys.stderr)
        return 1

    previous = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = sorted(
        k for k in set(current) & set(previous) if current[k] != previous[k]
    )

    if not (added or removed or changed):
        print("OpenAPI 契约无变化")
        return 0

    for k in added:
        print(f"  + {k}")
    for k in removed:
        print(f"  - {k}  (breaking: 删除端点会影响已上线前端)")
    for k in changed:
        print(f"  ~ {k}")
        print(f"      before: {previous[k]}")
        print(f"      after : {current[k]}")

    print(
        f"\n契约变更：新增 {len(added)}，删除 {len(removed)}，修改 {len(changed)}。\n"
        "评审要求：PR 描述里必须说明这些变更对前端/设备端的影响，"
        "删除或缩小字段即视为 breaking change。",
        file=sys.stderr,
    )
    return 1 if (removed or changed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
