"""Khảo sát MCP: in schema các tool và (tuỳ chọn) gọi tool cho một case.

Dùng:
    python scripts/explore_case.py --tools
    python scripts/explore_case.py L3A_CASE_001 get_order get_order_items
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_id", nargs="?")
    parser.add_argument("tools", nargs="*")
    parser.add_argument("--tools", dest="show_tools", action="store_true")
    parser.add_argument("--arg", action="append", default=[], help="key=value thêm vào call")
    args = parser.parse_args()

    root = Path.cwd()
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:
        if args.show_tools:
            response = await gw._session.list_tools()
            for tool in response.tools:
                print(
                    json.dumps(
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "input_schema": tool.input_schema,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        if args.case_id:
            case = json.loads((root / "inputs" / f"{args.case_id}.json").read_text("utf-8"))
            extra = dict(item.split("=", 1) for item in args.arg)
            if not extra:
                extra = {"order_id": case["customer_request"]["claimed_order_id"]}
            for tool in args.tools:
                try:
                    evidence = await gw.call(tool, case_id=args.case_id, **extra)
                except Exception as exc:  # noqa: BLE001
                    evidence = {"error": f"{type(exc).__name__}: {exc}"}
                print(f"### {tool}")
                print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
