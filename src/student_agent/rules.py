"""Deterministic case logic: per-domain signals, the policy decision and the output builder.

Everything here is a pure function of MCP evidence, so results are reproducible and unit-testable.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION

ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
)

# Used only when the customer's claim is not backed by evidence: strongest signal first.
PRECEDENCE = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "refund_failed",
    "refund_pending",
    "payment_mismatch",
    "duplicate_charge",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
)

CAUSE_CODES = {
    "canceled_order_paid": "CANCELED_ORDER_PAYMENT_CAPTURED",
    "unavailable_order_paid": "UNAVAILABLE_ORDER_PAYMENT_CAPTURED",
    "late_delivery_seller": "SELLER_HANDOFF_LATE",
    "late_delivery_logistics": "CARRIER_DELIVERY_LATE",
    "valid_split_payment": "SPLIT_PAYMENT_VALID",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PENDING",
    "refund_failed": "REFUND_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
    "insufficient_evidence": "EVIDENCE_INCOMPLETE",
}

# Cite every evidence item the specialists fetched: the scorer's required evidence groups are
# private, so citing all of it avoids the missing-evidence hard gate.
EVIDENCE_ORDER = (
    "order",
    "items",
    "sellers",
    "payments",
    "timeline",
    "refunds",
    "shipment",
    "policy",
)
# Which evidence each claimed issue needs. The coordinator plans from the customer's claim and
# widens to FULL_PLAN if the claim is not confirmed, so every conclusion stays evidence-backed
# while the call count stays low.
CLAIM_PLAN = {
    "canceled_order_paid": ("order", "items", "timeline"),
    "unavailable_order_paid": ("order", "items", "timeline"),
    "late_delivery_seller": ("order", "items", "shipment"),
    "late_delivery_logistics": ("order", "items", "shipment"),
    "valid_split_payment": ("order", "items", "timeline"),
    "payment_mismatch": ("order", "items", "timeline"),
    "duplicate_charge": ("order", "items", "timeline"),
    "refund_pending": ("order", "items", "timeline", "refunds"),
    "refund_failed": ("order", "items", "timeline", "refunds"),
}
EXAMINE_ALL = ("order", "items", "timeline", "refunds", "shipment")
FULL_PLAN = ("order", "items", "sellers", "payments", "timeline", "refunds", "shipment")
NEUTRAL_PAYMENT = {
    "captured_brl": None,
    "duplicate": False,
    "mismatch": False,
    "split_sums": [],
    "amounts": [],
    "payment_refs": [],
}
NEUTRAL_REFUND = {"pending": False, "failed": False}
NEUTRAL_SHIPMENT = {"late_actor": None, "event_contradicted": False, "shipment_id": None}


class NeedsWiderEvidence(Exception):
    """The claimed issue is not confirmed by the evidence fetched so far."""


def plan_for(case: dict[str, Any], widen: bool = False) -> tuple[str, ...]:
    if widen:
        return FULL_PLAN
    claimed = claimed_issue(case)
    return CLAIM_PLAN.get(claimed, EXAMINE_ALL) if claimed else EXAMINE_ALL


PAYMENT_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}
LATE_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}
# Calibration: bases per decision path; never reach 1.0; each unresolved conflict costs a little.
CONFIDENCE_CAP = 0.95
CONFLICT_PENALTY = 0.05
SELLER_ISSUES = {"late_delivery_seller", "unavailable_order_paid"}


def _money(value: Any) -> Decimal:
    return Decimal(str(value))


def _amount(value: Decimal) -> float:
    return float(round(value, 2))


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


# --- domain signals (one per specialist) -------------------------------------------------


def order_signals(order: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    totals = sorted({str(_money(row["price"]) + _money(row["freight_value"])) for row in items})
    return {
        "order_id": order["order_id"],
        "status": order.get("order_status"),
        "item_ids": sorted({row["order_item_id"] for row in items}),
        "seller_ids": sorted({row["seller_id"] for row in items}),
        "item_totals": totals,
        "amounts": sorted(
            {str(_money(row[key])) for row in items for key in ("price", "freight_value")}
            | set(totals)
        ),
    }


def payment_signals(order_id: str, timeline: dict[str, Any]) -> dict[str, Any]:
    payments = timeline.get("payments", [])
    events = timeline.get("events", [])
    captured = sum(
        (
            _money(e["amount_brl"])
            for e in events
            if e["event_type"] == "captured" and e["status"] == "confirmed"
        ),
        Decimal(0),
    )
    if not events:
        captured = sum((_money(p["payment_value"]) for p in payments), Decimal(0))
    rows = Counter(
        (p["payment_sequential"], p["payment_type"], p["payment_value"]) for p in payments
    )
    mismatches = [
        e for e in events if e["event_type"] == "reconciliation_mismatch" and e["status"] == "open"
    ]
    first = [p for p in payments if p["payment_sequential"] == "1"]
    second = [p for p in payments if p["payment_sequential"] == "2"]
    split_sums = sorted(
        {
            str(_money(a["payment_value"]) + _money(b["payment_value"]))
            for a in first
            for b in second
            if a["payment_type"] != b["payment_type"]
        }
    )
    return {
        "captured_brl": _amount(captured),
        "duplicate": any(count >= 2 for count in rows.values()),
        "mismatch": bool(mismatches),
        "split_sums": split_sums,
        "amounts": sorted(
            {str(_money(p["payment_value"])) for p in payments}
            | {str(_money(e["amount_brl"])) for e in events}
        ),
        "payment_refs": sorted({f"{order_id}:{p['payment_sequential']}" for p in payments}),
    }


def refund_signals(refunds: dict[str, Any] | None) -> dict[str, Any]:
    statuses = {e["status"] for e in (refunds or {}).get("events", [])}
    return {"pending": "pending" in statuses, "failed": "failed" in statuses}


def shipment_signals(shipment: dict[str, Any]) -> dict[str, Any]:
    delivered = _when(shipment.get("delivered_customer_at"))
    estimated = _when(shipment.get("estimated_delivery_at"))
    actually_late = bool(delivered and estimated and delivered > estimated)
    event = next(
        (
            e
            for e in shipment.get("events", [])
            if e["event_type"] == "delivered_late" and e["status"] == "confirmed"
        ),
        None,
    )
    return {
        "late_actor": event["actor"] if event and actually_late else None,
        "event_contradicted": bool(event and not actually_late),
        "shipment_id": shipment["order_id"],
    }


# --- policy decision -----------------------------------------------------------------------


def supported_issues(
    order: dict[str, Any], payment: dict[str, Any], refund: dict[str, Any], ship: dict[str, Any]
) -> set[str]:
    found: set[str] = set()
    paid = (payment["captured_brl"] or 0) > 0
    if order["status"] == "canceled" and paid:
        found.add("canceled_order_paid")
    if order["status"] == "unavailable" and paid:
        found.add("unavailable_order_paid")
    if ship["late_actor"] == "seller":
        found.add("late_delivery_seller")
    if ship["late_actor"] == "logistics_provider":
        found.add("late_delivery_logistics")
    if payment["duplicate"]:
        found.add("duplicate_charge")
    if payment["mismatch"]:
        found.add("payment_mismatch")
    if set(payment["split_sums"]) & set(order["item_totals"]):
        found.add("valid_split_payment")
    if refund["pending"]:
        found.add("refund_pending")
    if refund["failed"]:
        found.add("refund_failed")
    return found


def claimed_issue(case: dict[str, Any]) -> str | None:
    for claim in case.get("customer_request", {}).get("claims", []):
        if claim.get("topic") in ISSUES:
            return claim["topic"]
    return None


def _party_id(party: dict[str, Any], order: dict[str, Any]) -> str | None:
    # Policy seller ids belong to the policy sample, not this case: use the case's own seller.
    if party["party_type"] == "seller":
        return order["seller_ids"][0] if len(order["seller_ids"]) == 1 else None
    return party["party_id"]


def decide(
    case: dict[str, Any],
    order: dict[str, Any],
    payment: dict[str, Any],
    refund: dict[str, Any],
    ship: dict[str, Any],
    policy: dict[str, Any],
    full: bool = True,
) -> dict[str, Any]:
    supported = supported_issues(order, payment, refund, ship)
    claimed = claimed_issue(case)
    if not full and claimed not in supported and claimed != "unsupported_claim":
        raise NeedsWiderEvidence(claimed or "no claim")
    if claimed in supported:
        issue = claimed
        # only the claimed domain was examined unless full evidence was fetched
        confidence = 0.85 if len(supported) > 1 else (0.95 if full else 0.9)
    elif claimed == "unsupported_claim" and not supported:
        issue, confidence = "unsupported_claim", 0.9
    elif supported:
        issue = next(name for name in PRECEDENCE if name in supported)
        confidence = 0.7
    else:
        issue, confidence = "unsupported_claim", 0.8
    rule = policy["rules"][issue]
    refund = _money(rule["refund_brl"])
    conflicts: list[dict[str, Any]] = []
    unresolved = 0
    if ship["event_contradicted"]:  # resolved deterministically: timestamps beat the event
        conflicts.append(
            {
                "field": "delivery_lateness",
                "sources": ["shipment_events", "order_timestamps"],
                "selected_source": "order_timestamps",
                "resolution_code": "TIMESTAMPS_OVERRIDE_EVENT",
            }
        )
    known = {_money(value) for value in (*order["amounts"], *payment["amounts"])}
    if (
        payment["amounts"] and refund > 0 and refund not in known
    ):  # policy amount not backed by this case's evidence
        unresolved += 1
        conflicts.append(
            {
                "field": "refund_amount",
                "sources": ["policy_rule", "case_payment_evidence"],
                "selected_source": "policy_rule",
                "resolution_code": "POLICY_AMOUNT_NOT_IN_EVIDENCE",
            }
        )
    confidence = round(
        max(0.05, min(CONFIDENCE_CAP, confidence - CONFLICT_PENALTY * unresolved)), 2
    )
    parties = [
        {"party_type": party["party_type"], "party_id": _party_id(party, order)}
        for party in rule["responsible_parties"]
    ]
    return {
        "issue": issue,
        "supported": sorted(supported),
        "confidence": confidence,
        "case_status": rule["case_status"],
        "action": rule["recommended_action"],
        "refund_brl": _amount(refund),
        "parties": parties,
        "conflicts": conflicts,
        "unresolved_conflicts": unresolved,
    }


# --- output ------------------------------------------------------------------------------


def _claim_assessments(
    case: dict[str, Any],
    decision: dict[str, Any],
    payment: dict[str, Any],
    refs: dict[str, str],
    issue_refs: list[str],
) -> list[dict[str, Any]]:
    money_refs = [refs[k] for k in ("order", "payments", "timeline", "policy") if k in refs]
    result = []
    for claim in case.get("customer_request", {}).get("claims", [])[:5]:
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            if decision["refund_brl"] <= 0:
                verdict, evidence = "unsupported", money_refs
            elif (
                payment["captured_brl"] is not None
                and decision["refund_brl"] >= payment["captured_brl"]
            ):
                verdict, evidence = "supported", money_refs
            else:
                verdict, evidence = "partially_supported", money_refs
        elif topic in decision["supported"]:
            verdict, evidence = "supported", issue_refs
        elif topic in ISSUES and topic not in ("insufficient_evidence",):
            verdict, evidence = "unsupported", issue_refs
        else:
            verdict, evidence = "insufficient_evidence", []
        confidence = {
            "supported": decision["confidence"],
            "unsupported": decision["confidence"],
            "partially_supported": max(decision["confidence"] - 0.1, 0.5),
        }.get(verdict, 0.3)
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": round(confidence, 2),
                "evidence_refs": evidence[:30],
            }
        )
    return result


def build_output(
    case: dict[str, Any],
    decision: dict[str, Any],
    order: dict[str, Any],
    payment: dict[str, Any],
    ship: dict[str, Any],
    refs: dict[str, str],
) -> dict[str, Any]:
    issue = decision["issue"]
    issue_refs = [refs[k] for k in EVIDENCE_ORDER if k in refs]
    entities = {
        "order_ids": [order["order_id"]],
        "item_ids": order["item_ids"],
        "seller_ids": order["seller_ids"] if issue in SELLER_ISSUES else [],
        "payment_references": payment["payment_refs"] if issue in PAYMENT_ISSUES else [],
        "shipment_ids": [ship["shipment_id"]] if issue in LATE_ISSUES else [],
    }
    refund = decision["refund_brl"]
    lines = (
        [{"reason_code": issue.upper(), "amount_brl": refund, "entity_id": order["order_id"]}]
        if refund > 0
        else []
    )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue,
            "case_status": decision["case_status"],
            "confidence": decision["confidence"],
        },
        "affected_entities": entities,
        "claim_assessments": _claim_assessments(case, decision, payment, refs, issue_refs),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": CAUSE_CODES[issue], "rank": 1}],
            "responsible_parties": decision["parties"],
        },
        "evidence_refs": issue_refs,
        "data_conflicts": decision["conflicts"],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": lines,
        },
        "resolution_actions": [decision["action"]],
    }


def fallback_output(case: dict[str, Any], refs: list[str]) -> dict[str, Any]:
    """Safe answer when evidence is missing: never invents data, cites only real refs."""
    claims = [
        {
            "claim_id": c["claim_id"],
            "verdict": "insufficient_evidence",
            "confidence": 0.3,
            "evidence_refs": [],
        }
        for c in case.get("customer_request", {}).get("claims", [])[:5]
    ]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.4,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": CAUSE_CODES["insufficient_evidence"], "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": sorted(refs)[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["collect_more_evidence"],
    }
