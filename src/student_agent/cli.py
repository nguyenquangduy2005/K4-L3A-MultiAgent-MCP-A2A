from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")

    async with connect_gateway(
        settings.mcp_endpoint,
        settings.team_api_key,
        contracts,
    ) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run_one_case(
    *,
    root: Path,
    settings: Settings,
    contracts: Contracts,
    case_id: str,
    case: dict,
    trace: TraceWriter,
    output_root: Path,
    max_attempts: int = 3,
) -> bool:
    target = output_root / f"{case_id}.json"

    # Nếu output đã tồn tại thì giữ lại và bỏ qua case này.
    if target.exists():
        try:
            existing = json.loads(
                target.read_text(encoding="utf-8")
            )

            contracts.validate_output(
                existing,
                f"outputs/{case_id}.json",
            )

            if existing.get("case_id") == case_id:
                print(
                    f"[SKIP] {case_id} - output đã tồn tại",
                    flush=True,
                )
                return True

        except (OSError, ValueError, json.JSONDecodeError):
            # Output hỏng thì chạy lại case.
            pass

    for attempt in range(1, max_attempts + 1):
        print(
            f"[CASE] {case_id} - attempt {attempt}/{max_attempts}",
            flush=True,
        )

        try:
            # Mỗi case sử dụng một MCP session riêng.
            async with connect_gateway(
                settings.mcp_endpoint,
                settings.team_api_key,
                contracts,
            ) as gateway:

                discovered_tools = await gateway.list_tools()

                if not discovered_tools:
                    raise RuntimeError(
                        "MCP Gateway returned no tools"
                    )

                output = await solve_case(
                    case,
                    gateway,
                    trace,
                )

                contracts.validate_output(
                    output,
                    f"outputs/{case_id}.json",
                )

                if output.get("case_id") != case_id:
                    raise ValueError(
                        f"solver returned a mismatched "
                        f"case_id for {case_id}"
                    )

                temporary = target.with_suffix(
                    ".json.tmp"
                )

                temporary.write_text(
                    json.dumps(
                        output,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )

                temporary.replace(target)

                print(
                    f"[OK] {case_id}",
                    flush=True,
                )

                return True

        except Exception as exc:
            print(
                f"[WARN] {case_id} attempt "
                f"{attempt}/{max_attempts} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

            if attempt < max_attempts:
                wait_seconds = 2 * attempt

                print(
                    f"[RETRY] {case_id} sau "
                    f"{wait_seconds}s...",
                    flush=True,
                )

                await asyncio.sleep(
                    wait_seconds
                )

    print(
        f"[FAILED] {case_id}: "
        f"đã thử {max_attempts} lần",
        file=sys.stderr,
        flush=True,
    )

    return False


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")

    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"

    output_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    trace_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Không xóa output/trace cũ.
    # Cho phép chạy tiếp những case chưa hoàn thành.
    trace = TraceWriter(
        trace_path,
        contracts,
    )

    total = len(case_set.case_ids)
    completed = 0
    failed: list[str] = []

    for index, case_id in enumerate(
        case_set.case_ids,
        start=1,
    ):
        case = case_set.cases[case_id]

        print(
            f"\n===== CASE {index}/{total}: "
            f"{case_id} =====",
            flush=True,
        )

        success = await _run_one_case(
            root=root,
            settings=settings,
            contracts=contracts,
            case_id=case_id,
            case=case,
            trace=trace,
            output_root=output_root,
            max_attempts=3,
        )

        if success:
            completed += 1
        else:
            failed.append(case_id)

    print(
        f"\n===== RUN COMPLETE =====",
        flush=True,
    )

    print(
        f"Completed: {completed}/{total}",
        flush=True,
    )

    if failed:
        print(
            "Failed cases:",
            ", ".join(failed),
            file=sys.stderr,
            flush=True,
        )

        raise RuntimeError(
            f"{len(failed)} case(s) failed"
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Day09 L3A student workflow"
    )

    result.add_argument(
        "--root",
        default=".",
        help="repository root (default: current directory)",
    )

    commands = result.add_subparsers(
        dest="command",
        required=True,
    )

    commands.add_parser(
        "validate-inputs",
        help="validate case-set.json and all 100 inputs",
    )

    commands.add_parser(
        "mcp-tools",
        help="authenticate and list discovered MCP tools",
    )

    commands.add_parser(
        "run",
        help="run the implemented workflow for all cases",
    )

    commands.add_parser(
        "validate",
        help="validate outputs and observable trace",
    )

    package = commands.add_parser(
        "package",
        help="validate and build the submission ZIP",
    )

    package.add_argument(
        "--output",
        default="dist/submission.zip",
    )

    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)

    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)

            print(
                f"OK: {case_set.variant_id} / "
                f"{case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )

        elif args.command == "mcp-tools":
            asyncio.run(
                _show_tools(root)
            )

        elif args.command == "run":
            asyncio.run(
                _run(root)
            )

        elif args.command == "validate":
            case_set = load_case_set(root)

            contracts = Contracts(
                root / "contracts" / "schemas"
            )

            _, trace = validate_artifacts(
                root,
                case_set,
                contracts,
            )

            print(
                f"OK: {len(case_set.case_ids)} outputs / "
                f"{len(trace)} trace events"
            )

        elif args.command == "package":
            destination = package_submission(
                root,
                root / args.output,
            )

            print(
                f"OK: {destination}"
            )

    except (
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()