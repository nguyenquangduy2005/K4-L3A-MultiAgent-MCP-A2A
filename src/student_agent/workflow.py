"""L3A multi-agent workflow on LangGraph.

coordinator -> (order-item | payment | shipment) -> policy -> verifier -> output
The verifier may send the case back to the coordinator once; after that it falls back to a safe
"insufficient_evidence" answer. Each agent may only call the MCP tools in evidence.PERMISSIONS.
"""

from __future__ import annotations

import asyncio
import operator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from . import rules
from .contracts import ContractError, Contracts
from .evidence import EvidenceCollector, EvidenceStore, Fetched
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

CASE_TIMEOUT_SECONDS = 180.0
MAX_REPLANS = 1

# evidence key -> tool
TOOL_BY_KEY = {
    "order": "get_order",
    "items": "get_order_items",
    "sellers": "get_sellers",
    "payments": "get_order_payments",
    "timeline": "get_payment_timeline",
    "refunds": "get_refund_timeline",
    "shipment": "get_shipment_summary",
    "policy": "get_policy",
}


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class CaseState(TypedDict, total=False):
    case: dict[str, Any]
    plan: dict[str, Any]
    replans: int
    messages: Annotated[list[dict[str, Any]], operator.add]  # A2A envelopes
    signals: Annotated[dict[str, Any], _merge]
    draft: dict[str, Any] | None
    output: dict[str, Any] | None


@dataclass
class Runtime:
    case_id: str
    collector: EvidenceCollector
    store: EvidenceStore
    trace: TraceWriter
    contracts: Contracts
    fetched: dict[str, Fetched] = field(default_factory=dict)
    consumed: set[str] = field(default_factory=set)
    failures: dict[str, str] = field(default_factory=dict)

    def emit(self, event_type: str, actor: str, **fields: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **fields)

    async def get(self, agent: str, key: str, **arguments: str) -> Fetched:
        result = await self.collector.fetch(agent, TOOL_BY_KEY[key], **arguments)
        self.fetched[key] = result
        if result.ok:
            self.failures.pop(key, None)
            # A cached result (replan) is not a new server call: report each ref only once.
            if result.ref not in self.consumed:
                self.consumed.add(result.ref)
                self.emit(
                    "tool_result_consumed", agent, tool_name=result.tool, evidence_refs=[result.ref]
                )
        else:
            # Reported on the specialist's own handoff, so the lifecycle stays strictly ordered.
            self.failures[key] = result.status
        return result

    def data(self, key: str) -> Any:
        result = self.fetched.get(key)
        return result.data if result and result.ok else None

    def refs(self) -> dict[str, str]:
        return {k: v.ref for k, v in self.fetched.items() if v.ok and v.ref}

    def fallback_refs(self) -> list[str]:
        """Only the order/payment evidence actually inspected before giving up."""
        refs = self.refs()
        return [refs[key] for key in ("order", "payments", "timeline") if key in refs]


def _rt(config: RunnableConfig) -> Runtime:
    return config["configurable"]["rt"]


def _envelope(
    sender: str, receiver: str, kind: str, case_id: str, **payload: Any
) -> dict[str, Any]:
    return {"from": sender, "to": receiver, "kind": kind, "case_id": case_id, "payload": payload}


# --- agents ---------------------------------------------------------------------------------


async def coordinator(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    rt = _rt(config)
    case = state["case"]
    replans = state.get("replans", 0)
    if replans:
        rt.collector.forget_failures()
    order_id = case.get("customer_request", {}).get("claimed_order_id")
    plan = {"order_id": order_id, "policy_version": case.get("policy_version")}
    for agent in ("order-item-agent", "payment-agent", "shipment-agent"):
        rt.emit(
            "task_assigned",
            "coordinator",
            target=agent,
            decision_code="replan" if replans else "collect_evidence",
        )
    return {
        "plan": plan,
        "draft": None,
        "output": None,
        "messages": [_envelope("coordinator", "specialists", "task", rt.case_id, **plan)],
    }


def _handoff(rt: Runtime, agent: str, keys: list[str]) -> dict[str, Any]:
    refs = [rt.fetched[k].ref for k in keys if k in rt.fetched and rt.fetched[k].ok]
    rt.emit(
        "handoff",
        agent,
        target="policy-agent",
        decision_code="findings_ready",
        evidence_refs=refs or None,
        attributes={key: rt.failures[key] for key in keys if key in rt.failures} or None,
    )
    return {"messages": [_envelope(agent, "policy-agent", "result", rt.case_id, keys=keys)]}


async def order_item_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    rt, agent = _rt(config), "order-item-agent"
    order_id = state["plan"]["order_id"]
    if not order_id:
        return _handoff(rt, agent, [])
    order, items, _ = await asyncio.gather(
        rt.get(agent, "order", order_id=order_id),
        rt.get(agent, "items", order_id=order_id),
        rt.get(agent, "sellers", order_id=order_id),
    )
    signals: dict[str, Any] = {}
    if order.ok and items.ok:
        signals["order"] = rules.order_signals(order.data, items.data)
    return {**_handoff(rt, agent, ["order", "items", "sellers"]), "signals": signals}


async def payment_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    rt, agent = _rt(config), "payment-agent"
    order_id = state["plan"]["order_id"]
    if not order_id:
        return _handoff(rt, agent, [])
    _, timeline, refunds = await asyncio.gather(
        rt.get(agent, "payments", order_id=order_id),
        rt.get(agent, "timeline", order_id=order_id),
        rt.get(agent, "refunds", order_id=order_id),
    )
    signals: dict[str, Any] = {}
    if timeline.ok:
        signals["payment"] = rules.payment_signals(order_id, timeline.data)
    # A tool error means the gateway holds no refund events for this order; only a transport
    # failure leaves the refund state unknown.
    if refunds.ok or refunds.status == "tool_error":
        signals["refund"] = rules.refund_signals(refunds.data if refunds.ok else None)
    return {**_handoff(rt, agent, ["payments", "timeline", "refunds"]), "signals": signals}


async def shipment_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    rt, agent = _rt(config), "shipment-agent"
    order_id = state["plan"]["order_id"]
    if not order_id:
        return _handoff(rt, agent, [])
    shipment = await rt.get(agent, "shipment", order_id=order_id)
    signals = {"shipment": rules.shipment_signals(shipment.data)} if shipment.ok else {}
    return {**_handoff(rt, agent, ["shipment"]), "signals": signals}


async def policy_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    rt, agent = _rt(config), "policy-agent"
    case, signals = state["case"], state["signals"]
    policy = await rt.get(agent, "policy", policy_version=state["plan"]["policy_version"] or "")
    needed = ("order", "payment", "refund", "shipment")
    if policy.ok and all(name in signals for name in needed):
        try:
            decision = rules.decide(
                case,
                signals["order"],
                signals["payment"],
                signals["refund"],
                signals["shipment"],
                policy.data,
            )
            draft = rules.build_output(
                case,
                decision,
                signals["order"],
                signals["payment"],
                signals["shipment"],
                rt.refs(),
            )
        except (KeyError, TypeError, ValueError):
            decision, draft = None, None
    else:
        decision, draft = None, None
    if draft is None:
        missing = [name for name in needed if name not in signals]
        if not policy.ok:
            missing.append("policy")
        code, attributes = "insufficient_evidence", {"missing": ",".join(missing)}
    else:
        code = decision["issue"]
        attributes = {
            "confidence": decision["confidence"],
            "supported_issues": len(decision["supported"]),
            "refund_brl": decision["refund_brl"],
        }
    rt.emit("policy_decided", agent, decision_code=code, attributes=attributes)
    rt.emit("handoff", agent, target="verifier-agent", decision_code="draft_ready")
    return {
        "draft": draft,
        "messages": [_envelope(agent, "verifier-agent", "result", rt.case_id, issue=code)],
    }


# --- verifier -------------------------------------------------------------------------------


def verify(
    rt: Runtime, case: dict[str, Any], draft: dict[str, Any] | None, signals: dict[str, Any]
) -> list[str]:
    if draft is None:
        return ["no_draft"]
    problems: list[str] = []
    try:
        rt.contracts.validate_output(draft, f"draft/{rt.case_id}")
    except ContractError:
        return ["schema"]
    if draft["case_id"] != case["case_id"]:
        problems.append("case_id")
    cited = set(draft["evidence_refs"])
    for claim in draft.get("claim_assessments", []):
        cited.update(claim["evidence_refs"])
    if not cited <= rt.store.refs:
        problems.append("evidence_ownership")
    for claim in draft.get("claim_assessments", []):
        if claim["verdict"] != "insufficient_evidence" and not claim["evidence_refs"]:
            problems.append("claim_linkage")
            break
    money = draft["financial_resolution"]
    line_total = sum((Decimal(str(x["amount_brl"])) for x in money["refund_lines"]), Decimal(0))
    refund = Decimal(str(money["recommended_refund_brl"]))
    if line_total != refund:
        problems.append("refund_total")
    if refund > Decimal(str(signals["payment"]["captured_brl"])):
        problems.append("refund_exceeds_paid")
    status = draft["assessment"]["case_status"]
    if status == "no_action" and (refund != 0 or money["refund_lines"]):
        problems.append("no_action_with_refund")
    if status == "action_required" and not draft["resolution_actions"]:
        problems.append("action_missing")
    entities = draft["affected_entities"]
    if not set(entities["item_ids"]) <= set(signals["order"]["item_ids"]) or not set(
        entities["seller_ids"]
    ) <= set(signals["order"]["seller_ids"]):
        problems.append("entity_scope")
    if not evidence_has_required_domains(draft):
        problems.append("evidence_missing")
    problems.extend(check_against_policy(rt.data("policy"), draft, signals["order"]))
    if draft["assessment"]["confidence"] >= 1.0:
        problems.append("overconfident")
    return problems


def check_against_policy(
    policy: dict[str, Any] | None, draft: dict[str, Any], order: dict[str, Any]
) -> list[str]:
    """Cross-field consistency: issue, status, parties, refund and action must match one rule."""
    issue = draft["assessment"]["primary_issue"]
    if policy is None or issue not in policy.get("rules", {}):
        return []
    rule = policy["rules"][issue]
    problems: list[str] = []
    if draft["assessment"]["case_status"] != rule["case_status"]:
        problems.append("status_vs_issue")
    if draft["resolution_actions"] != [rule["recommended_action"]]:
        problems.append("action_vs_issue")
    if Decimal(str(draft["financial_resolution"]["recommended_refund_brl"])) != Decimal(
        str(rule["refund_brl"])
    ):
        problems.append("refund_vs_issue")
    parties = draft["root_cause_analysis"]["responsible_parties"]
    if sorted(p["party_type"] for p in parties) != sorted(
        p["party_type"] for p in rule["responsible_parties"]
    ):
        problems.append("party_vs_issue")
    for party in parties:
        if party["party_type"] == "seller":
            known = order["seller_ids"]
            if party["party_id"] is None:
                ambiguous = len(known) != 1  # several sellers: policy cannot name one
                if not ambiguous:
                    problems.append("seller_not_in_evidence")
            elif party["party_id"] not in known:
                problems.append("seller_not_in_evidence")
        elif party["party_id"] is not None:
            problems.append("party_id_unexpected")
    return problems


def evidence_has_required_domains(draft: dict[str, Any]) -> bool:
    return bool(draft["evidence_refs"]) or draft["assessment"]["primary_issue"] == (
        "insufficient_evidence"
    )


async def verifier_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    rt, case = _rt(config), state["case"]
    draft = state.get("draft")
    replans = state.get("replans", 0)
    try:
        problems = verify(rt, case, draft, state.get("signals", {}))
    except (KeyError, TypeError, ValueError):
        problems = ["verifier_error"]
    if not problems:
        rt.emit(
            "verification_completed",
            "verifier-agent",
            decision_code="pass",
            attributes={"issues": 0},
        )
        return {"output": draft}
    if replans < MAX_REPLANS:
        rt.emit(
            "verification_completed",
            "verifier-agent",
            decision_code="replan",
            attributes={"issues": len(problems), "first": problems[0]},
        )
        rt.emit("handoff", "verifier-agent", target="coordinator", decision_code="replan")
        return {"output": None, "replans": replans + 1}
    output = rules.fallback_output(case, rt.fallback_refs())
    rt.emit(
        "verification_completed",
        "verifier-agent",
        decision_code="fallback",
        attributes={"issues": len(problems), "first": problems[0]},
    )
    return {"output": output}


def _route(state: CaseState) -> str:
    return END if state.get("output") is not None else "coordinator"


def build_graph() -> Any:
    graph = StateGraph(CaseState)
    graph.add_node("coordinator", coordinator)
    graph.add_node("order_item", order_item_agent)
    graph.add_node("payment", payment_agent)
    graph.add_node("shipment", shipment_agent)
    graph.add_node("policy", policy_agent)
    graph.add_node("verifier", verifier_agent)
    graph.add_edge(START, "coordinator")
    for specialist in ("order_item", "payment", "shipment"):
        graph.add_edge("coordinator", specialist)
    graph.add_edge(["order_item", "payment", "shipment"], "policy")
    graph.add_edge("policy", "verifier")
    graph.add_conditional_edges("verifier", _route, {END: END, "coordinator": "coordinator"})
    return graph.compile()


_GRAPH = build_graph()


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    store = EvidenceStore()
    rt = Runtime(
        case_id, EvidenceCollector(gateway, case_id, store), store, trace, gateway.contracts
    )
    try:
        state = await asyncio.wait_for(
            _GRAPH.ainvoke(
                {"case": case, "replans": 0, "signals": {}, "messages": []},
                config={"configurable": {"rt": rt}, "recursion_limit": 40},
            ),
            timeout=CASE_TIMEOUT_SECONDS,
        )
        output = state.get("output")
        if output is not None:
            return output
    except Exception as exc:  # one broken case must not stop the run
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier-agent",
            decision_code="fallback",
            attributes={"error": type(exc).__name__},
        )
    return rules.fallback_output(case, rt.fallback_refs())
