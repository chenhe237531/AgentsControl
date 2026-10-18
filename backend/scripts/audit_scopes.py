#!/usr/bin/env python3
"""扫描全部路由，列出「未挂认证」和「写端点未挂 scope」的端点。

用法：
    python scripts/audit_scopes.py            # 人类可读；有问题时退出码 1（CI 断言用）
    python scripts/audit_scopes.py --json     # 机器可读（供 PR 描述/流水线解析）
    python scripts/audit_scopes.py --check-roles   # 额外校验角色表自洽

为什么退出码是 1 而不是 0：CI 里这一条的写法只能是「脚本成功即通过」，
如果脚本永远 exit 0，那它就只是个打印工具。`--warn-only` 留给本地探索用。

扫描逻辑在 `app/modules/identity/scopes_audit.py`（这样能被 pytest 覆盖），本文件只负责
命令行参数与退出码。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402
from app.modules.identity.scopes_audit import check_role_table, scan_app  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="路由认证/scope 审计")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--warn-only", action="store_true", help="有问题也返回 0（仅本地探索用）")
    parser.add_argument("--check-roles", action="store_true", help="同时校验角色表")
    args = parser.parse_args(argv)

    report = scan_app(create_app())
    role_problems = check_role_table() if args.check_roles else []
    ok = report.ok and not role_problems

    if args.json:
        payload = report.as_dict()
        if args.check_roles:
            payload["role_problems"] = role_problems
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(report.format_text())
        for problem in role_problems:
            print(f"  role_table     {problem}")

    if ok or args.warn_only:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
