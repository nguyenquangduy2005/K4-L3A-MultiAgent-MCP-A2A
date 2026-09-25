"""MCP Evidence Gateway chạy local để phát triển và kiểm thử khi server thi không dùng được.

Server cung cấp đúng 10 tool như gateway thi, trả về envelope `day09-mcp-evidence-v1`.
Dữ liệu được sinh deterministic (theo schema Olist) cho các order trong `inputs/`, mỗi case
một kịch bản theo topic khách claim; một số case được cố ý cho dữ liệu mâu thuẫn với claim
để kiểm tra rằng agent kết luận theo evidence chứ không theo lời khách.

Mọi evidence ref được ghi vào `.local_mcp/audit.jsonl`; nhãn kỳ vọng để tự chấm ghi vào
`.local_mcp/expected_labels.json`. Ref do server này cấp KHÔNG hợp lệ với server thi.

Chạy:
    python scripts/local_mcp_server.py            # http://127.0.0.1:8001/mcp
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".local_mcp"
FMT = "%Y-%m-%d %H:%M:%S"

# Case có dữ liệu mâu thuẫn với claim: case_id -> kịch bản thực tế.
TWISTS = {
    "late_delivery_seller": [(3, "late_delivery_logistics")],
    "late_delivery_logistics": [(5, "late_delivery_seller")],
    "payment_mismatch": [(7, "valid_split_payment")],
    "duplicate_charge": [(2, "unsupported_claim")],
    "canceled_order_paid": [(8, "unsupported_claim")],
}

POLICY = {
    "policy_version": "EC_POLICY_V1",
    "currency": "BRL",
    "rules": {
        "canceled_or_unavailable_paid": {
            "refund": "full_captured_amount",
            "responsible_party": {"canceled": "platform", "unavailable": "seller"},
        },
        "late_delivery": {
            "late_when": "delivered_customer_date > estimated_delivery_date",
            "seller_fault_when": "delivered_carrier_date > shipping_limit_date",
            "compensation": "freight_value",
        },
        "duplicate_charge": {"refund": "duplicated_capture_amount"},
        "payment_mismatch": {"refund": "captured_minus_order_total_when_positive"},
        "refund_failed": {"action": "retry_refund", "refund": "failed_refund_amount"},
        "refund_pending": {"action": "monitor_refund_settlement", "refund": 0},
        "valid_split_payment": {"action": "no_action_required"},
    },
}


def _ts(value: datetime | None) -> str | None:
    return value.strftime(FMT) if value else None


def _short(order_id: str) -> str:
    return order_id[:12]


def _scenario_for(case: dict[str, Any], index_in_topic: int) -> str:
    topic = next(
        c["topic"]
        for c in case["customer_request"]["claims"]
        if c["topic"] != "requested_full_refund"
    )
    for position, actual in TWISTS.get(topic, []):
        if index_in_topic == position:
            return actual
    return topic


def _build_case(case: dict[str, Any], scenario: str) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]
    rng = random.Random(case_id)
    short = _short(order_id)
    customer = f"cust-{hashlib.md5(order_id.encode()).hexdigest()[:12]}"
    purchase = datetime(2017, 1, 1) + timedelta(days=rng.randint(0, 500), hours=rng.randint(8, 20))
    approved = purchase + timedelta(hours=1)

    n_items = 2 if scenario == "late_delivery_seller" else rng.choice([1, 1, 2])
    items = []
    for number in range(1, n_items + 1):
        price = round(rng.uniform(25, 300), 2)
        freight = round(rng.uniform(8, 35), 2)
        items.append(
            {
                "order_item_id": number,
                "item_id": f"item-{short}-{number}",
                "product_id": f"prod-{short}-{number}",
                "seller_id": f"seller-{short}-{number}",
                "shipping_limit_date": _ts(approved + timedelta(days=3)),
                "price": price,
                "freight_value": freight,
            }
        )
    total = round(sum(i["price"] + i["freight_value"] for i in items), 2)

    status = "delivered"
    limit = approved + timedelta(days=3)
    carrier = approved + timedelta(days=2)
    estimated = purchase + timedelta(days=15)
    delivered: datetime | None = purchase + timedelta(days=10)
    late_seller_id = None

    if scenario == "late_delivery_seller":
        carrier = limit + timedelta(days=4)
        delivered = estimated + timedelta(days=6)
        late_seller_id = items[-1]["seller_id"]
        items[0]["shipping_limit_date"] = _ts(limit + timedelta(days=10))
    elif scenario == "late_delivery_logistics":
        delivered = estimated + timedelta(days=7)
    elif scenario in {"canceled_order_paid", "unavailable_order_paid"}:
        status = "canceled" if scenario == "canceled_order_paid" else "unavailable"
        carrier, delivered = None, None

    reference = f"pay-{short}-1"
    payments = [
        {
            "payment_reference": reference,
            "payment_sequential": 1,
            "payment_type": "credit_card",
            "payment_installments": rng.choice([1, 2, 3]),
            "payment_value": total,
        }
    ]
    if scenario == "valid_split_payment":
        voucher = round(total * 0.3, 2)
        payments = [
            {
                "payment_reference": f"pay-{short}-1",
                "payment_sequential": 1,
                "payment_type": "voucher",
                "payment_installments": 1,
                "payment_value": voucher,
            },
            {
                "payment_reference": f"pay-{short}-2",
                "payment_sequential": 2,
                "payment_type": "credit_card",
                "payment_installments": 1,
                "payment_value": round(total - voucher, 2),
            },
        ]

    events = []
    for payment in payments:
        events.append(
            {
                "event_type": "authorized",
                "payment_reference": payment["payment_reference"],
                "amount": payment["payment_value"],
                "occurred_at": _ts(purchase),
            }
        )
        events.append(
            {
                "event_type": "captured",
                "payment_reference": payment["payment_reference"],
                "amount": payment["payment_value"],
                "occurred_at": _ts(approved),
            }
        )

    overcharge = 0.0
    if scenario == "payment_mismatch":
        overcharge = round(rng.uniform(10, 60), 2)
        payments[0]["payment_value"] = round(total + overcharge, 2)
        for event in events:
            event["amount"] = payments[0]["payment_value"]
    if scenario == "duplicate_charge":
        events.append(
            {
                "event_type": "captured",
                "payment_reference": reference,
                "amount": total,
                "occurred_at": _ts(approved + timedelta(minutes=3)),
            }
        )

    refunds = []
    if scenario in {"refund_pending", "refund_failed"}:
        amount = total
        refunds.append(
            {
                "refund_id": f"rf-{short}-1",
                "payment_reference": reference,
                "event_type": "refund_requested",
                "status": "requested",
                "amount": amount,
                "occurred_at": _ts(delivered + timedelta(days=2)),
            }
        )
        refunds.append(
            {
                "refund_id": f"rf-{short}-1",
                "payment_reference": reference,
                "event_type": "refund_processing"
                if scenario == "refund_pending"
                else "refund_failed",
                "status": "pending" if scenario == "refund_pending" else "failed",
                "amount": amount,
                "occurred_at": _ts(delivered + timedelta(days=4)),
            }
        )

    shipment_events = []
    if carrier:
        shipment_events.append({"event_type": "handed_to_carrier", "occurred_at": _ts(carrier)})
    if delivered:
        shipment_events.append({"event_type": "delivered", "occurred_at": _ts(delivered)})

    order = {
        "order_id": order_id,
        "customer_id": customer,
        "customer_unique_id": f"u-{customer}",
        "order_status": status,
        "order_purchase_timestamp": _ts(purchase),
        "order_approved_at": _ts(approved),
        "order_delivered_carrier_date": _ts(carrier),
        "order_delivered_customer_date": _ts(delivered),
        "order_estimated_delivery_date": _ts(estimated),
    }
    data = {
        "get_order": order,
        "get_order_items": {"order_id": order_id, "items": items},
        "get_order_payments": {"order_id": order_id, "payments": payments},
        "get_payment_timeline": {"order_id": order_id, "payments": payments, "events": events},
        "get_refund_timeline": {"order_id": order_id, "refunds": refunds},
        "get_shipment_summary": {
            "order_id": order_id,
            "shipment_id": f"shp-{short}" if carrier else None,
            "carrier": "correios",
            "shipping_limit_date": _ts(limit),
            "order_delivered_carrier_date": _ts(carrier),
            "order_delivered_customer_date": _ts(delivered),
            "order_estimated_delivery_date": _ts(estimated),
            "events": shipment_events,
        },
        "get_sellers": {
            "order_id": order_id,
            "sellers": [
                {"seller_id": i["seller_id"], "seller_city": "sao paulo", "seller_state": "SP"}
                for i in items
            ],
        },
        "get_product_context": {
            "order_id": order_id,
            "products": [
                {
                    "product_id": i["product_id"],
                    "product_category_name": "utilidades_domesticas",
                    "product_category_name_english": "housewares",
                }
                for i in items
            ],
        },
        "get_customer_history": {
            "customer_unique_id": f"u-{customer}",
            "orders": [{"order_id": order_id, "order_status": status}],
        },
    }

    freight = round(sum(i["freight_value"] for i in items), 2)
    expected_refund = {
        "canceled_order_paid": total,
        "unavailable_order_paid": total,
        "late_delivery_seller": freight,
        "late_delivery_logistics": freight,
        "duplicate_charge": total,
        "payment_mismatch": overcharge,
        "refund_failed": total,
        "refund_pending": 0.0,
        "valid_split_payment": 0.0,
        "unsupported_claim": 0.0,
    }[scenario]
    expected = {
        "primary_issue": scenario,
        "recommended_refund_brl": expected_refund,
        "late_seller_id": late_seller_id,
    }
    return {"order_id": order_id, "customer": f"u-{customer}", "data": data, "expected": expected}


DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_product_context": "product",
    "get_customer_history": "customer",
    "get_policy": "policy",
}


class Store:
    def __init__(self) -> None:
        case_set = json.loads((ROOT / "case-set.json").read_text("utf-8"))
        self.cases: dict[str, dict[str, Any]] = {}
        per_topic: dict[str, int] = {}
        for case_id in case_set["case_ids"]:
            case = json.loads((ROOT / "inputs" / f"{case_id}.json").read_text("utf-8"))
            topic = next(
                c["topic"]
                for c in case["customer_request"]["claims"]
                if c["topic"] != "requested_full_refund"
            )
            index = per_topic.get(topic, 0)
            per_topic[topic] = index + 1
            self.cases[case_id] = _build_case(case, _scenario_for(case, index))
        STATE_DIR.mkdir(exist_ok=True)
        (STATE_DIR / "expected_labels.json").write_text(
            json.dumps({k: v["expected"] for k, v in self.cases.items()}, indent=2), "utf-8"
        )
        self.audit = STATE_DIR / "audit.jsonl"

    def evidence(self, tool: str, case_id: str, data: Any) -> dict[str, Any]:
        ref = f"ev_{secrets.token_urlsafe(24)}"
        body = json.dumps(data, sort_keys=True, ensure_ascii=False)
        with self.audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"evidence_ref": ref, "case_id": case_id, "tool": tool}) + "\n")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": ref,
            "result_hash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            "domain": DOMAINS[tool],
            "data": data,
        }

    def order_tool(self, tool: str, case_id: str, order_id: str) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise ValueError(f"unknown case_id {case_id}")
        if order_id != case["order_id"]:
            raise ValueError("order not found in case scope")
        return self.evidence(tool, case_id, case["data"][tool])


def build_server() -> MCPServer:
    store = Store()
    server = MCPServer("day09-local-evidence-gateway")

    def order_tool(name: str, description: str) -> None:
        async def handler(case_id: str, order_id: str) -> dict[str, Any]:
            return store.order_tool(name, case_id, order_id)

        handler.__name__ = name
        server.tool(name=name, description=description, structured_output=True)(handler)

    order_tool("get_order", "Return the authoritative order row for one order.")
    order_tool("get_order_items", "Return item and seller rows belonging to one order.")
    order_tool("get_order_payments", "Return payment rows and lifecycle evidence for one order.")
    order_tool("get_payment_timeline", "Return base payments and payment lifecycle events.")
    order_tool("get_refund_timeline", "Return refund lifecycle events for a scoped order.")
    order_tool("get_shipment_summary", "Return delivery timestamps, handoff limits and events.")
    order_tool("get_sellers", "Return seller records associated with an order's items.")
    order_tool("get_product_context", "Return products and categories for a scoped order.")

    @server.tool(name="get_policy", structured_output=True)
    async def get_policy(case_id: str, policy_version: str) -> dict[str, Any]:
        """Return the public machine-readable policy for the requested version."""
        if case_id not in store.cases or policy_version != POLICY["policy_version"]:
            raise ValueError("unknown case or policy version")
        return store.evidence("get_policy", case_id, POLICY)

    @server.tool(name="get_customer_history", structured_output=True)
    async def get_customer_history(case_id: str, customer_unique_id: str) -> dict[str, Any]:
        """Return order history for one scoped customer identity."""
        case = store.cases.get(case_id)
        if case is None or customer_unique_id != case["customer"]:
            raise ValueError("customer not found in case scope")
        return store.evidence("get_customer_history", case_id, case["data"]["get_customer_history"])

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    app = build_server().streamable_http_app(host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
