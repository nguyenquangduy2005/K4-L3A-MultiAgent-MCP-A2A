from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.student_agent.contracts import Contracts
from src.student_agent.mcp_gateway import connect_gateway


def load_env() -> dict[str, str]:
    env = {}

    env_path = Path(".env")

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()

    return env


async def main() -> None:
    # 1. Đọc case
    case_path = Path("inputs/L3A_CASE_001.json")
    case = json.loads(case_path.read_text(encoding="utf-8"))

    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]

    # 2. Đọc .env
    env = load_env()

    team_api_key = env["COMPETITION_TEAM_API_KEY"]
    endpoint = env["MCP_ENDPOINT"]

    print("CASE:", case_id)
    print("ORDER:", order_id)
    print("ENDPOINT:", endpoint)
    print()

    # 3. Load contracts
    contracts = Contracts(Path("contracts"))

    # 4. Kết nối MCP
    async with connect_gateway(
        endpoint,
        team_api_key,
        contracts,
    ) as gateway:

        # 5. Liệt kê tools
        print("TOOLS:")
        print(await gateway.list_tools())
        print()

        # 6. Lấy definition của get_order
        response = await gateway._session.list_tools()

        for tool in response.tools:
            if tool.name == "get_order":
                print("GET_ORDER TOOL DEFINITION:")
                print("NAME:", tool.name)
                print("DESCRIPTION:", tool.description)
                print("INPUT SCHEMA:")
                print(
                    json.dumps(
                        tool.input_schema,
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                print()
                break

        # 7. Test gọi get_order
        print("Đang gọi get_order...")

        evidence = await gateway.call(
            "get_order",
            case_id=case_id,
            order_id=order_id,
        )

        print()
        print("GET_ORDER SUCCESS")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())