from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, recite_outputs, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case

MAX_RECONNECTS = 10
MAX_FALLBACK_RATIO = 0.1  # more fallbacks than this means MCP is unhealthy, not the cases


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path, force: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    # Run into a staging area so a bad run (e.g. MCP down) never overwrites good artifacts.
    staging = root / ".run-staging"
    shutil.rmtree(staging, ignore_errors=True)
    output_root = staging / "outputs"
    output_root.mkdir(parents=True)
    trace_path = staging / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    pending = list(case_set.case_ids)
    reconnects = 0
    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                if not await gateway.list_tools():
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending:
                    case_id = pending[0]
                    case = case_set.cases[case_id]
                    trace.discard_case(case_id)
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case, gateway, trace)
                    if gateway.broken:  # transport died mid-case: redo it on a fresh session
                        raise ConnectionError(f"MCP session lost during {case_id}")
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    target = output_root / f"{case_id}.json"
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    temporary.replace(target)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    pending.pop(0)
        except Exception as exc:
            if not pending:
                break  # everything finished; only the connection teardown failed
            if isinstance(exc, ValueError | RuntimeError):
                raise
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                raise RuntimeError(f"MCP connection kept failing: {exc}") from exc
            print(
                f"reconnecting MCP ({reconnects}/{MAX_RECONNECTS}): {type(exc).__name__}",
                file=sys.stderr,
            )
            await asyncio.sleep(min(2 * reconnects, 10))

    fallbacks = sum(
        '"primary_issue": "insufficient_evidence"' in path.read_text(encoding="utf-8")
        for path in output_root.glob("*.json")
    )
    limit = MAX_FALLBACK_RATIO * len(case_set.case_ids)
    if fallbacks > limit and not force:
        raise RuntimeError(
            f"{fallbacks}/{len(case_set.case_ids)} cases fell back to insufficient_evidence "
            f"(MCP likely unhealthy); existing outputs/traces were kept. "
            f"Inspect {staging} or rerun with --force to overwrite."
        )
    final_outputs = root / "outputs"
    final_outputs.mkdir(exist_ok=True)
    for stale in final_outputs.glob("*.json"):
        stale.unlink()
    for produced in output_root.glob("*.json"):
        shutil.move(str(produced), final_outputs / produced.name)
    (root / "traces").mkdir(exist_ok=True)
    shutil.move(str(trace_path), root / "traces" / "trace.jsonl")
    shutil.rmtree(staging, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument("--force", action="store_true", help="overwrite even if many cases fell back")
    commands.add_parser("validate", help="validate outputs and observable trace")
    commands.add_parser(
        "recite", help="offline: cite all trace-consumed evidence in outputs (no MCP calls)"
    )
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, args.force))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "recite":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            print(f"OK: updated {recite_outputs(root, case_set, contracts)} outputs")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
