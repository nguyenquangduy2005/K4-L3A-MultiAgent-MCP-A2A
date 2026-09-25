from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway

# tool -> evidence domain the envelope must declare
TOOL_DOMAINS: dict[str, str] = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
}

# Least-privilege allowlist: which actor may call which tool.
PERMISSIONS: dict[str, frozenset[str]] = {
    "order-item-agent": frozenset({"get_order", "get_order_items", "get_sellers"}),
    "payment-agent": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "policy-agent": frozenset({"get_policy"}),
}

MAX_ATTEMPTS = 3
CALL_TIMEOUT_SECONDS = 60.0
BACKOFF_SECONDS = (1.0, 2.0)


@dataclass(frozen=True)
class Fetched:
    tool: str
    agent: str
    status: str  # ok | tool_error | unavailable | invalid
    ref: str | None = None
    data: Any = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class EvidenceStore:
    """Per-case evidence. A new store is created for every case, so refs never cross cases."""

    results: dict[tuple[str, tuple[tuple[str, str], ...]], Fetched] = field(default_factory=dict)
    refs: set[str] = field(default_factory=set)


class EvidenceCollector:
    def __init__(self, gateway: EvidenceGateway, case_id: str, store: EvidenceStore) -> None:
        self._gateway = gateway
        self._case_id = case_id
        self._store = store

    async def fetch(self, agent: str, tool: str, **arguments: str) -> Fetched:
        if tool not in PERMISSIONS.get(agent, frozenset()):
            raise PermissionError(f"{agent} is not allowed to call {tool}")
        key = (tool, tuple(sorted(arguments.items())))
        cached = self._store.results.get(key)
        if cached is not None and cached.ok:
            return cached
        result = await self._call(agent, tool, arguments)
        self._store.results[key] = result
        if result.ok and result.ref:
            self._store.refs.add(result.ref)
        return result

    def forget_failures(self) -> None:
        """Let a replan re-fetch calls that failed; successful evidence stays cached."""
        self._store.results = {k: v for k, v in self._store.results.items() if v.ok}

    async def _call(self, agent: str, tool: str, arguments: dict[str, str]) -> Fetched:
        detail = ""
        for attempt in range(MAX_ATTEMPTS):
            if self._gateway.broken:  # a dead session cannot recover by retrying
                break
            try:
                evidence = await asyncio.wait_for(
                    self._gateway.call(tool, case_id=self._case_id, **arguments),
                    timeout=CALL_TIMEOUT_SECONDS,
                )
            except RuntimeError as exc:  # the tool answered with an error: deterministic, no retry
                return Fetched(tool, agent, "tool_error", detail=str(exc)[:160])
            except ValueError as exc:  # envelope failed contract validation
                return Fetched(tool, agent, "invalid", detail=str(exc)[:160])
            except Exception as exc:  # transport/timeout: idempotent read, bounded retry
                detail = f"{type(exc).__name__}: {exc}"[:160]
                if attempt + 1 < MAX_ATTEMPTS:
                    await asyncio.sleep(BACKOFF_SECONDS[attempt])
                continue
            if evidence["domain"] != TOOL_DOMAINS[tool]:
                return Fetched(
                    tool, agent, "invalid", detail=f"unexpected domain {evidence['domain']}"
                )
            return Fetched(tool, agent, "ok", ref=evidence["evidence_ref"], data=evidence["data"])
        return Fetched(tool, agent, "unavailable", detail=detail)
