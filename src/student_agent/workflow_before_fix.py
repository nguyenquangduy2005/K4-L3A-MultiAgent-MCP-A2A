from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _unique_refs(refs: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for ref in refs:
        if ref and ref not in seen:
            seen.add(ref)
            result.append(ref)

    return result


def _find_refs(evidence: dict[str, Any]) -> list[str]:
    """
    Evidence MCP chuẩn có evidence_ref ở cấp root.
    Đồng thời giữ khả năng đọc một số cấu trúc evidence khác.
    """
    refs: list[str] = []

    # MCP chuẩn:
    # {
    #   "evidence_ref": "...",
    #   "data": {...}
    # }
    direct_ref = evidence.get("evidence_ref")

    if isinstance(direct_ref, str):
        refs.append(direct_ref)

    # Fallback cho các cấu trúc khác
    for key in (
        "evidence_refs",
        "evidenceRefs",
        "refs",
        "evidence",
    ):
        value = evidence.get(key)

        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    refs.append(item)

                elif isinstance(item, dict):
                    ref = (
                        item.get("evidence_ref")
                        or item.get("evidenceRef")
                        or item.get("ref")
                    )

                    if isinstance(ref, str):
                        refs.append(ref)

    return _unique_refs(refs)


def _find_value_recursive(
    data: Any,
    target_keys: set[str],
) -> str | None:
    if isinstance(data, dict):
        for key, value in data.items():
            normalized_key = str(key).lower()

            if normalized_key in target_keys:
                if isinstance(value, str) and value:
                    return value

            found = _find_value_recursive(
                value,
                target_keys,
            )

            if found:
                return found

    elif isinstance(data, list):
        for item in data:
            found = _find_value_recursive(
                item,
                target_keys,
            )

            if found:
                return found

    return None


def _claimed_order_id(
    case: dict[str, Any],
) -> str | None:
    return _find_value_recursive(
        case,
        {
            "order_id",
            "orderid",
            "claimed_order_id",
            "claimedorderid",
        },
    )


def _clamp_confidence(value: float) -> float:
    return max(
        0.0,
        min(1.0, float(value)),
    )


async def _call_and_consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    arguments: dict[str, str],
) -> dict[str, Any]:

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor=actor,
        target=tool_name,
        tool_name=tool_name,
    )

    evidence = await gateway.call(
        tool_name,
        case_id=case_id,
        **arguments,
    )

    evidence_refs = _find_refs(evidence)

    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=evidence_refs,
    )

    return evidence


async def _order_specialist(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> list[dict[str, Any]]:

    case_id = str(case["case_id"])

    order_id = _claimed_order_id(case)

    if not order_id:
        raise ValueError(
            f"{case_id}: không tìm thấy order_id trong input case"
        )

    order = await _call_and_consume(
        gateway,
        trace,
        case_id=case_id,
        actor="order_agent",
        tool_name="get_order",
        arguments={
            "order_id": order_id,
        },
    )

    items = await _call_and_consume(
        gateway,
        trace,
        case_id=case_id,
        actor="order_agent",
        tool_name="get_order_items",
        arguments={
            "order_id": order_id,
        },
    )

    return [
        order,
        items,
    ]


async def _payment_specialist(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> list[dict[str, Any]]:

    case_id = str(case["case_id"])

    order_id = _claimed_order_id(case)

    if not order_id:
        raise ValueError(
            f"{case_id}: không tìm thấy order_id trong input case"
        )

    payments = await _call_and_consume(
        gateway,
        trace,
        case_id=case_id,
        actor="payment_agent",
        tool_name="get_order_payments",
        arguments={
            "order_id": order_id,
        },
    )

    timeline = await _call_and_consume(
        gateway,
        trace,
        case_id=case_id,
        actor="payment_agent",
        tool_name="get_payment_timeline",
        arguments={
            "order_id": order_id,
        },
    )

    return [
        payments,
        timeline,
    ]


async def _shipment_specialist(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> list[dict[str, Any]]:

    case_id = str(case["case_id"])

    order_id = _claimed_order_id(case)

    if not order_id:
        raise ValueError(
            f"{case_id}: không tìm thấy order_id trong input case"
        )

    shipment = await _call_and_consume(
        gateway,
        trace,
        case_id=case_id,
        actor="shipment_agent",
        tool_name="get_shipment_summary",
        arguments={
            "order_id": order_id,
        },
    )

    return [
        shipment,
    ]


def _collect_all_evidence_refs(
    evidence_groups: list[dict[str, Any]],
) -> list[str]:

    refs: list[str] = []

    for evidence in evidence_groups:
        refs.extend(
            _find_refs(evidence)
        )

    return _unique_refs(refs)


def _extract_order_ids(
    evidence_groups: list[dict[str, Any]],
    case: dict[str, Any],
) -> list[str]:

    result: list[str] = []

    claimed = _claimed_order_id(case)

    if claimed:
        result.append(claimed)

    for evidence in evidence_groups:

        data = evidence.get("data")

        if not isinstance(data, dict):
            continue

        value = (
            data.get("order_id")
            or data.get("orderId")
        )

        if isinstance(value, str):
            result.append(value)

    return list(dict.fromkeys(result))


def _extract_item_ids(
    evidence_groups: list[dict[str, Any]],
) -> list[str]:

    result: list[str] = []

    for evidence in evidence_groups:

        data = evidence.get("data")

        if not isinstance(data, dict):
            continue

        items = data.get("items")

        if not isinstance(items, list):
            continue

        for item in items:

            if not isinstance(item, dict):
                continue

            value = (
                item.get("item_id")
                or item.get("itemId")
                or item.get("id")
            )

            if isinstance(value, str):
                result.append(value)

    return list(dict.fromkeys(result))


def _extract_claims(
    case: dict[str, Any],
) -> list[dict[str, Any]]:

    customer_request = case.get(
        "customer_request"
    )

    if not isinstance(customer_request, dict):
        return []

    claims = customer_request.get("claims")

    if not isinstance(claims, list):
        return []

    return [
        claim
        for claim in claims
        if isinstance(claim, dict)
    ]


def _claim_topics(
    case: dict[str, Any],
) -> set[str]:

    topics: set[str] = set()

    for claim in _extract_claims(case):

        topic = claim.get("topic")

        if isinstance(topic, str):
            topics.add(topic.lower())

    return topics


def _policy_decision(
    case: dict[str, Any],
    evidence_refs: list[str],
    evidence_groups: list[dict[str, Any]],
) -> dict[str, Any]:

    topics = _claim_topics(case)

    # Lấy trạng thái đơn hàng từ evidence MCP
    order_statuses: list[str] = []

    for evidence in evidence_groups:

        data = evidence.get("data")

        if not isinstance(data, dict):
            continue

        status = data.get("order_status")

        if isinstance(status, str):
            order_statuses.append(
                status.lower()
            )

    primary_issue = "insufficient_evidence"
    responsible_party = "unknown"
    case_status = "needs_investigation"
    confidence = 0.45
    actions: list[str] = []

    # ---------------------------------------------------------
    # 1. Canceled order + paid
    # ---------------------------------------------------------
    if (
        "canceled_order_paid" in topics
        and "canceled" in order_statuses
    ):
        primary_issue = "canceled_order_paid"
        responsible_party = "seller"
        case_status = "action_required"
        confidence = 0.80
        actions.append(
            "review_canceled_paid_order"
        )

    # ---------------------------------------------------------
    # 2. Unavailable order + paid
    # ---------------------------------------------------------
    elif "unavailable_order_paid" in topics:
        primary_issue = "unavailable_order_paid"
        responsible_party = "seller"
        case_status = "action_required"
        confidence = 0.78
        actions.append(
            "review_unavailable_paid_order"
        )

    # ---------------------------------------------------------
    # 3. Late delivery - seller
    # ---------------------------------------------------------
    elif "late_delivery_seller" in topics:
        primary_issue = "late_delivery_seller"
        responsible_party = "seller"
        case_status = "action_required"
        confidence = 0.75
        actions.append(
            "review_seller_delivery_delay"
        )

    # ---------------------------------------------------------
    # 4. Late delivery - logistics
    # ---------------------------------------------------------
    elif "late_delivery_logistics" in topics:
        primary_issue = "late_delivery_logistics"
        responsible_party = "logistics_provider"
        case_status = "action_required"
        confidence = 0.75
        actions.append(
            "review_logistics_delivery_delay"
        )

    # ---------------------------------------------------------
    # 5. Valid split payment
    # ---------------------------------------------------------
    elif "valid_split_payment" in topics:
        primary_issue = "valid_split_payment"
        responsible_party = "platform"
        case_status = "no_action"
        confidence = 0.75

    # ---------------------------------------------------------
    # 6. Payment mismatch
    # ---------------------------------------------------------
    elif "payment_mismatch" in topics:
        primary_issue = "payment_mismatch"
        responsible_party = "payment_provider"
        case_status = "needs_investigation"
        confidence = 0.65
        actions.append(
            "investigate_payment_mismatch"
        )

    # ---------------------------------------------------------
    # 7. Duplicate charge
    # ---------------------------------------------------------
    elif "duplicate_charge" in topics:
        primary_issue = "duplicate_charge"
        responsible_party = "payment_provider"
        case_status = "action_required"
        confidence = 0.82
        actions.append(
            "review_duplicate_charge"
        )

    # ---------------------------------------------------------
    # 8. Refund pending
    # ---------------------------------------------------------
    elif "refund_pending" in topics:
        primary_issue = "refund_pending"
        responsible_party = "platform"
        case_status = "action_required"
        confidence = 0.78
        actions.append(
            "review_refund_status"
        )

    # ---------------------------------------------------------
    # 9. Refund failed
    # ---------------------------------------------------------
    elif "refund_failed" in topics:
        primary_issue = "refund_failed"
        responsible_party = "payment_provider"
        case_status = "action_required"
        confidence = 0.78
        actions.append(
            "review_failed_refund"
        )

    # ---------------------------------------------------------
    # 10. Unsupported claim
    # ---------------------------------------------------------
    elif "unsupported_claim" in topics:
        primary_issue = "unsupported_claim"
        responsible_party = "unknown"
        case_status = "needs_investigation"
        confidence = 0.55
        actions.append(
            "collect_additional_evidence"
        )

    # ---------------------------------------------------------
    # 11. Insufficient evidence
    # ---------------------------------------------------------
    elif "insufficient_evidence" in topics:
        primary_issue = "insufficient_evidence"
        responsible_party = "unknown"
        case_status = "needs_investigation"
        confidence = 0.30
        actions.append(
            "collect_additional_evidence"
        )

    # Không có evidence → không được khẳng định issue
    if not evidence_refs:
        primary_issue = "insufficient_evidence"
        responsible_party = "unknown"
        case_status = "needs_investigation"
        confidence = 0.30

        if "collect_additional_evidence" not in actions:
            actions.append(
                "collect_additional_evidence"
            )

    return {
        "primary_issue": primary_issue,
        "responsible_party": responsible_party,
        "case_status": case_status,
        "confidence": _clamp_confidence(
            confidence
        ),
        "actions": actions,
    }


def _verify(
    policy: dict[str, Any],
    evidence_refs: list[str],
) -> dict[str, Any]:

    confidence = float(
        policy["confidence"]
    )

    if not evidence_refs:
        confidence = min(
            confidence,
            0.30,
        )

    if policy["primary_issue"] == "insufficient_evidence":
        confidence = min(
            confidence,
            0.60,
        )

    return {
        **policy,
        "confidence": _clamp_confidence(
            confidence
        ),
    }


def _build_claim_assessments(
    case: dict[str, Any],
    evidence_refs: list[str],
    confidence: float,
) -> list[dict[str, Any]]:

    claims = _extract_claims(case)

    result: list[dict[str, Any]] = []

    for index, claim in enumerate(claims):

        claim_id = (
            claim.get("claim_id")
            or claim.get("id")
        )

        if not isinstance(claim_id, str):
            claim_id = f"claim_{index + 1}"

        topic = claim.get("topic")

        if (
            isinstance(topic, str)
            and topic.lower()
            in {
                "canceled_order_paid",
                "unavailable_order_paid",
                "late_delivery_seller",
                "late_delivery_logistics",
                "valid_split_payment",
                "payment_mismatch",
                "duplicate_charge",
                "refund_pending",
                "refund_failed",
            }
            and evidence_refs
        ):
            verdict = "supported"

        elif (
            topic == "unsupported_claim"
        ):
            verdict = "unsupported"

        else:
            verdict = (
                "insufficient_evidence"
                if not evidence_refs
                else "partially_supported"
            )

        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": _clamp_confidence(
                    confidence
                ),
                "evidence_refs": evidence_refs[:10],
            }
        )

    return result


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:

    case_id = str(
        case["case_id"]
    )

    # ---------------------------------------------------------
    # CASE RECEIVED
    # ---------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="case_received",
        actor="coordinator",
    )

    # ---------------------------------------------------------
    # ORDER SPECIALIST
    # ---------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order_agent",
    )

    order_evidence = await _order_specialist(
        case,
        gateway,
        trace,
    )

    order_refs = _collect_all_evidence_refs(
        order_evidence
    )

    # ---------------------------------------------------------
    # HANDOFF → PAYMENT
    # ---------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="payment_agent",
        evidence_refs=order_refs,
    )

    payment_evidence = await _payment_specialist(
        case,
        gateway,
        trace,
    )

    payment_refs = _collect_all_evidence_refs(
        payment_evidence
    )

    # ---------------------------------------------------------
    # HANDOFF → SHIPMENT
    # ---------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="shipment_agent",
        evidence_refs=payment_refs,
    )

    shipment_evidence = await _shipment_specialist(
        case,
        gateway,
        trace,
    )

    # ---------------------------------------------------------
    # GỘP EVIDENCE
    # ---------------------------------------------------------
    all_evidence = (
        order_evidence
        + payment_evidence
        + shipment_evidence
    )

    evidence_refs = _collect_all_evidence_refs(
        all_evidence
    )

    # ---------------------------------------------------------
    # POLICY
    # ---------------------------------------------------------
    policy = _policy_decision(
        case,
        evidence_refs,
        all_evidence,
    )

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy_agent",
        decision_code=policy["primary_issue"],
        evidence_refs=evidence_refs,
        attributes={
            "confidence": policy["confidence"],
        },
    )

    # ---------------------------------------------------------
    # HANDOFF → VERIFIER
    # ---------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy_agent",
        target="verifier",
        decision_code=policy["primary_issue"],
        evidence_refs=evidence_refs,
    )

    # ---------------------------------------------------------
    # VERIFICATION
    # ---------------------------------------------------------
    verified = _verify(
        policy,
        evidence_refs,
    )

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=verified["primary_issue"],
        evidence_refs=evidence_refs,
        attributes={
            "confidence": verified["confidence"],
        },
    )

    # ---------------------------------------------------------
    # ENTITIES
    # ---------------------------------------------------------
    order_ids = _extract_order_ids(
        all_evidence,
        case,
    )

    item_ids = _extract_item_ids(
        all_evidence
    )

    claim_assessments = _build_claim_assessments(
        case,
        evidence_refs,
        verified["confidence"],
    )

    primary_issue = verified[
        "primary_issue"
    ]

    # ---------------------------------------------------------
    # ROOT CAUSE
    # ---------------------------------------------------------
    root_cause_analysis = {
        "ranked_causes": [
            {
                "cause_code": primary_issue.upper(),
                "rank": 1,
            }
        ],
        "responsible_parties": [
            {
                "party_type": verified[
                    "responsible_party"
                ],
                "party_id": None,
            }
        ],
    }

    # ---------------------------------------------------------
    # FINANCIAL RESOLUTION
    # ---------------------------------------------------------
    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": 0.0,
        "refund_lines": [],
    }

    # ---------------------------------------------------------
    # ACTIONS
    # ---------------------------------------------------------
    resolution_actions = list(
        dict.fromkeys(
            verified.get(
                "actions",
                [],
            )
        )
    )

    if not resolution_actions:

        if (
            verified["case_status"]
            == "no_action"
        ):
            resolution_actions = [
                "no_action_required"
            ]
        else:
            resolution_actions = [
                "investigate_case"
            ]

    # ---------------------------------------------------------
    # OUTPUT
    # ---------------------------------------------------------
    output = {
        "schema_version":
            "day09-l3a-output-v2",

        "case_id":
            case_id,

        "assessment": {
            "primary_issue":
                primary_issue,

            "case_status":
                verified[
                    "case_status"
                ],

            "confidence":
                verified[
                    "confidence"
                ],
        },

        "affected_entities": {
            "order_ids":
                order_ids,

            "item_ids":
                item_ids,

            "seller_ids": [],

            "payment_references": [],

            "shipment_ids": [],
        },

        "root_cause_analysis":
            root_cause_analysis,

        "evidence_refs":
            evidence_refs[:30],

        "data_conflicts": [],

        "financial_resolution":
            financial_resolution,

        "resolution_actions":
            resolution_actions[:8],
    }

    if claim_assessments:
        output[
            "claim_assessments"
        ] = claim_assessments

    # ---------------------------------------------------------
    # CASE FINALIZED
    # ---------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="case_finalized",
        actor="coordinator",
        decision_code=primary_issue,
        evidence_refs=evidence_refs,
    )

    return output