"""Multi-agent workflow cho L3A.

Coordinator giao việc cho các specialist (order, seller, payment, shipment, policy).
Mỗi specialist chỉ gọi MCP tool thuộc phạm vi của mình và ghi `tool_result_consumed`.
Policy agent kết luận dựa trên evidence (claim của khách chỉ là giả thuyết),
verifier kiểm tra các invariant trước khi coordinator dựng output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

# Issue mà hướng xử lý là hoàn toàn bộ số đã thu.
FULL_REFUND_ISSUES = {"canceled_order_paid", "unavailable_order_paid", "refund_failed"}


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
# Lọc nhiễu: chỉ dùng các dòng nằm trong cửa sổ thời gian của đơn hàng
# ---------------------------------------------------------------------------

WINDOW_BEFORE = timedelta(days=1)
WINDOW_AFTER = timedelta(days=3)


def _order_window(store: CaseEvidence) -> tuple[datetime, datetime] | None:
    """[ngày mua − 1 ngày, max(ngày giao, ngày dự kiến) + 3 ngày].

    Evidence của gateway trộn dòng thuộc đơn này với dòng mang mốc thời gian không
    liên quan (capture/refund/hạn giao hàng lệch hàng tuần). Dòng ngoài cửa sổ bị bỏ.
    """
    order = store.order() or {}
    start = _time(_first(order, "order_purchase_timestamp", "order_approved_at"))
    ends = [
        _time(_first(order, "order_estimated_delivery_date")),
        _time(_first(order, "order_delivered_customer_date")),
    ]
    ends = [t for t in ends if t]
    if not start or not ends:
        return None
    return start - WINDOW_BEFORE, max(ends) + WINDOW_AFTER


def _in_window(store: CaseEvidence, value: Any) -> bool:
    window = _order_window(store)
    moment = _time(value)
    if window is None or moment is None:
        return True
    return window[0] <= moment <= window[1]


def _event_time(event: dict[str, Any]) -> Any:
    return _first(event, "event_at", "occurred_at", "created_at", "timestamp")


def _dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bỏ dòng trùng y hệt (cùng thời điểm, cùng số tiền): bản ghi lặp, không phải giao dịch."""
    unique: list[dict[str, Any]] = []
    for record in records:
        if record not in unique:
            unique.append(record)
    return unique


def _relevant_payment_events(store: CaseEvidence) -> list[dict[str, Any]]:
    """Capture/đối soát diễn ra quanh lúc duyệt đơn; dùng cửa sổ hẹp ±1 ngày khi có mốc này."""
    approved = _time(_first(store.order(), "order_approved_at", "order_purchase_timestamp"))
    events = store.payment_events()
    if approved is None:
        return [e for e in events if _in_window(store, _event_time(e))]
    return [
        e
        for e in events
        if (moment := _time(_event_time(e))) is None or abs(moment - approved) <= timedelta(days=1)
    ]


def _relevant_captures(store: CaseEvidence) -> list[dict[str, Any]]:
    return _dedupe(
        [
            e
            for e in _relevant_payment_events(store)
            if "captur" in _event_kind(e) and _event_amount(e) is not None
        ]
    )


def _relevant_refunds(store: CaseEvidence) -> list[dict[str, Any]]:
    return [e for e in store.refund_events() if _in_window(store, _event_time(e))]


def _relevant_items(store: CaseEvidence) -> list[dict[str, Any]]:
    items = store.items()
    relevant = [i for i in items if _in_window(store, _first(i, "shipping_limit_date"))]
    return relevant or items


def _relevant_shipping_limits(store: CaseEvidence) -> list[dict[str, Any]]:
    limits = _records(store.shipment(), {"shipping_limit_at", "shipping_limit_date"})
    relevant = [
        s
        for s in limits
        if _in_window(store, _first(s, "shipping_limit_at", "shipping_limit_date"))
    ]
    if relevant:
        return relevant
    return [
        {"seller_id": _first(i, "seller_id"), "shipping_limit_at": _first(i, "shipping_limit_date")}
        for i in _relevant_items(store)
    ]


def _relevant_shipment_events(store: CaseEvidence) -> list[dict[str, Any]]:
    events = _records(store.shipment().get("events"), {"event_type"})
    return [e for e in events if _in_window(store, _event_time(e))]


def _sum(records: list[dict[str, Any]]) -> float:
    return round(sum(_event_amount(r) or 0.0 for r in records), 2)


def _item_total(items: list[dict[str, Any]]) -> float:
    return round(
        sum(
            (_number(_first(i, "price")) or 0.0) + (_number(_first(i, "freight_value")) or 0.0)
            for i in _dedupe(items)
        ),
        2,
    )


# ---------------------------------------------------------------------------
# Detectors: mỗi hàm trả về Finding nếu evidence xác nhận issue
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    issue: str
    cause_code: str
    confidence: float
    evidence_amount: float = 0.0
    party_id: str | None = None
    tools: tuple[str, ...] = ()
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    # Điền từ policy (hoặc mặc định) trong _apply_policy.
    case_status: str = "needs_investigation"
    party_type: str = "unknown"
    refund: float = 0.0
    actions: tuple[str, ...] = ()


PAYMENT_TOOLS = ("get_order_payments", "get_payment_timeline")


def _detect_unfulfilled(store: CaseEvidence) -> Finding | None:
    status = store.order_status()
    if status not in {"canceled", "unavailable"}:
        return None
    captured = _sum(_relevant_captures(store))
    if captured <= MONEY_TOLERANCE:
        return None
    canceled = status == "canceled"
    return Finding(
        issue="canceled_order_paid" if canceled else "unavailable_order_paid",
        cause_code="ORDER_CANCELED_AFTER_CAPTURE" if canceled else "ITEM_UNAVAILABLE_AFTER_CAPTURE",
        confidence=0.9,
        evidence_amount=captured,
        tools=("get_order", *PAYMENT_TOOLS) + (() if canceled else ("get_order_items",)),
    )


def _detect_late_delivery(store: CaseEvidence) -> Finding | None:
    order = store.order() or {}
    delivered = _time(_first(order, "order_delivered_customer_date"))
    estimated = _time(_first(order, "order_estimated_delivery_date"))
    shipment = store.shipment()
    delivered = delivered or _time(_first(shipment, "delivered_customer_at"))
    estimated = estimated or _time(_first(shipment, "estimated_delivery_at"))
    if not delivered or not estimated or delivered <= estimated:
        return None

    carrier = _time(_first(order, "order_delivered_carrier_date")) or _time(
        _first(shipment, "delivered_carrier_at")
    )
    limits = _relevant_shipping_limits(store)
    late_limits = [
        s
        for s in limits
        if carrier
        and (limit := _time(_first(s, "shipping_limit_at", "shipping_limit_date")))
        and carrier > limit
    ]
    event_actors = {_lower(_first(e, "actor")) for e in _relevant_shipment_events(store)}
    seller_fault = bool(late_limits) or ("seller" in event_actors and not limits)
    # Hai nguồn (mốc thời gian và event của shipment) phải thống nhất mới cho confidence cao.
    agrees = ("seller" in event_actors) == seller_fault if event_actors else True
    confidence = 0.9 if agrees else 0.65
    freight = round(
        sum(_number(_first(i, "freight_value")) or 0.0 for i in _dedupe(_relevant_items(store))), 2
    )
    captured = _sum(_relevant_captures(store))
    if captured > MONEY_TOLERANCE:
        # Chỉ hoàn phần phí vận chuyển thực sự đã thu.
        freight = min(freight, captured)
    if seller_fault:
        sellers = _strings([_first(s, "seller_id") for s in late_limits or limits])
        return Finding(
            issue="late_delivery_seller",
            cause_code="SELLER_HANDOFF_AFTER_LIMIT",
            confidence=confidence,
            evidence_amount=freight,
            party_id=sellers[0] if sellers else None,
            tools=("get_order", "get_order_items", "get_shipment_summary", "get_sellers"),
        )
    return Finding(
        issue="late_delivery_logistics",
        cause_code="CARRIER_DELIVERY_DELAY",
        confidence=confidence,
        evidence_amount=freight,
        tools=("get_order", "get_order_items", "get_shipment_summary"),
    )


def _detect_refund_problem(store: CaseEvidence) -> Finding | None:
    refunds = _relevant_refunds(store)
    if not refunds:
        return None
    statuses = {_lower(_first(r, "status")) for r in refunds}
    if statuses & {"completed", "succeeded", "refunded", "settled"}:
        return None
    amount = round(max(_event_amount(r) or 0.0 for r in refunds), 2)
    if statuses & {"failed", "rejected", "declined", "error"}:
        return Finding(
            issue="refund_failed",
            cause_code="REFUND_PROCESSING_FAILED",
            confidence=0.9,
            evidence_amount=amount,
            tools=("get_refund_timeline", *PAYMENT_TOOLS),
        )
    if statuses & {"pending", "requested", "processing", "initiated"}:
        return Finding(
            issue="refund_pending",
            cause_code="REFUND_NOT_SETTLED",
            confidence=0.85,
            evidence_amount=amount,
            tools=("get_refund_timeline", *PAYMENT_TOOLS),
        )
    return None


def _detect_mismatch(store: CaseEvidence) -> Finding | None:
    flags = [
        e
        for e in _relevant_payment_events(store)
        if "mismatch" in _event_kind(e)
        and _lower(_first(e, "status")) not in {"closed", "resolved"}
    ]
    if not flags:
        return None
    amount = round(max(_event_amount(e) or 0.0 for e in flags), 2)
    expected = _item_total(_relevant_items(store))
    captured = _sum(_relevant_captures(store))
    return Finding(
        issue="payment_mismatch",
        cause_code="PAYMENT_RECONCILIATION_MISMATCH",
        confidence=0.9,
        evidence_amount=amount,
        tools=("get_order_items", *PAYMENT_TOOLS),
        conflicts=[
            {
                "field": "captured_amount_brl",
                "sources": ["get_order_items", "get_payment_timeline"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "RECONCILIATION_EVENT_OPEN",
            }
        ]
        if abs(expected - captured) > MONEY_TOLERANCE
        else [],
    )


def _detect_capture_pattern(store: CaseEvidence) -> Finding | None:
    """Nhiều capture trong cửa sổ: khớp tổng đơn là split; cùng số tiền và vượt tổng là trùng."""
    captures = _relevant_captures(store)
    if len(captures) < 2:
        return None
    expected = _item_total(_relevant_items(store))
    captured = _sum(captures)
    amounts = [round(_event_amount(c) or 0.0, 2) for c in captures]
    if expected and abs(captured - expected) <= MONEY_TOLERANCE:
        return Finding(
            issue="valid_split_payment",
            cause_code="LEGITIMATE_SPLIT_PAYMENT",
            confidence=0.9,
            tools=("get_order_items", *PAYMENT_TOOLS),
        )
    repeated = [a for a in set(amounts) if amounts.count(a) > 1]
    if repeated and captured > expected + MONEY_TOLERANCE:
        return Finding(
            issue="duplicate_charge",
            cause_code="DUPLICATE_CAPTURE",
            confidence=0.85,
            evidence_amount=max(repeated),
            tools=("get_order_items", *PAYMENT_TOOLS),
        )
    return None


def _detect(store: CaseEvidence) -> Finding | None:
    for detector in (
        _detect_unfulfilled,
        _detect_late_delivery,
        _detect_refund_problem,
        _detect_mismatch,
        _detect_capture_pattern,
    ):
        finding = detector(store)
        if finding:
            return finding
    return None


# ---------------------------------------------------------------------------
# Policy + verifier
# ---------------------------------------------------------------------------

DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "case_status": "action_required",
        "action": "issue_refund",
        "party": "platform",
    },
    "unavailable_order_paid": {
        "case_status": "action_required",
        "action": "issue_refund",
        "party": "seller",
    },
    "late_delivery_seller": {
        "case_status": "action_required",
        "action": "refund_freight",
        "party": "seller",
    },
    "late_delivery_logistics": {
        "case_status": "action_required",
        "action": "refund_freight",
        "party": "logistics_provider",
    },
    "duplicate_charge": {
        "case_status": "action_required",
        "action": "refund_duplicate_charge",
        "party": "payment_provider",
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "action": "reconcile_payment",
        "party": "payment_provider",
    },
    "refund_failed": {
        "case_status": "action_required",
        "action": "retry_refund",
        "party": "payment_provider",
    },
    "refund_pending": {
        "case_status": "needs_investigation",
        "action": "monitor_refund",
        "party": "payment_provider",
        "no_refund": True,
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "action": "document_no_action",
        "party": "customer",
        "no_refund": True,
    },
    "unsupported_claim": {
        "case_status": "no_action",
        "action": "document_no_action",
        "party": "customer",
        "no_refund": True,
    },
    "insufficient_evidence": {
        "case_status": "needs_investigation",
        "action": "collect_additional_evidence",
        "party": "unknown",
        "no_refund": True,
    },
}


def _policy_rule(store: CaseEvidence, issue: str) -> dict[str, Any] | None:
    data = store.data("get_policy")
    rules = data.get("rules") if isinstance(data, dict) else None
    rule = rules.get(issue) if isinstance(rules, dict) else None
    return rule if isinstance(rule, dict) else None


def _apply_policy(store: CaseEvidence, finding: Finding) -> Finding:
    """Trạng thái, action, bên chịu trách nhiệm và số tiền hoàn theo policy công khai."""
    default = DEFAULT_RULES[finding.issue]
    rule = _policy_rule(store, finding.issue)
    finding.case_status = (rule or {}).get("case_status") or default["case_status"]
    finding.actions = ((rule or {}).get("recommended_action") or default["action"],)
    parties = (rule or {}).get("responsible_parties") or []
    finding.party_type = (
        parties[0].get("party_type") if parties and isinstance(parties[0], dict) else None
    ) or default["party"]
    if finding.party_type != "seller":
        finding.party_id = None

    policy_refund = _number((rule or {}).get("refund_brl"))
    if default.get("no_refund") or finding.case_status == "no_action":
        finding.refund = 0.0
    elif policy_refund is not None:
        finding.refund = _money(policy_refund)
        if finding.evidence_amount and abs(finding.evidence_amount - policy_refund) > 0.01:
            # Policy là nguồn chuẩn cho số tiền; evidence lệch thì hạ nhẹ confidence.
            finding.confidence = min(finding.confidence, 0.8)
    else:
        finding.refund = _money(finding.evidence_amount)
    if "get_policy" not in finding.tools and rule is not None:
        finding.tools = (*finding.tools, "get_policy")
    return finding


def _policy_decision(store: CaseEvidence, claimed: str | None) -> Finding:
    if not store.by_tool.get("get_order"):
        finding = Finding(
            issue="insufficient_evidence",
            cause_code="EVIDENCE_UNAVAILABLE",
            confidence=0.3,
            tools=("get_order",),
        )
        return _apply_policy(store, finding)
    finding = _detect(store)
    if finding is None:
        finding = Finding(
            issue="unsupported_claim",
            cause_code="CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
            confidence=0.85 if claimed in {None, "unsupported_claim"} else 0.7,
            tools=("get_order", "get_shipment_summary", *PAYMENT_TOOLS),
        )
    elif claimed and claimed in ISSUE_TOPICS and claimed != finding.issue:
        # Evidence mâu thuẫn với claim: vẫn theo evidence, nhưng bớt chắc chắn.
        finding.confidence = min(finding.confidence, 0.7)
    return _apply_policy(store, finding)


def _verify(finding: Finding, store: CaseEvidence) -> tuple[Finding, list[str]]:
    """Kiểm tra invariant nhất quán; trả về finding đã chỉnh và danh sách mã điều chỉnh."""
    adjustments: list[str] = []
    if finding.case_status == "no_action" and finding.refund > 0:
        finding.refund = 0.0
        adjustments.append("REFUND_CLEARED_FOR_NO_ACTION")
    if finding.party_type == "seller" and not finding.party_id:
        sellers = _strings(
            [_first(i, "seller_id") for i in _relevant_items(store)]
            + _walk(store.data("get_sellers"), {"seller_id"})
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
        [_first(i, "seller_id") for i in _relevant_items(store)]
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
            if finding.refund <= MONEY_TOLERANCE and finding.issue != "refund_pending":
                verdict = "unsupported"
            elif finding.issue in FULL_REFUND_ISSUES:
                verdict = "supported"
            else:
                # Chỉ hoàn một phần (phí ship, khoản trùng/chênh lệch) hoặc refund đang xử lý.
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
                "reason_code": finding.actions[0] if finding.actions else finding.cause_code,
                "amount_brl": _money(finding.refund),
                "entity_id": store.order_id,
            }
        )
    actions = list(dict.fromkeys(finding.actions)) or ["investigate_case"]
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
