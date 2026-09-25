from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.evidence import EvidenceCollector, EvidenceStore
from student_agent.submission import lifecycle_problems
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "o" * 32
POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_id": None, "party_type": "platform"}],
        },
        "unsupported_claim": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    },
}
ITEM = {
    "order_item_id": "item-1",
    "seller_id": "seller-1",
    "price": "79.00",
    "freight_value": "10.00",
}
PAYMENT = {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "79.00"}


def order_data(status: str) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "order_status": status,
        "order_delivered_customer_date": None,
        "order_estimated_delivery_date": "2018-01-10T09:00:00-03:00",
    }


class FakeGateway:
    """Offline stand-in for the MCP gateway; serves scripted evidence per tool."""

    def __init__(self, status: str, failing: frozenset[str] = frozenset()) -> None:
        self.contracts = Contracts(ROOT / "contracts" / "schemas")
        self.broken = False
        self.failing = failing
        self.calls: list[str] = []
        self.case_ids: list[str] = []
        self.issued: set[str] = set()
        self.data = {
            "get_order": ("order", order_data(status)),
            "get_order_items": ("item", [ITEM]),
            "get_sellers": ("seller", [{"seller_id": "seller-1"}]),
            "get_order_payments": ("payment", [PAYMENT]),
            "get_payment_timeline": (
                "payment",
                {
                    "payments": [PAYMENT],
                    "events": [
                        {"event_type": "captured", "status": "confirmed", "amount_brl": "79.00"}
                    ],
                },
            ),
            "get_refund_timeline": ("refund", {"events": []}),
            "get_shipment_summary": (
                "shipment",
                {
                    "order_id": ORDER_ID,
                    "delivered_customer_at": None,
                    "estimated_delivery_at": "2018-01-10T09:00:00-03:00",
                    "events": [],
                },
            ),
            "get_policy": ("policy", POLICY),
        }

    async def call(self, tool: str, *, case_id: str, **_: str) -> dict[str, Any]:
        self.calls.append(tool)
        self.case_ids.append(case_id)
        if tool in self.failing:
            raise RuntimeError(f"MCP tool {tool} failed: boom")
        domain, data = self.data[tool]
        ref = f"ev_{tool}_{case_id}".ljust(24, "x")
        self.issued.add(ref)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": ref,
            "result_hash": "sha256:" + "a" * 64,
            "domain": domain,
            "data": data,
        }


def run_case(gateway: FakeGateway, tmp_path: Path) -> tuple[dict[str, Any], list[str]]:
    case = {
        "case_id": "L3A_CASE_T01",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "c1", "topic": "canceled_order_paid"},
                {"claim_id": "c2", "topic": "requested_full_refund"},
            ],
        },
    }
    trace = TraceWriter(tmp_path / "trace.jsonl", gateway.contracts)
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    events = [line for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    return output, events


def test_supported_claim_is_confirmed_and_traced(tmp_path: Path) -> None:
    gateway = FakeGateway("canceled")
    output, events = run_case(gateway, tmp_path)
    gateway.contracts.validate_output(output, "output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    text = "\n".join(events)
    for event_type in ("task_assigned", "handoff", "policy_decided", "verification_completed"):
        assert f'"event_type":"{event_type}"' in text
    cited = set(output["evidence_refs"])
    assert cited and all(ref in text for ref in cited)


def test_unsupported_claim_when_evidence_contradicts(tmp_path: Path) -> None:
    output, _ = run_case(FakeGateway("delivered"), tmp_path)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0


def test_missing_required_evidence_falls_back_without_inventing_refs(tmp_path: Path) -> None:
    gateway = FakeGateway("canceled", failing=frozenset({"get_order"}))
    output, events = run_case(gateway, tmp_path)
    gateway.contracts.validate_output(output, "output")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert '"decision_code":"fallback"' in "\n".join(events)


def test_agent_cannot_call_a_tool_outside_its_allowlist() -> None:
    gateway = FakeGateway("canceled")
    collector = EvidenceCollector(gateway, "L3A_CASE_T01", EvidenceStore())  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        asyncio.run(collector.fetch("shipment-agent", "get_order_payments", order_id=ORDER_ID))
    assert gateway.calls == []


def consumed_refs(events: list[str]) -> list[str]:
    found: list[str] = []
    for line in events:
        event = json.loads(line)
        if event["event_type"] == "tool_result_consumed":
            found.extend(event["evidence_refs"])
    return found


def test_every_call_carries_the_case_id(tmp_path: Path) -> None:
    gateway = FakeGateway("canceled")
    run_case(gateway, tmp_path)
    assert gateway.case_ids and set(gateway.case_ids) == {"L3A_CASE_T01"}


def test_output_and_trace_only_use_refs_issued_by_the_gateway(tmp_path: Path) -> None:
    gateway = FakeGateway("canceled")
    output, events = run_case(gateway, tmp_path)
    cited = set(output["evidence_refs"])
    for claim in output["claim_assessments"]:
        cited.update(claim["evidence_refs"])
    assert cited <= gateway.issued
    assert set(consumed_refs(events)) <= gateway.issued
    assert cited <= set(consumed_refs(events))


def test_replan_does_not_report_cached_evidence_twice(tmp_path: Path) -> None:
    gateway = FakeGateway("canceled", failing=frozenset({"get_order"}))
    output, events = run_case(gateway, tmp_path)
    assert '"decision_code":"replan"' in "\n".join(events)
    refs = consumed_refs(events)
    assert refs and len(refs) == len(set(refs))
    # the fallback cites only order/payment evidence, never unrelated refs
    assert set(output["evidence_refs"]) <= {r for r in gateway.issued if "payment" in r}


def event_types(events: list[str]) -> list[str]:
    return [json.loads(line)["event_type"] for line in events]


def test_lifecycle_is_complete_and_ordered_even_when_a_tool_errors(tmp_path: Path) -> None:
    # get_refund_timeline erroring is normal for orders without refunds; it must not add a
    # stray handoff ahead of the first tool_result_consumed.
    _, events = run_case(
        FakeGateway("canceled", failing=frozenset({"get_refund_timeline"})), tmp_path
    )
    types = ["case_received", *event_types(events), "case_finalized"]
    assert lifecycle_problems({"L3A_CASE_T01": types}) == ([], [])


def test_lifecycle_check_flags_missing_and_misordered_events() -> None:
    full = [
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
        "case_finalized",
    ]
    errors, _ = lifecycle_problems({"C": [t for t in full if t != "policy_decided"]})
    assert errors and "policy_decided" in errors[0]
    swapped = [full[0], full[1], full[3], full[2], *full[4:]]
    errors, warnings = lifecycle_problems({"C": swapped})
    assert not errors and warnings


def test_verifier_rejects_party_that_contradicts_the_issue(tmp_path: Path) -> None:
    from student_agent.workflow import check_against_policy

    gateway = FakeGateway("canceled")
    output, _ = run_case(gateway, tmp_path)
    order = {"seller_ids": ["seller-1"]}
    assert check_against_policy(POLICY, output, order) == []
    output["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "logistics_provider", "party_id": None}
    ]
    assert "party_vs_issue" in check_against_policy(POLICY, output, order)


def test_confidence_drops_only_for_an_unbacked_refund_amount(tmp_path: Path) -> None:
    clean, _ = run_case(FakeGateway("canceled"), tmp_path)
    assert clean["assessment"]["confidence"] == 0.95
    gateway = FakeGateway("canceled")
    gateway.data["get_payment_timeline"] = (
        "payment",
        {
            "payments": [{**PAYMENT, "payment_value": "90.00"}],
            "events": [{"event_type": "captured", "status": "confirmed", "amount_brl": "90.00"}],
        },
    )
    gateway.data["get_order_items"] = (
        "item",
        [{**ITEM, "price": "90.00", "freight_value": "0.00"}],
    )
    doubtful, _ = run_case(gateway, tmp_path / "b")
    assert doubtful["assessment"]["confidence"] == 0.9
    assert doubtful["data_conflicts"][0]["resolution_code"] == "POLICY_AMOUNT_NOT_IN_EVIDENCE"


def test_output_cites_every_evidence_the_specialists_consumed(tmp_path: Path) -> None:
    output, events = run_case(FakeGateway("canceled"), tmp_path)
    assert set(output["evidence_refs"]) == set(consumed_refs(events))
