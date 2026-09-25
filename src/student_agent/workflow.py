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
    refs: list[str] = []

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
    """
    Tìm một giá trị string theo key ở bất kỳ cấp nào
    trong dictionary/list của input case.
    """

    if isinstance(data, dict):

        for key, value in data.items():

            if str(key).lower() in target_keys:
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
        min(
            1.0,
            float(value),
        ),
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

        for key in (
            "order_id",
            "orderId",
        ):

            value = evidence.get(key)

            if isinstance(value, str):
                result.append(value)

        orders = evidence.get("orders")

        if isinstance(orders, list):

            for order in orders:

                if isinstance(order, dict):

                    value = (
                        order.get("order_id")
                        or order.get("orderId")
                    )

                    if isinstance(value, str):
                        result.append(value)

    return list(
        dict.fromkeys(result)
    )


def _extract_item_ids(
    evidence_groups: list[dict[str, Any]],
) -> list[str]:

    result: list[str] = []

    for evidence in evidence_groups:

        items = evidence.get("items")

        if isinstance(items, list):

            for item in items:

                if isinstance(item, dict):

                    value = (
                        item.get("item_id")
                        or item.get("itemId")
                        or item.get("id")
                    )

                    if isinstance(value, str):
                        result.append(value)

    return list(
        dict.fromkeys(result)
    )


def _policy_decision(
    case: dict[str, Any],
    evidence_refs: list[str],
) -> dict[str, Any]:

    case_text = " ".join(
        str(value).lower()
        for key, value in case.items()
        if key != "case_id"
        and isinstance(
            value,
            (str, int, float),
        )
    )

    primary_issue = (
        "insufficient_evidence"
    )

    responsible_party = "unknown"

    case_status = (
        "needs_investigation"
    )

    confidence = 0.45

    actions: list[str] = []

    if (
        "canceled" in case_text
        and "paid" in case_text
    ):

        primary_issue = (
            "canceled_order_paid"
        )

        responsible_party = "seller"
        case_status = "action_required"
        confidence = 0.80

        actions.append(
            "review_canceled_paid_order"
        )

    elif (
        "unavailable" in case_text
        and "paid" in case_text
    ):

        primary_issue = (
            "unavailable_order_paid"
        )

        responsible_party = "seller"
        case_status = "action_required"
        confidence = 0.78

        actions.append(
            "review_unavailable_paid_order"
        )

    elif (
        "late" in case_text
        and "seller" in case_text
    ):

        primary_issue = (
            "late_delivery_seller"
        )

        responsible_party = "seller"
        case_status = "action_required"
        confidence = 0.75

        actions.append(
            "review_seller_delivery_delay"
        )

    elif (
        "late" in case_text
        and (
            "logistics" in case_text
            or "shipping" in case_text
        )
    ):

        primary_issue = (
            "late_delivery_logistics"
        )

        responsible_party = (
            "logistics_provider"
        )

        case_status = "action_required"
        confidence = 0.75

        actions.append(
            "review_logistics_delivery_delay"
        )

    elif "duplicate" in case_text:

        primary_issue = (
            "duplicate_charge"
        )

        responsible_party = (
            "payment_provider"
        )

        case_status = "action_required"
        confidence = 0.82

        actions.append(
            "review_duplicate_charge"
        )

    elif (
        "refund" in case_text
        and "pending" in case_text
    ):

        primary_issue = (
            "refund_pending"
        )

        responsible_party = "platform"
        case_status = "action_required"
        confidence = 0.78

        actions.append(
            "review_refund_status"
        )

    elif (
        "refund" in case_text
        and "failed" in case_text
    ):

        primary_issue = (
            "refund_failed"
        )

        responsible_party = (
            "payment_provider"
        )

        case_status = "action_required"
        confidence = 0.78

        actions.append(
            "review_failed_refund"
        )

    elif "mismatch" in case_text:

        primary_issue = (
            "payment_mismatch"
        )

        responsible_party = (
            "payment_provider"
        )

        case_status = (
            "needs_investigation"
        )

        confidence = 0.65

        actions.append(
            "investigate_payment_mismatch"
        )

    elif (
        "split payment" in case_text
        or "split_payment" in case_text
    ):

        primary_issue = (
            "valid_split_payment"
        )

        responsible_party = "platform"
        case_status = "no_action"
        confidence = 0.75

    if not evidence_refs:

        primary_issue = (
            "insufficient_evidence"
        )

        responsible_party = "unknown"

        case_status = (
            "needs_investigation"
        )

        confidence = 0.30

        actions = [
            "collect_additional_evidence"
        ]

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

    if (
        policy["primary_issue"]
        == "insufficient_evidence"
    ):

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

    claims = case.get(
        "claims",
        [],
    )

    if not isinstance(
        claims,
        list,
    ):
        return []

    result: list[dict[str, Any]] = []

    for index, claim in enumerate(
        claims
    ):

        if not isinstance(
            claim,
            dict,
        ):
            continue

        claim_id = (
            claim.get("claim_id")
            or claim.get("id")
        )

        if not isinstance(
            claim_id,
            str,
        ):

            claim_id = (
                f"claim_{index + 1}"
            )

        result.append(
            {
                "claim_id": claim_id,

                "verdict": (
                    "supported"
                    if evidence_refs
                    else "insufficient_evidence"
                ),

                "confidence": (
                    _clamp_confidence(
                        confidence
                    )
                ),

                "evidence_refs":
                    evidence_refs[:10],
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

    # =========================================================
    # 1. CASE RECEIVED
    # =========================================================

    trace.emit(
        case_id=case_id,
        event_type="case_received",
        actor="coordinator",
    )

    # =========================================================
    # 2. ORDER SPECIALIST
    # =========================================================

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order_agent",
    )

    order_evidence = (
        await _order_specialist(
            case,
            gateway,
            trace,
        )
    )

    # =========================================================
    # 3. PAYMENT SPECIALIST
    # =========================================================

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="payment_agent",
        evidence_refs=(
            _collect_all_evidence_refs(
                order_evidence
            )
        ),
    )

    payment_evidence = (
        await _payment_specialist(
            case,
            gateway,
            trace,
        )
    )

    # =========================================================
    # 4. SHIPMENT SPECIALIST
    # =========================================================

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="shipment_agent",
        evidence_refs=(
            _collect_all_evidence_refs(
                payment_evidence
            )
        ),
    )

    shipment_evidence = (
        await _shipment_specialist(
            case,
            gateway,
            trace,
        )
    )

    # =========================================================
    # 5. COLLECT EVIDENCE
    # =========================================================

    all_evidence = (
        order_evidence
        + payment_evidence
        + shipment_evidence
    )

    evidence_refs = (
        _collect_all_evidence_refs(
            all_evidence
        )
    )

    # =========================================================
    # 6. POLICY AGENT
    # =========================================================

    policy = _policy_decision(
        case,
        evidence_refs,
    )

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy_agent",
        target="verifier",
        decision_code=(
            policy["primary_issue"]
        ),
        evidence_refs=evidence_refs,
    )

    # =========================================================
    # 7. VERIFIER
    # =========================================================

    verified = _verify(
        policy,
        evidence_refs,
    )

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=(
            verified["primary_issue"]
        ),
        evidence_refs=evidence_refs,
        attributes={
            "confidence": (
                verified["confidence"]
            ),
        },
    )

    # =========================================================
    # 8. ENTITIES
    # =========================================================

    order_ids = _extract_order_ids(
        all_evidence,
        case,
    )

    item_ids = _extract_item_ids(
        all_evidence,
    )

    # =========================================================
    # 9. CLAIM ASSESSMENTS
    # =========================================================

    claim_assessments = (
        _build_claim_assessments(
            case,
            evidence_refs,
            verified["confidence"],
        )
    )

    # =========================================================
    # 10. ROOT CAUSE
    # =========================================================

    primary_issue = (
        verified["primary_issue"]
    )

    root_cause_analysis = {
        "ranked_causes": [
            {
                "cause_code":
                    primary_issue.upper(),
                "rank": 1,
            }
        ],

        "responsible_parties": [
            {
                "party_type":
                    verified[
                        "responsible_party"
                    ],

                "party_id": None,
            }
        ],
    }

    # =========================================================
    # 11. FINANCIAL RESOLUTION
    # =========================================================

    financial_resolution = {
        "currency": "BRL",

        "recommended_refund_brl": 0.0,

        "refund_lines": [],
    }

    # =========================================================
    # 12. RESOLUTION ACTIONS
    # =========================================================

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

    # =========================================================
    # 13. FINAL OUTPUT
    # =========================================================

    output: dict[str, Any] = {

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

            "seller_ids":
                [],

            "payment_references":
                [],

            "shipment_ids":
                [],
        },

        "root_cause_analysis":
            root_cause_analysis,

        "evidence_refs":
            evidence_refs[:30],

        "data_conflicts":
            [],

        "financial_resolution":
            financial_resolution,

        "resolution_actions":
            resolution_actions[:8],
    }

    if claim_assessments:

        output[
            "claim_assessments"
        ] = claim_assessments

    # =========================================================
    # 14. CASE FINALIZED
    # =========================================================

    trace.emit(
        case_id=case_id,
        event_type="case_finalized",
        actor="coordinator",
        decision_code=primary_issue,
        evidence_refs=evidence_refs,
    )

    return output