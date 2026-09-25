"""Multi-agent workflow cho L3A.

Coordinator giao việc cho các specialist (order, seller, payment, shipment, policy).
Mỗi specialist chỉ gọi MCP tool thuộc phạm vi của mình và ghi `tool_result_consumed`.
Policy agent kết luận dựa trên evidence (claim của khách chỉ là giả thuyết),
verifier kiểm tra các invariant trước khi coordinator dựng output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ISSUE_TOPICS = {
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
}

# Domain evidence hỗ trợ từng kết luận; chỉ cite các domain này.
ISSUE_DOMAINS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order", "payment"),
    "unavailable_order_paid": ("order", "payment"),
    "late_delivery_seller": ("order", "item", "shipment", "seller"),
    "late_delivery_logistics": ("order", "shipment"),
    "valid_split_payment": ("order", "payment"),
    "payment_mismatch": ("order", "item", "payment"),
    "duplicate_charge": ("payment",),
    "refund_pending": ("payment", "refund"),
    "refund_failed": ("payment", "refund"),
    "unsupported_claim": ("order", "payment", "shipment"),
    "insufficient_evidence": ("order",),
}

MONEY_TOLERANCE = 0.01


# ---------------------------------------------------------------------------
# Helpers đọc dữ liệu evidence
# ---------------------------------------------------------------------------


def _walk(value: Any, keys: set[str]) -> list[Any]:
    """Mọi giá trị có key (không phân biệt hoa thường) thuộc `keys`, duyệt đệ quy."""
    found: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in keys:
                found.append(item)
            found.extend(_walk(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item, keys))
    return found


def _records(value: Any, marker_keys: set[str]) -> list[dict[str, Any]]:
    """Các dict (duyệt đệ quy) có chứa ít nhất một key trong `marker_keys`."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(str(key).lower() in marker_keys for key in value):
            found.append(value)
        for item in value.values():
            found.extend(_records(item, marker_keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_records(item, marker_keys))
    return found


def _strings(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(v) for v in values if isinstance(v, (str, int)) and str(v)))


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _first(record: dict[str, Any] | None, *keys: str) -> Any:
    if not record:
        return None
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        if lowered.get(key) not in (None, ""):
            return lowered[key]
    return None


def _money(value: float) -> float:
    return round(max(0.0, value), 2)


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 2)


def _lower(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


# ---------------------------------------------------------------------------
# Evidence store dùng chung trong một case
# ---------------------------------------------------------------------------


@dataclass
class CaseEvidence:
    case_id: str
    order_id: str | None
    by_tool: dict[str, dict[str, Any]] = field(default_factory=dict)

    def data(self, tool: str) -> Any:
        evidence = self.by_tool.get(tool)
        return evidence.get("data") if evidence else None

    def ref(self, tool: str) -> str | None:
        evidence = self.by_tool.get(tool)
        return evidence.get("evidence_ref") if evidence else None

    def refs_for_domains(self, domains: tuple[str, ...]) -> list[str]:
        refs = [
            evidence["evidence_ref"]
            for evidence in self.by_tool.values()
            if evidence.get("domain") in domains
        ]
        return list(dict.fromkeys(refs))

    def refs_for_tools(self, *tools: str) -> list[str]:
        return list(dict.fromkeys(r for t in tools if (r := self.ref(t))))

    def all_refs(self) -> list[str]:
        return list(dict.fromkeys(e["evidence_ref"] for e in self.by_tool.values()))

    # ---- order ----
    def order(self) -> dict[str, Any] | None:
        data = self.data("get_order")
        if isinstance(data, dict):
            nested = data.get("order")
            return nested if isinstance(nested, dict) else data
        return None

    def order_status(self) -> str:
        return _lower(_first(self.order(), "order_status", "status"))

    # ---- items ----
    def items(self) -> list[dict[str, Any]]:
        return _records(self.data("get_order_items"), {"price", "order_item_id", "item_id"})

    def items_total(self) -> float | None:
        items = self.items()
        if not items:
            return None
        total = 0.0
        for item in items:
            total += (_number(_first(item, "price")) or 0.0) + (
                _number(_first(item, "freight_value")) or 0.0
            )
        return round(total, 2)

    # ---- payments ----
    def base_payments(self) -> list[dict[str, Any]]:
        for tool in ("get_order_payments", "get_payment_timeline"):
            data = self.data(tool)
            payments = [
                record
                for record in _records(data, {"payment_value", "payment_sequential"})
                if _number(_first(record, "payment_value")) is not None
            ]
            if payments:
                return payments
        return []

    def payment_total(self) -> float | None:
        payments = self.base_payments()
        if not payments:
            return None
        return round(sum(_number(_first(p, "payment_value")) or 0.0 for p in payments), 2)

    def payment_events(self) -> list[dict[str, Any]]:
        # Lấy từ một nguồn duy nhất; không khử trùng lặp vì capture trùng chính là tín hiệu.
        for tool in ("get_payment_timeline", "get_order_payments"):
            events = _records(self.data(tool), {"event_type", "event", "lifecycle_event", "action"})
            if events:
                return events
        return []

    def refund_events(self) -> list[dict[str, Any]]:
        return _records(
            self.data("get_refund_timeline"),
            {"event_type", "status", "refund_status", "event", "refund_id"},
        )

    # ---- shipment ----
    def shipment(self) -> dict[str, Any]:
        data = self.data("get_shipment_summary")
        return data if isinstance(data, dict) else {}


def _event_kind(event: dict[str, Any]) -> str:
    return _lower(
        _first(event, "event_type", "event", "lifecycle_event", "action", "type", "status")
    )


def _event_amount(event: dict[str, Any]) -> float | None:
    return _number(_first(event, "amount_brl", "amount", "value", "payment_value", "refund_amount"))


# ---------------------------------------------------------------------------
# Specialist agents
# ---------------------------------------------------------------------------


async def _consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    store: CaseEvidence,
    *,
    actor: str,
    tool_name: str,
    arguments: dict[str, str],
) -> dict[str, Any] | None:
    """Gọi một MCP tool; lỗi của tool (vd not found) không làm hỏng cả case."""
    try:
        evidence = await gateway.call(tool_name, case_id=store.case_id, **arguments)
    except RuntimeError:
        return None
    store.by_tool[tool_name] = evidence
    trace.emit(
        case_id=store.case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
        attributes={"domain": evidence.get("domain")},
    )
    return evidence


async def _order_agent(gateway: EvidenceGateway, trace: TraceWriter, store: CaseEvidence) -> None:
    if not store.order_id:
        return
    args = {"order_id": store.order_id}
    # get_order là evidence bắt buộc: để lỗi lan ra để CLI retry cả case.
    evidence = await gateway.call("get_order", case_id=store.case_id, **args)
    store.by_tool["get_order"] = evidence
    trace.emit(
        case_id=store.case_id,
        event_type="tool_result_consumed",
        actor="order_agent",
        tool_name="get_order",
        evidence_refs=[evidence["evidence_ref"]],
        attributes={"domain": evidence.get("domain")},
    )
    await _consume(
        gateway, trace, store, actor="order_agent", tool_name="get_order_items", arguments=args
    )


async def _seller_agent(gateway: EvidenceGateway, trace: TraceWriter, store: CaseEvidence) -> None:
    if not store.order_id:
        return
    await _consume(
        gateway,
        trace,
        store,
        actor="seller_agent",
        tool_name="get_sellers",
        arguments={"order_id": store.order_id},
    )


async def _payment_agent(
    gateway: EvidenceGateway, trace: TraceWriter, store: CaseEvidence, topics: set[str]
) -> None:
    if not store.order_id:
        return
    args = {"order_id": store.order_id}
    await _consume(
        gateway, trace, store, actor="payment_agent", tool_name="get_order_payments", arguments=args
    )
    await _consume(
        gateway,
        trace,
        store,
        actor="payment_agent",
        tool_name="get_payment_timeline",
        arguments=args,
    )
    mentions_refund = any("refund" in _event_kind(e) for e in store.payment_events())
    if mentions_refund or topics & {"refund_pending", "refund_failed", "canceled_order_paid"}:
        await _consume(
            gateway,
            trace,
            store,
            actor="payment_agent",
            tool_name="get_refund_timeline",
            arguments=args,
        )


async def _shipment_agent(
    gateway: EvidenceGateway, trace: TraceWriter, store: CaseEvidence
) -> None:
    if not store.order_id:
        return
    await _consume(
        gateway,
        trace,
        store,
        actor="shipment_agent",
        tool_name="get_shipment_summary",
        arguments={"order_id": store.order_id},
    )


async def _policy_lookup(
    gateway: EvidenceGateway, trace: TraceWriter, store: CaseEvidence, policy_version: str | None
) -> None:
    if policy_version:
        await _consume(
            gateway,
            trace,
            store,
            actor="policy_agent",
            tool_name="get_policy",
            arguments={"policy_version": policy_version},
        )


# ---------------------------------------------------------------------------
# Detectors: mỗi hàm trả về Finding nếu evidence xác nhận issue
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    issue: str
    case_status: str
    party_type: str
    cause_code: str
    confidence: float
    refund: float = 0.0
    refund_reason: str = ""
    refund_entity: str | None = None
    party_id: str | None = None
    tools: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def _captured_total(store: CaseEvidence) -> float | None:
    captures = [
        amount
        for e in store.payment_events()
        if any(word in _event_kind(e) for word in ("capture", "charge", "paid", "settle"))
        and (amount := _event_amount(e)) is not None
    ]
    if captures:
        return round(sum(captures), 2)
    return store.payment_total()


def _refunded_total(store: CaseEvidence, statuses: tuple[str, ...]) -> float:
    total = 0.0
    for event in store.refund_events() + store.payment_events():
        kind = _event_kind(event)
        if "refund" in kind or event in store.refund_events():
            status = _lower(_first(event, "status", "refund_status", "event_type", "event"))
            if any(s in status or s in kind for s in statuses):
                total += _event_amount(event) or 0.0
    return round(total, 2)


def _detect_paid_but_not_fulfilled(store: CaseEvidence, status: str, issue: str) -> Finding | None:
    if store.order_status() != status:
        return None
    paid = _captured_total(store) or 0.0
    refunded = _refunded_total(store, ("completed", "succeeded", "success", "refunded", "done"))
    outstanding = _money(paid - refunded)
    if outstanding <= MONEY_TOLERANCE:
        return None
    return Finding(
        issue=issue,
        case_status="action_required",
        party_type="seller" if issue == "unavailable_order_paid" else "platform",
        cause_code="ORDER_CANCELED_AFTER_PAYMENT"
        if issue == "canceled_order_paid"
        else "ORDER_UNAVAILABLE_AFTER_PAYMENT",
        confidence=0.9,
        refund=outstanding,
        refund_reason="FULL_REFUND_UNFULFILLED_ORDER",
        refund_entity=store.order_id,
        tools=("get_order", "get_order_payments", "get_payment_timeline", "get_refund_timeline"),
        actions=("issue_full_refund",),
    )


def _delivery_times(store: CaseEvidence) -> dict[str, datetime | None]:
    order = store.order() or {}
    ship = store.shipment()
    merged = {**{str(k).lower(): v for k, v in order.items()}}
    merged.update({str(k).lower(): v for k, v in ship.items() if not isinstance(v, (list, dict))})
    limits = [_time(v) for v in _walk(store.data("get_order_items"), {"shipping_limit_date"})]
    limits += [_time(v) for v in _walk(ship, {"shipping_limit_date", "seller_handoff_limit"})]
    limits = [t for t in limits if t]

    def pick(*keys: str) -> datetime | None:
        for key in keys:
            if (value := _time(merged.get(key))) is not None:
                return value
        for value in _walk(ship, set(keys)):
            if (parsed := _time(value)) is not None:
                return parsed
        return None

    return {
        "delivered": pick("order_delivered_customer_date", "delivered_customer_at", "delivered_at"),
        "estimated": pick(
            "order_estimated_delivery_date", "estimated_delivery_date", "estimated_delivery_at"
        ),
        "carrier": pick(
            "order_delivered_carrier_date", "delivered_carrier_at", "carrier_handoff_at"
        ),
        "limit": min(limits) if limits else None,
    }


def _late_seller_id(store: CaseEvidence, handoff: datetime | None) -> str | None:
    items = store.items()
    late = [
        item
        for item in items
        if handoff and (limit := _time(_first(item, "shipping_limit_date"))) and handoff > limit
    ]
    candidates = _strings([_first(i, "seller_id") for i in (late or items)])
    return candidates[0] if candidates else None


def _late_compensation(store: CaseEvidence) -> float:
    """Bồi thường trễ giao theo policy công khai: hoàn phí vận chuyển nếu policy quy định."""
    rules = _walk(store.data("get_policy"), {"late_delivery"})
    if not any("freight" in json.dumps(rule).lower() for rule in rules):
        return 0.0
    return _money(sum(_number(_first(i, "freight_value")) or 0.0 for i in store.items()))


def _detect_late_delivery(store: CaseEvidence) -> Finding | None:
    times = _delivery_times(store)
    delivered, estimated = times["delivered"], times["estimated"]
    if not delivered or not estimated or delivered <= estimated:
        return None
    carrier, limit = times["carrier"], times["limit"]
    seller_late = bool(carrier and limit and carrier > limit)
    if seller_late:
        seller_id = _late_seller_id(store, carrier)
        return Finding(
            issue="late_delivery_seller",
            case_status="action_required",
            party_type="seller",
            party_id=seller_id,
            cause_code="SELLER_HANDOFF_AFTER_LIMIT",
            confidence=0.85 if seller_id else 0.7,
            tools=(
                "get_order",
                "get_order_items",
                "get_shipment_summary",
                "get_sellers",
                "get_policy",
            ),
            refund=_late_compensation(store),
            refund_reason="LATE_DELIVERY_FREIGHT_REFUND",
            refund_entity=store.order_id,
            actions=("compensate_late_delivery", "notify_seller_sla_breach"),
        )
    return Finding(
        issue="late_delivery_logistics",
        case_status="action_required",
        party_type="logistics_provider",
        cause_code="CARRIER_DELIVERY_DELAY",
        confidence=0.85 if carrier and limit else 0.65,
        tools=("get_order", "get_shipment_summary", "get_order_items", "get_policy"),
        refund=_late_compensation(store),
        refund_reason="LATE_DELIVERY_FREIGHT_REFUND",
        refund_entity=store.order_id,
        actions=("compensate_late_delivery", "escalate_to_logistics_provider"),
    )


def _detect_duplicate_charge(store: CaseEvidence) -> Finding | None:
    captures: list[tuple[str, float]] = []
    for event in store.payment_events():
        kind = _event_kind(event)
        if not any(word in kind for word in ("capture", "charge")):
            continue
        amount = _event_amount(event)
        if amount is None:
            continue
        key = _lower(
            _first(event, "payment_reference", "payment_id", "payment_sequential", "reference")
        )
        captures.append((key, amount))
    seen: dict[tuple[str, float], int] = {}
    for key, amount in captures:
        seen[(key, round(amount, 2))] = seen.get((key, round(amount, 2)), 0) + 1
    duplicated = sum(amount * (count - 1) for (_, amount), count in seen.items() if count > 1)
    if duplicated <= MONEY_TOLERANCE and "duplicate" not in " ".join(
        _event_kind(e) for e in store.payment_events()
    ):
        return None
    if duplicated <= MONEY_TOLERANCE:
        dup_events = [e for e in store.payment_events() if "duplicate" in _event_kind(e)]
        duplicated = sum(_event_amount(e) or 0.0 for e in dup_events)
    return Finding(
        issue="duplicate_charge",
        case_status="action_required",
        party_type="payment_provider",
        cause_code="DUPLICATE_CAPTURE",
        confidence=0.9,
        refund=_money(duplicated),
        refund_reason="DUPLICATE_CAPTURE_REFUND",
        refund_entity=store.order_id,
        tools=("get_order_payments", "get_payment_timeline"),
        actions=("refund_duplicate_charge",),
    )


def _detect_refund_problem(store: CaseEvidence) -> Finding | None:
    events = store.refund_events() or [
        e for e in store.payment_events() if "refund" in _event_kind(e)
    ]
    if not events:
        return None
    statuses = " ".join(
        _lower(_first(e, "status", "refund_status")) + " " + _event_kind(e) for e in events
    )
    amounts = [a for e in events if (a := _event_amount(e)) is not None]
    amount = _money(max(amounts)) if amounts else 0.0
    completed = any(s in statuses for s in ("completed", "succeeded", "refunded"))
    if any(s in statuses for s in ("failed", "rejected", "declined", "error")) and not completed:
        return Finding(
            issue="refund_failed",
            case_status="action_required",
            party_type="payment_provider",
            cause_code="REFUND_PROCESSING_FAILED",
            confidence=0.85,
            refund=amount,
            refund_reason="RETRY_FAILED_REFUND",
            refund_entity=store.order_id,
            tools=("get_refund_timeline", "get_payment_timeline"),
            actions=("retry_refund",),
        )
    if any(s in statuses for s in ("pending", "requested", "initiated", "processing")) and not (
        completed
    ):
        return Finding(
            issue="refund_pending",
            case_status="needs_investigation",
            party_type="payment_provider",
            cause_code="REFUND_NOT_SETTLED",
            confidence=0.8,
            tools=("get_refund_timeline", "get_payment_timeline"),
            actions=("monitor_refund_settlement",),
        )
    return None


def _detect_payment_amounts(store: CaseEvidence) -> Finding | None:
    expected = store.items_total()
    paid = _captured_total(store)
    payments = store.base_payments()
    if expected is None or paid is None:
        return None
    difference = round(paid - expected, 2)
    if abs(difference) > MONEY_TOLERANCE:
        overcharge = _money(difference)
        return Finding(
            issue="payment_mismatch",
            case_status="action_required" if overcharge > 0 else "needs_investigation",
            party_type="payment_provider",
            cause_code="CAPTURED_AMOUNT_MISMATCH",
            confidence=0.8,
            refund=overcharge,
            refund_reason="OVERCHARGE_REFUND",
            refund_entity=store.order_id,
            tools=("get_order_items", "get_order_payments", "get_payment_timeline"),
            actions=("refund_overcharge",) if overcharge > 0 else ("reconcile_payment",),
            conflicts=[
                {
                    "field": "order_total_brl",
                    "sources": ["get_order_items", "get_order_payments"],
                    "selected_source": "get_order_items",
                    "resolution_code": "ITEM_TOTAL_IS_AUTHORITATIVE",
                }
            ],
        )
    if len(payments) > 1:
        return Finding(
            issue="valid_split_payment",
            case_status="no_action",
            party_type="customer",
            cause_code="LEGITIMATE_SPLIT_PAYMENT",
            confidence=0.85,
            tools=("get_order_items", "get_order_payments"),
            actions=("no_action_required",),
        )
    return None


def _detect(store: CaseEvidence, claimed: str | None) -> Finding | None:
    """Ưu tiên kiểm chứng giả thuyết của khách, sau đó quét các issue còn lại."""
    detectors = {
        "canceled_order_paid": lambda: _detect_paid_but_not_fulfilled(
            store, "canceled", "canceled_order_paid"
        ),
        "unavailable_order_paid": lambda: _detect_paid_but_not_fulfilled(
            store, "unavailable", "unavailable_order_paid"
        ),
        "late_delivery_seller": lambda: _detect_late_delivery(store),
        "late_delivery_logistics": lambda: _detect_late_delivery(store),
        "duplicate_charge": lambda: _detect_duplicate_charge(store),
        "refund_pending": lambda: _detect_refund_problem(store),
        "refund_failed": lambda: _detect_refund_problem(store),
        "payment_mismatch": lambda: _detect_payment_amounts(store),
        "valid_split_payment": lambda: _detect_payment_amounts(store),
    }
    order = [claimed] if claimed in detectors else []
    order += [name for name in detectors if name not in order]
    for name in order:
        finding = detectors[name]()
        if finding:
            return finding
    return None


def _unsupported(store: CaseEvidence) -> Finding:
    return Finding(
        issue="unsupported_claim",
        case_status="no_action",
        party_type="customer",
        cause_code="CLAIM_CONTRADICTED_BY_EVIDENCE",
        confidence=0.75,
        tools=("get_order", "get_order_payments", "get_shipment_summary"),
        actions=("no_action_required",),
    )


def _insufficient() -> Finding:
    return Finding(
        issue="insufficient_evidence",
        case_status="needs_investigation",
        party_type="unknown",
        cause_code="EVIDENCE_UNAVAILABLE",
        confidence=0.3,
        tools=("get_order",),
        actions=("collect_additional_evidence",),
    )


# ---------------------------------------------------------------------------
# Policy + verifier
# ---------------------------------------------------------------------------


def _policy_decision(store: CaseEvidence, claimed: str | None) -> Finding:
    if not store.by_tool.get("get_order"):
        return _insufficient()
    finding = _detect(store, claimed)
    if finding is None:
        return _unsupported(store)
    if claimed and claimed != finding.issue and claimed in ISSUE_TOPICS:
        finding.confidence = min(finding.confidence, 0.75)
    return finding


def _verify(finding: Finding, store: CaseEvidence) -> tuple[Finding, list[str]]:
    """Kiểm tra invariant nhất quán; trả về finding đã chỉnh và danh sách mã điều chỉnh."""
    adjustments: list[str] = []
    if finding.case_status == "no_action" and finding.refund > 0:
        finding.refund = 0.0
        adjustments.append("REFUND_CLEARED_FOR_NO_ACTION")
    if finding.party_type == "seller" and not finding.party_id:
        sellers = _strings(
            _walk(store.data("get_sellers"), {"seller_id"})
            + _walk(store.data("get_order_items"), {"seller_id"})
        )
        if sellers:
            finding.party_id = sellers[0]
            adjustments.append("SELLER_ID_FILLED")
        else:
            finding.confidence = min(finding.confidence, 0.6)
            adjustments.append("SELLER_ID_MISSING")
    if not store.refs_for_tools(*finding.tools):
        finding.confidence = min(finding.confidence, 0.3)
        adjustments.append("NO_SUPPORTING_EVIDENCE")
    finding.confidence = _clamp(finding.confidence)
    return finding, adjustments


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------


def _claims(case: dict[str, Any]) -> list[dict[str, Any]]:
    request = case.get("customer_request")
    claims = request.get("claims") if isinstance(request, dict) else None
    return [c for c in claims or [] if isinstance(c, dict)]


def _claimed_order_id(case: dict[str, Any]) -> str | None:
    request = case.get("customer_request")
    if isinstance(request, dict):
        for key in ("claimed_order_id", "order_id"):
            if isinstance(request.get(key), str) and request[key]:
                return request[key]
    values = _strings(_walk(case, {"claimed_order_id", "order_id"}))
    return values[0] if values else None


def _entities(store: CaseEvidence, finding: Finding) -> dict[str, list[str]]:
    everything = [e.get("data") for e in store.by_tool.values() if e.get("domain") != "policy"]
    order_ids = _strings(
        ([store.order_id] if store.order_id else []) + _walk(everything, {"order_id"})
    )
    item_ids = _strings(_walk(store.data("get_order_items"), {"item_id", "order_item_uid"}))
    if not item_ids:
        item_ids = _strings(_walk(store.data("get_order_items"), {"order_item_id"}))
    seller_ids = _strings(
        _walk(store.data("get_order_items"), {"seller_id"})
        + _walk(store.data("get_sellers"), {"seller_id"})
    )
    if finding.party_type == "seller" and finding.party_id:
        seller_ids = [finding.party_id]
    payment_refs = _strings(
        _walk(
            [store.data("get_order_payments"), store.data("get_payment_timeline")],
            {"payment_reference", "payment_id", "transaction_id"},
        )
    )
    shipment_ids = _strings(_walk(store.data("get_shipment_summary"), {"shipment_id"}))
    return {
        "order_ids": order_ids[:20],
        "item_ids": item_ids[:20],
        "seller_ids": seller_ids[:20],
        "payment_references": payment_refs[:20],
        "shipment_ids": shipment_ids[:20],
    }


def _cited_refs(store: CaseEvidence, finding: Finding) -> list[str]:
    refs = store.refs_for_tools(*finding.tools)
    if not refs:
        refs = store.refs_for_domains(ISSUE_DOMAINS.get(finding.issue, ("order",)))
    if not refs:
        refs = store.all_refs()[:1]
    return refs[:30]


def _claim_assessments(
    case: dict[str, Any], finding: Finding, refs: list[str], store: CaseEvidence
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, claim in enumerate(_claims(case)[:5]):
        claim_id = claim.get("claim_id") or claim.get("id") or f"claim_{index + 1}"
        topic = _lower(claim.get("topic"))
        if finding.issue == "insufficient_evidence":
            verdict, confidence = "insufficient_evidence", finding.confidence
        elif topic in ISSUE_TOPICS:
            if topic == finding.issue:
                verdict, confidence = "supported", finding.confidence
            elif topic == "unsupported_claim":
                verdict = "supported" if finding.issue == "unsupported_claim" else "unsupported"
                confidence = finding.confidence
            elif topic.split("_")[:2] == finding.issue.split("_")[:2]:
                verdict, confidence = "partially_supported", finding.confidence * 0.8
            else:
                verdict, confidence = "unsupported", finding.confidence
        elif topic == "requested_full_refund":
            paid = _captured_total(store) or 0.0
            if finding.refund <= MONEY_TOLERANCE:
                verdict = "unsupported"
            elif paid and finding.refund + MONEY_TOLERANCE >= paid:
                verdict = "supported"
            else:
                verdict = "partially_supported"
            confidence = finding.confidence * 0.9
        else:
            verdict, confidence = "insufficient_evidence", 0.3
        result.append(
            {
                "claim_id": str(claim_id)[:64],
                "verdict": verdict,
                "confidence": _clamp(confidence),
                "evidence_refs": refs[:10],
            }
        )
    return result


def _build_output(
    case: dict[str, Any], store: CaseEvidence, finding: Finding, refs: list[str]
) -> dict[str, Any]:
    refund_lines = []
    if finding.refund > MONEY_TOLERANCE:
        refund_lines.append(
            {
                "reason_code": finding.refund_reason or finding.cause_code,
                "amount_brl": _money(finding.refund),
                "entity_id": finding.refund_entity,
            }
        )
    actions = list(dict.fromkeys(finding.actions)) or ["investigate_case"]
    if finding.case_status == "no_action":
        actions = ["no_action_required"]
    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": store.case_id,
        "assessment": {
            "primary_issue": finding.issue,
            "case_status": finding.case_status,
            "confidence": finding.confidence,
        },
        "affected_entities": _entities(store, finding),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": finding.cause_code, "rank": 1}],
            "responsible_parties": [
                {"party_type": finding.party_type, "party_id": finding.party_id}
            ],
        },
        "evidence_refs": refs,
        "data_conflicts": finding.conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _money(sum(line["amount_brl"] for line in refund_lines)),
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions[:8],
    }
    assessments = _claim_assessments(case, finding, refs, store)
    if assessments:
        output["claim_assessments"] = assessments
    return output


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    store = CaseEvidence(case_id=case_id, order_id=_claimed_order_id(case))
    topics = {_lower(c.get("topic")) for c in _claims(case)}
    claimed = next((t for t in topics if t in ISSUE_TOPICS), None)
    policy_version = case.get("policy_version")

    trace.emit(
        case_id=case_id,
        event_type="case_received",
        actor="coordinator",
        attributes={"claimed_issue": claimed, "claim_count": len(_claims(case))},
    )

    # Order agent: dữ kiện gốc của đơn hàng.
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="order_agent"
    )
    await _order_agent(gateway, trace, store)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order_agent",
        target="coordinator",
        evidence_refs=store.all_refs()[:20],
        attributes={"order_status": store.order_status() or None},
    )

    # Các specialist còn lại, mỗi agent nhận task từ coordinator và handoff kết quả về.
    specialists = (
        ("seller_agent", lambda: _seller_agent(gateway, trace, store), ("get_sellers",)),
        (
            "payment_agent",
            lambda: _payment_agent(gateway, trace, store, topics),
            ("get_order_payments", "get_payment_timeline", "get_refund_timeline"),
        ),
        (
            "shipment_agent",
            lambda: _shipment_agent(gateway, trace, store),
            ("get_shipment_summary",),
        ),
    )
    for actor, run, tools in specialists:
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
        await run()
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            evidence_refs=store.refs_for_tools(*tools),
        )

    # Policy agent: tra policy công khai rồi kết luận từ evidence.
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy_agent",
        evidence_refs=store.all_refs()[:20],
    )
    await _policy_lookup(gateway, trace, store, policy_version)
    finding = _policy_decision(store, claimed)
    refs = _cited_refs(store, finding)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy_agent",
        decision_code=finding.issue,
        evidence_refs=refs[:20],
        attributes={
            "case_status": finding.case_status,
            "responsible_party": finding.party_type,
            "confidence": finding.confidence,
            "claimed_issue": claimed,
        },
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy_agent",
        target="verifier",
        decision_code=finding.issue,
        evidence_refs=refs[:20],
    )

    # Verifier.
    finding, adjustments = _verify(finding, store)
    refs = _cited_refs(store, finding)
    output = _build_output(case, store, finding, refs)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=adjustments[0] if adjustments else "INVARIANTS_OK",
        evidence_refs=refs[:20],
        attributes={
            "adjustment_count": len(adjustments),
            "confidence": finding.confidence,
            "refund_brl": output["financial_resolution"]["recommended_refund_brl"],
        },
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="verifier",
        target="coordinator",
        decision_code=finding.issue,
        evidence_refs=refs[:20],
    )

    trace.emit(
        case_id=case_id,
        event_type="case_finalized",
        actor="coordinator",
        decision_code=finding.issue,
        evidence_refs=refs[:20],
    )
    return output
